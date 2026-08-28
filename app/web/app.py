from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Awaitable, Callable, TypeVar

import httpx
import yaml
from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from plexapi.exceptions import Unauthorized
from plexapi.myplex import MyPlexPinLogin
from plexapi.server import PlexServer

from app.runtime import SettingsHolder
from app.settings import (
    LibrarySyncDefaults,
    QuoteMatchDefaults,
    RenderDefaults,
    Settings,
    SubtitleDefaults,
    WorkerConfig,
)
from app.web.generate import register_generate_routes
from app.web.settings import register_settings_routes
from app.web.state import LibraryChoice, MappingRow, WizardState, media_mount_candidates

logger = logging.getLogger(__name__)

_TEMPLATES_DIR = Path(__file__).parent / "templates"
_STATIC_DIR = Path(__file__).parent / "static"

# Bounds how long the wizard will wait on a Plex call before giving up.
# plexapi's own `timeout=10` on PlexServer only bounds a single HTTP
# request — it doesn't stop a genuinely hung/black-holed connection from
# blocking forever, and every Plex call here runs in a worker thread that
# can't be forcibly killed once it's actually stuck (confirmed the hard
# way: repeated real attempts against a truly unresponsive path left a
# long-running wizard process with orphaned threads that never returned,
# eventually starving the thread pool so *every* later Plex-touching
# request queued forever too — the whole wizard became unusable until the
# process was restarted). asyncio.wait_for at least bounds the wait from
# the caller's side so one bad attempt surfaces a clear error instead of a
# silently dead button, even though the underlying thread may still linger.
_PLEX_CALL_TIMEOUT_SECONDS = 45.0

# View Channels (1024) + Send Messages (2048) + Embed Links (16384) +
# Attach Files (32768) — the minimum CineSnip's slash commands actually
# need (posting a GIF/video attachment, in an embed, in a channel it can
# see). "Use Application Commands" isn't a permission bit at all — it comes
# from the `applications.commands` OAuth2 scope in the invite URL below,
# not the permissions bitmask.
_DISCORD_INVITE_PERMISSIONS = 1024 + 2048 + 16384 + 32768


def discord_invite_url(bot_id: str) -> str:
    return (
        f"https://discord.com/oauth2/authorize?client_id={bot_id}"
        f"&scope=bot%20applications.commands&permissions={_DISCORD_INVITE_PERMISSIONS}"
    )


async def _verify_discord_token(token: str) -> tuple[bool, str, dict | None]:
    # Shared by discord_submit (step 1) and _validate_panel (step 4's final
    # check) so there's exactly one place that calls Discord to check a
    # token — the final check used to just trust step 1's in-memory result
    # instead of re-verifying, which meant a token revoked/regenerated
    # between step 1 and clicking "Finish setup" still showed green.
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://discord.com/api/v10/users/@me",
                headers={"Authorization": f"Bot {token}"},
            )
    except httpx.HTTPError:
        return False, "Couldn't reach Discord — check your network and try again.", None

    if resp.status_code != 200:
        return False, "That token was rejected by Discord. Double-check it and try again.", None

    payload = resp.json()
    username = payload.get("username", "bot")
    return True, f"logged in as {username}", payload


_T = TypeVar("_T")


async def _call_with_timeout(func, *args, timeout: float = _PLEX_CALL_TIMEOUT_SECONDS) -> _T:
    return await asyncio.wait_for(run_in_threadpool(func, *args), timeout=timeout)


def _build_filename_index(candidates: list[str]) -> dict[str, list[tuple[str, str]]]:
    # Walks each mounted candidate exactly ONCE, building filename -> [(root,
    # relative_path), ...]. This replaced a version that called
    # Path(candidate).rglob("*") fresh for every single sampled file — fine
    # against a small handful of titles, but a real correctness-at-scale bug
    # against a real library: up to 40 samples x up to 3 libraries x up to 5
    # mounted drives meant potentially hundreds of full recursive directory
    # walks for one "Connect & continue" click. Confirmed against this
    # developer's real ~11,000-title library: that took over 45 seconds and
    # tripped the wizard's own timeout; building one index up front and
    # doing O(1) dict lookups for every sample afterward is what the design
    # actually called for.
    #
    # os.walk (not rglob) so a literal '[' ']' in a real release filename
    # (e.g. "[Bluray-1080p][AC3 5.1][x265]") is never treated as glob syntax
    # — the same bracket bug rglob had, avoided the same way.
    index: dict[str, list[tuple[str, str]]] = {}
    for candidate in candidates:
        for root, _dirs, files in os.walk(candidate):
            for name in files:
                rel = os.path.relpath(os.path.join(root, name), candidate).replace(os.sep, "/")
                index.setdefault(name, []).append((candidate, rel))
    return index


def _suggest_mapping(
    sample_plex_path: str | None, filename_index: dict[str, list[tuple[str, str]]]
) -> tuple[str | None, str | None]:
    # Section 14: "auto-suggest path mappings by comparing the Plex-reported
    # file path against what's actually visible under the container's
    # mounted volumes" — the strongest real signal is whether the sampled
    # file's own basename exists inside a candidate mount, not just a
    # similar-looking folder name (two libraries can share a folder name
    # like "Movies" on different drives).
    #
    # Once a real match is found, the plex_prefix isn't just "the sample
    # file's parent folder" — that would only be correct for this one title.
    # It's derived by stripping the matched file's own relative path (under
    # the container mount) off the end of the Plex-reported path, leaving
    # the shared library ROOT both sides agree on — e.g. Plex path
    # "D:\Plex Additional\Movies\Foo (2020)\Foo.mkv" matched under
    # "/media/movies-d/Foo (2020)/Foo.mkv" strips "Foo (2020)/Foo.mkv" off
    # both, leaving "D:\Plex Additional\Movies" <-> "/media/movies-d".
    #
    # Returns (container_path, plex_prefix) — either may be None if no
    # match was found, in which case the user picks/confirms manually.
    if not sample_plex_path:
        return None, None

    normalized_plex_path = sample_plex_path.replace("\\", "/").rstrip("/")
    filename = normalized_plex_path.rsplit("/", 1)[-1]

    for candidate, relative in filename_index.get(filename, []):
        if normalized_plex_path.endswith(relative):
            prefix = normalized_plex_path[: -(len(relative) + 1)]
            # Report the prefix back in the same separator style Plex used,
            # so it's recognizable/editable if the user checks it.
            if "\\" in sample_plex_path:
                prefix = prefix.replace("/", "\\")
            return candidate, prefix

    return None, None


def _discover_library_choices_sync(section, filename_index: dict[str, list[tuple[str, str]]]) -> LibraryChoice:
    # A library can span more than one physical folder/drive (Section 3 —
    # this developer's own Movies and TV Shows each really do span two
    # drives), so a single sampled item can only ever suggest one of them.
    # Sampling a broader slice (~40 items — confirmed fast against this
    # developer's real library: media parts already ride along with the
    # search results, no extra per-item round trip) reliably surfaces every
    # distinct mounted folder the library actually touches.
    try:
        items = section.searchEpisodes(maxresults=40) if section.type == "show" else section.all(maxresults=40)
    except Exception:
        items = []

    discovered: dict[str, str] = {}  # container_path -> plex_prefix
    for item in items:
        try:
            sample_path = item.media[0].parts[0].file
        except Exception:
            continue
        container, prefix = _suggest_mapping(sample_path, filename_index)
        if container and container not in discovered:
            discovered[container] = prefix or ""

    suggested = [MappingRow(plex_prefix=prefix, container_path=container) for container, prefix in discovered.items()]
    rows = [MappingRow(plex_prefix=r.plex_prefix, container_path=r.container_path) for r in suggested]
    if not rows:
        # Nothing auto-detected — still give the user one blank row to fill
        # in by hand, rather than an empty block with only the "+ Add
        # another location" button to start from.
        rows.append(MappingRow())

    return LibraryChoice(name=section.title, section_type=section.type, mapping_rows=rows, suggested_rows=suggested)


def _connect_and_discover_sync(plex_url: str, account_token: str | None) -> tuple[str, list[LibraryChoice]]:
    # Every call in here is real, blocking network I/O to Plex (plexapi has
    # no async API) — this whole function is meant to be run off the event
    # loop via run_in_threadpool. Doing this inline inside an `async def`
    # route instead was a real bug, not just slow: it blocked Uvicorn's
    # single event loop entirely for the duration of the call, freezing the
    # *whole* wizard server (every other request, every other user) for
    # however long Plex took to answer — confirmed by hitting it for real
    # against this developer's library, where 3 libraries' worth of
    # sampling took long enough to make the server briefly unresponsive to
    # everything, not just show a slow button.
    server = PlexServer(plex_url, account_token, timeout=10)
    server_name = server.friendlyName
    filename_index = _build_filename_index(media_mount_candidates())
    choices = [
        _discover_library_choices_sync(section, filename_index)
        for section in server.library.sections()
        if section.type in ("movie", "show")
    ]
    return server_name, choices


def _run_validation_sync(state: WizardState) -> list[tuple[str, bool, str]]:
    # Discord's own check is deliberately NOT here — it needs a live async
    # HTTP call (_verify_discord_token), and this function runs off the
    # event loop via run_in_threadpool alongside the Plex checks below.
    # _validate_panel awaits it separately and prepends the result.
    from app.worker.path_mapper import NoPathMappingError, resolve_container_path

    checks: list[tuple[str, bool, str]] = []

    try:
        plex_server = PlexServer(state.plex_url, state.plex_account_token, timeout=10)
        plex_server.friendlyName
        checks.append(("Plex server reachable", True, state.plex_server_name or state.plex_url))
    except Exception as exc:
        checks.append(("Plex server reachable", False, str(exc)))
        plex_server = None

    for library in state.selected_libraries():
        if not library.path_mappings:
            checks.append((f"{library.name}: files resolve", False, "no path mapping entered"))
            continue
        if plex_server is None:
            checks.append((f"{library.name}: files resolve", False, "Plex unreachable"))
            continue

        try:
            section = plex_server.library.section(library.name)
            items = section.searchEpisodes(maxresults=10) if section.type == "show" else section.all(maxresults=10)
        except Exception as exc:
            checks.append((f"{library.name}: files resolve", False, str(exc)))
            continue

        resolved_count = 0
        total = 0
        first_problem = None
        for item in items:
            try:
                plex_path = item.media[0].parts[0].file
            except Exception:
                continue
            total += 1
            try:
                resolved = resolve_container_path(plex_path, library.path_mappings)
                if Path(resolved).exists():
                    resolved_count += 1
                elif first_problem is None:
                    first_problem = f"{resolved} not found on disk"
            except NoPathMappingError as exc:
                if first_problem is None:
                    first_problem = str(exc)

        ok = total > 0 and resolved_count == total
        detail = f"{resolved_count}/{total} sample titles resolve" if total else "no titles found to sample"
        if not ok and first_problem:
            detail += f" — {first_problem}"
        checks.append((f"{library.name}: files resolve", ok, detail))

    return checks


def create_web_app(
    settings_holder: SettingsHolder, on_setup_complete: Callable[[], Awaitable[None]]
) -> FastAPI:
    # One persistent app for the whole container lifetime (Section 14 /
    # decision #6), not the throwaway instance this used to be: while
    # settings_holder.settings is None only the wizard routes below do
    # anything useful (there's no worker to talk to yet); once it's set,
    # /generate (app/web/generate.py) also comes alive and /wizard/... stays
    # reachable afterward as the reconfiguration entry point rather than
    # disappearing once setup completes.
    app = FastAPI(title="CineSnip")
    app.state.wizard = WizardState()
    templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")
    register_generate_routes(app, templates, settings_holder)
    register_settings_routes(app, templates, settings_holder, on_setup_complete)

    def render(request: Request, panel: str, **ctx) -> HTMLResponse:
        # A plain page load renders the full shell (page.html) with the
        # current step's panel included; an htmx form response renders just
        # the panel fragment and swaps it into #wizard-panel in place — same
        # template either way, so the two paths can never drift apart. The
        # step-nav strip lives outside #wizard-panel, so an htmx response
        # also carries an out-of-band update for it (step_nav_oob.html) —
        # otherwise the "2 of 4" indicator would silently go stale the
        # moment a step transition happens without a full page reload.
        state: WizardState = request.app.state.wizard
        context = {"request": request, "state": state, "current_step": state.current_step, **ctx}
        if request.headers.get("HX-Request"):
            nav_html = templates.env.get_template("step_nav_oob.html").render(context)
            panel_html = templates.env.get_template(panel).render(context)
            return HTMLResponse(nav_html + panel_html)
        context["panel_template"] = panel
        return templates.TemplateResponse(request, "page.html", context)

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        if settings_holder.settings is None:
            state: WizardState = request.app.state.wizard
            step = state.current_step
            return RedirectResponse(
                {1: "/wizard/discord", 2: "/wizard/plex", 3: "/wizard/libraries", 4: "/wizard/validate"}[step]
            )
        return RedirectResponse("/generate")

    @app.get("/wizard/restart")
    async def wizard_restart(request: Request):
        # The only entry point back into a completed wizard (Section 14):
        # always starts a fresh WizardState rather than trying to prefill
        # from the live Settings — simpler, and correct regardless of
        # whether the running config came from the wizard, hand-edited
        # files, or a previous reconfiguration.
        request.app.state.wizard = WizardState()
        return RedirectResponse("/wizard/discord")

    # ---- Step 1: Discord ----------------------------------------------

    @app.get("/wizard/discord", response_class=HTMLResponse)
    async def discord_step(request: Request):
        return render(request, "panel_discord.html")

    @app.post("/wizard/discord", response_class=HTMLResponse)
    async def discord_submit(request: Request):
        form = await request.form()
        token = str(form.get("discord_token", "")).strip()
        state: WizardState = request.app.state.wizard

        if not token:
            return render(request, "panel_discord.html", error="Enter a bot token.")

        ok, detail, payload = await _verify_discord_token(token)
        if not ok:
            return render(request, "panel_discord.html", error=detail)

        state.discord_token = token
        state.discord_username = payload.get("username", "bot")
        # A bot's own user ID is its application's client ID — this is what
        # actually lets the wizard build a real, working invite URL instead
        # of just telling the user to go figure one out themselves.
        state.discord_bot_id = payload.get("id")
        invite_url = discord_invite_url(state.discord_bot_id) if state.discord_bot_id else None
        return render(request, "panel_discord_invite.html", invite_url=invite_url)

    # ---- Step 2: Plex --------------------------------------------------

    async def _ensure_pin(state: WizardState) -> MyPlexPinLogin:
        if state.plex_pin is None:
            pin_login = MyPlexPinLogin(
                headers={"X-Plex-Product": "CineSnip", "X-Plex-Client-Identifier": "cinesnip-wizard"}
            )
            # .pin is a property, but accessing it is what actually makes
            # the (blocking) request to plex.tv — offload it like every
            # other real Plex network call in this module.
            await _call_with_timeout(lambda: pin_login.pin)
            state.plex_pin = pin_login
        return state.plex_pin

    @app.get("/wizard/plex", response_class=HTMLResponse)
    async def plex_step(request: Request):
        state: WizardState = request.app.state.wizard
        try:
            pin = await _ensure_pin(state)
        except asyncio.TimeoutError:
            return render(
                request, "panel_plex_pin.html", pin=None,
                error=f"plex.tv didn't respond within {int(_PLEX_CALL_TIMEOUT_SECONDS)}s. Check your network and try again.",
            )
        except Exception:
            return render(
                request, "panel_plex_pin.html", pin=None,
                error="Couldn't get a code from plex.tv. Check your network and try again.",
            )
        return render(request, "panel_plex_pin.html", pin=pin)

    @app.get("/wizard/plex/status", response_class=HTMLResponse)
    async def plex_status(request: Request):
        state: WizardState = request.app.state.wizard
        try:
            pin: MyPlexPinLogin = await _ensure_pin(state)
        except asyncio.TimeoutError:
            return render(
                request, "panel_plex_pin.html", pin=None,
                error=f"plex.tv didn't respond within {int(_PLEX_CALL_TIMEOUT_SECONDS)}s. Check your network and try again.",
            )
        except Exception:
            return render(
                request, "panel_plex_pin.html", pin=None,
                error="Couldn't get a code from plex.tv. Check your network and try again.",
            )

        try:
            logged_in = await _call_with_timeout(pin.checkLogin, timeout=15)
        except asyncio.TimeoutError:
            # A single slow poll isn't fatal — the next one (2s later, per
            # panel_plex_pin.html's hx-trigger) just tries again.
            return render(request, "panel_plex_pin.html", pin=pin)

        if logged_in:
            state.plex_account_token = pin.token
            return render(request, "panel_plex_connect.html")

        if pin.expired:
            state.plex_pin = None
            new_pin = await _ensure_pin(state)
            return render(request, "panel_plex_pin.html", pin=new_pin, error="That code expired — here's a new one.")

        return render(request, "panel_plex_pin.html", pin=pin)

    @app.post("/wizard/plex/connect", response_class=HTMLResponse)
    async def plex_connect(request: Request):
        form = await request.form()
        plex_url = str(form.get("plex_url", "")).strip().rstrip("/")
        state: WizardState = request.app.state.wizard

        if not plex_url:
            return render(request, "panel_plex_connect.html", error="Enter your Plex server's address.")

        try:
            server_name, choices = await _call_with_timeout(_connect_and_discover_sync, plex_url, state.plex_account_token)
        except Unauthorized:
            return render(
                request, "panel_plex_connect.html",
                error="Plex rejected that connection — your account may not have access to this server.",
            )
        except asyncio.TimeoutError:
            return render(
                request, "panel_plex_connect.html",
                error=f"Plex at {plex_url} didn't respond within {int(_PLEX_CALL_TIMEOUT_SECONDS)}s. "
                      f"Double-check the address and that CineSnip's container can actually reach it, then try again.",
            )
        except Exception:
            return render(
                request, "panel_plex_connect.html",
                error=f"Couldn't reach a Plex server at {plex_url}. Check the address and that CineSnip's container can reach it.",
            )

        state.plex_url = plex_url
        state.plex_server_name = server_name
        state.library_choices = choices

        return render(request, "panel_libraries.html", mounts=media_mount_candidates())

    # ---- Step 3: Libraries ----------------------------------------------

    def _sync_library_choices_from_form(state: WizardState, form) -> None:
        for i, choice in enumerate(state.library_choices):
            choice.selected = form.get(f"lib_{i}_selected") == "on"
            three_d = str(form.get(f"lib_{i}_three_d_format", "none")).strip()
            if three_d in ("none", "side_by_side", "over_under"):
                choice.three_d_format = three_d
            for j, row in enumerate(choice.mapping_rows):
                row.plex_prefix = str(form.get(f"lib_{i}_mapping_{j}_plex_prefix", "")).strip()
                row.container_path = str(form.get(f"lib_{i}_mapping_{j}_container_path", "")).strip()

    @app.get("/wizard/libraries", response_class=HTMLResponse)
    async def libraries_step(request: Request):
        return render(request, "panel_libraries.html", mounts=media_mount_candidates())

    @app.post("/wizard/libraries/add-row/{lib_index}", response_class=HTMLResponse)
    async def libraries_add_row(request: Request, lib_index: int):
        # The "+ Add another location" button lives inside the form and
        # posts with hx-include="closest form", so whatever the user's
        # already typed/checked elsewhere on the page comes along and isn't
        # lost — this re-syncs state from that first, then appends one
        # blank row to just the library that was clicked.
        form = await request.form()
        state: WizardState = request.app.state.wizard
        _sync_library_choices_from_form(state, form)
        if 0 <= lib_index < len(state.library_choices):
            state.library_choices[lib_index].mapping_rows.append(MappingRow())
        return render(request, "panel_libraries.html", mounts=media_mount_candidates())

    @app.post("/wizard/libraries/remove-row/{lib_index}/{row_index}", response_class=HTMLResponse)
    async def libraries_remove_row(request: Request, lib_index: int, row_index: int):
        form = await request.form()
        state: WizardState = request.app.state.wizard
        _sync_library_choices_from_form(state, form)
        if 0 <= lib_index < len(state.library_choices):
            rows = state.library_choices[lib_index].mapping_rows
            if 0 <= row_index < len(rows):
                rows.pop(row_index)
            if not rows:
                # A library block always shows at least one row to fill in,
                # same as right after discovery finds nothing.
                rows.append(MappingRow())
        return render(request, "panel_libraries.html", mounts=media_mount_candidates())

    @app.post("/wizard/libraries/restore-row/{lib_index}/{suggested_index}", response_class=HTMLResponse)
    async def libraries_restore_row(request: Request, lib_index: int, suggested_index: int):
        # Brings back one specific auto-suggested mapping that was removed
        # (accidentally or otherwise) — the only way to get it back at all,
        # since re-deriving it means reconnecting to Plex and re-scanning.
        form = await request.form()
        state: WizardState = request.app.state.wizard
        _sync_library_choices_from_form(state, form)
        if 0 <= lib_index < len(state.library_choices):
            choice = state.library_choices[lib_index]
            if 0 <= suggested_index < len(choice.suggested_rows):
                suggestion = choice.suggested_rows[suggested_index]
                already_present = any(
                    r.container_path == suggestion.container_path and r.plex_prefix == suggestion.plex_prefix
                    for r in choice.mapping_rows
                )
                if not already_present:
                    choice.mapping_rows.append(MappingRow(suggestion.plex_prefix, suggestion.container_path))
        return render(request, "panel_libraries.html", mounts=media_mount_candidates())

    @app.post("/wizard/libraries", response_class=HTMLResponse)
    async def libraries_submit(request: Request):
        form = await request.form()
        state: WizardState = request.app.state.wizard
        _sync_library_choices_from_form(state, form)

        if not any(c.selected for c in state.library_choices):
            return render(request, "panel_libraries.html", mounts=media_mount_candidates(), error="Pick at least one library.")

        return await _validate_panel(request)

    # ---- Step 4: Validate -----------------------------------------------

    async def _validate_panel(request: Request) -> HTMLResponse:
        # Validates against the ACTUAL end result (a real sample of titles
        # resolving under whatever path_mappings the user ended up with),
        # rather than trying to track which specific suggestion each row
        # came from — simpler, and correct regardless of whether a mapping
        # was auto-suggested, hand-edited, or added from a blank row for a
        # folder the sampler missed.
        state: WizardState = request.app.state.wizard

        if state.discord_token:
            discord_ok, discord_detail, _ = await _verify_discord_token(state.discord_token)
        else:
            discord_ok, discord_detail = False, "no token entered yet"
        checks: list[tuple[str, bool, str]] = [("Discord bot token", discord_ok, discord_detail)]

        try:
            checks += await _call_with_timeout(_run_validation_sync, state)
        except asyncio.TimeoutError:
            checks.append((
                "Validation",
                False,
                f"Plex didn't respond within {int(_PLEX_CALL_TIMEOUT_SECONDS)}s — check the connection and try again.",
            ))
        all_ok = all(ok for _, ok, _ in checks)
        state.last_validation_ok = all_ok
        return render(request, "panel_validate.html", checks=checks, all_ok=all_ok)

    @app.get("/wizard/validate", response_class=HTMLResponse)
    async def validate_step(request: Request):
        return await _validate_panel(request)

    @app.post("/wizard/finish", response_class=HTMLResponse)
    async def finish(request: Request):
        # Re-run validation server-side rather than trusting that the
        # "Finish setup" button was only reachable because the last render
        # showed all-green — a stale tab, a replayed request, or Plex/Discord
        # going away in between must not still write an unvalidated config.
        state: WizardState = request.app.state.wizard
        checks_response = await _validate_panel(request)
        if not state.last_validation_ok:
            return checks_response
        _write_config_files(state, current_settings=settings_holder.settings)
        await on_setup_complete()
        invite_url = discord_invite_url(state.discord_bot_id) if state.discord_bot_id else None
        return render(request, "panel_complete.html", invite_url=invite_url)

    return app


def _write_config_files(
    state: WizardState,
    current_settings: Settings | None = None,
    env_path: Path = Path(".env"),
    config_path: Path = Path("config.yaml"),
) -> None:
    # Written directly to the same two files app/settings.py already reads
    # (Section 14: "the wizard is purely a friendlier way to produce those
    # same two files, not a new runtime secret-handling path"). Tokens are
    # never logged anywhere in this module — this function is the only place
    # they're written, and only ever to these files.
    existing_lines = env_path.read_text().splitlines() if env_path.exists() else []
    kept = [
        line
        for line in existing_lines
        if not line.startswith(("DISCORD_TOKEN=", "PLEX_URL=", "PLEX_TOKEN="))
    ]
    kept += [
        f"DISCORD_TOKEN={state.discord_token}",
        f"PLEX_URL={state.plex_url}",
        f"PLEX_TOKEN={state.plex_account_token}",
    ]
    env_path.write_text("\n".join(kept) + "\n")

    # Everything below "libraries" is preserved from the currently-running
    # Settings when there is one (a reconfiguration — Section 14/decision
    # #6) rather than rebuilt from Pydantic defaults every time: this
    # function is the wizard's own write path, and the wizard only ever
    # collects discord_token/plex_url/plex_token/libraries. Wiping
    # render_defaults/subtitle_defaults/quote_match/worker/library_sync
    # back to defaults on every re-run would silently discard anything
    # edited through the Settings area (app/web/settings.py) — this is
    # what makes "re-running setup only rewrites Discord/Plex/libraries"
    # actually true instead of just a claim in the UI copy. Falls back to
    # fresh Pydantic defaults only on a genuine first run, when there's
    # nothing yet to preserve. lib.model_dump() still means any future
    # LibraryConfig field (e.g. three_d_format) flows into the written
    # file automatically instead of needing to be added here by hand.
    config = {
        "libraries": [lib.model_dump() for lib in state.selected_libraries()],
        "render_defaults": (current_settings.render_defaults if current_settings else RenderDefaults()).model_dump(),
        "subtitle_defaults": (current_settings.subtitle_defaults if current_settings else SubtitleDefaults()).model_dump(),
        "quote_match": (current_settings.quote_match if current_settings else QuoteMatchDefaults()).model_dump(),
        "worker": (current_settings.worker if current_settings else WorkerConfig()).model_dump(),
        "library_sync": (current_settings.library_sync if current_settings else LibrarySyncDefaults()).model_dump(),
    }
    config_path.write_text(yaml.safe_dump(config, sort_keys=False))
