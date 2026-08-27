from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable

import httpx
import yaml
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from plexapi.exceptions import Unauthorized
from plexapi.myplex import MyPlexPinLogin
from plexapi.server import PlexServer

from app.web.state import LibraryChoice, WizardState, media_mount_candidates

logger = logging.getLogger(__name__)

_TEMPLATES_DIR = Path(__file__).parent / "templates"
_STATIC_DIR = Path(__file__).parent / "static"


def _suggest_mapping(sample_plex_path: str | None, candidates: list[str]) -> tuple[str | None, str | None]:
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
    if not sample_plex_path or not candidates:
        return None, None

    normalized_plex_path = sample_plex_path.replace("\\", "/").rstrip("/")
    filename = normalized_plex_path.rsplit("/", 1)[-1]

    for candidate in candidates:
        # Real release filenames routinely contain literal '[' ']' (e.g.
        # "[Bluray-1080p][AC3 5.1][x265]") — Path.rglob() treats those as
        # glob character-class syntax, not literal text, so passing the raw
        # filename straight to rglob() silently fails to match a large
        # fraction of real media (confirmed against this developer's own
        # library: a bracket-tagged file matched zero results). Walking and
        # comparing names literally avoids that entirely.
        for match in Path(candidate).rglob("*"):
            if not match.is_file() or match.name != filename:
                continue
            relative = match.relative_to(candidate).as_posix()
            if normalized_plex_path.endswith(relative):
                prefix = normalized_plex_path[: -(len(relative) + 1)]
                # Report the prefix back in the same separator style Plex
                # used, so it's recognizable/editable if the user checks it.
                if "\\" in sample_plex_path:
                    prefix = prefix.replace("/", "\\")
                return candidate, prefix

    return None, None


def create_wizard_app(on_complete: Callable[[], None]) -> FastAPI:
    app = FastAPI(title="CineSnip Setup")
    app.state.wizard = WizardState()
    templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

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
        state: WizardState = request.app.state.wizard
        step = state.current_step
        return RedirectResponse(
            {1: "/wizard/discord", 2: "/wizard/plex", 3: "/wizard/libraries", 4: "/wizard/validate"}[step]
        )

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

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    "https://discord.com/api/v10/users/@me",
                    headers={"Authorization": f"Bot {token}"},
                )
        except httpx.HTTPError:
            return render(
                request, "panel_discord.html", error="Couldn't reach Discord — check your network and try again."
            )

        if resp.status_code != 200:
            return render(request, "panel_discord.html", error="That token was rejected by Discord. Double-check it and try again.")

        username = resp.json().get("username", "bot")
        state.discord_token = token
        state.discord_username = username
        return render(request, "panel_plex_pin.html", pin=_ensure_pin(state))

    # ---- Step 2: Plex --------------------------------------------------

    def _ensure_pin(state: WizardState) -> MyPlexPinLogin:
        if state.plex_pin is None:
            state.plex_pin = MyPlexPinLogin(
                headers={"X-Plex-Product": "CineSnip", "X-Plex-Client-Identifier": "cinesnip-wizard"}
            )
            state.plex_pin.pin  # noqa: B018 — property access is what actually requests the PIN
        return state.plex_pin

    @app.get("/wizard/plex", response_class=HTMLResponse)
    async def plex_step(request: Request):
        state: WizardState = request.app.state.wizard
        return render(request, "panel_plex_pin.html", pin=_ensure_pin(state))

    @app.get("/wizard/plex/status", response_class=HTMLResponse)
    async def plex_status(request: Request):
        state: WizardState = request.app.state.wizard
        pin: MyPlexPinLogin = _ensure_pin(state)

        if pin.checkLogin():
            state.plex_account_token = pin.token
            return render(request, "panel_plex_connect.html")

        if pin.expired:
            state.plex_pin = None
            return render(request, "panel_plex_pin.html", pin=_ensure_pin(state), error="That code expired — here's a new one.")

        return render(request, "panel_plex_pin.html", pin=pin)

    @app.post("/wizard/plex/connect", response_class=HTMLResponse)
    async def plex_connect(request: Request):
        form = await request.form()
        plex_url = str(form.get("plex_url", "")).strip().rstrip("/")
        state: WizardState = request.app.state.wizard

        if not plex_url:
            return render(request, "panel_plex_connect.html", error="Enter your Plex server's address.")

        try:
            server = PlexServer(plex_url, state.plex_account_token, timeout=10)
        except Unauthorized:
            return render(
                request, "panel_plex_connect.html",
                error="Plex rejected that connection — your account may not have access to this server.",
            )
        except Exception:
            return render(
                request, "panel_plex_connect.html",
                error=f"Couldn't reach a Plex server at {plex_url}. Check the address and that CineSnip's container can reach it.",
            )

        state.plex_url = plex_url
        state.plex_server_name = server.friendlyName

        mounts = media_mount_candidates()
        state.library_choices = []
        for section in server.library.sections():
            if section.type not in ("movie", "show"):
                continue
            sample_path = None
            try:
                if section.type == "show":
                    # section.all() on a show-type section returns Show
                    # objects, not Episodes — a Show has no .media/.parts of
                    # its own (only its episodes do), so this always raised
                    # and silently produced "no suggestion" for every TV
                    # library until caught by testing against this
                    # developer's real TV Shows library.
                    items = section.searchEpisodes(maxresults=1)
                else:
                    items = section.all(maxresults=1)
                if items:
                    part = items[0].media[0].parts[0]
                    sample_path = part.file
            except Exception:
                pass
            suggested_container, suggested_prefix = _suggest_mapping(sample_path, mounts)
            state.library_choices.append(
                LibraryChoice(
                    name=section.title,
                    section_type=section.type,
                    sample_plex_path=sample_path,
                    suggested_container_path=suggested_container,
                    suggested_plex_prefix=suggested_prefix,
                    container_path=suggested_container or "",
                )
            )

        return render(request, "panel_libraries.html", mounts=mounts)

    # ---- Step 3: Libraries ----------------------------------------------

    @app.get("/wizard/libraries", response_class=HTMLResponse)
    async def libraries_step(request: Request):
        return render(request, "panel_libraries.html", mounts=media_mount_candidates())

    @app.post("/wizard/libraries", response_class=HTMLResponse)
    async def libraries_submit(request: Request):
        form = await request.form()
        state: WizardState = request.app.state.wizard

        for i, choice in enumerate(state.library_choices):
            choice.selected = form.get(f"lib_{i}_selected") == "on"
            choice.container_path = str(form.get(f"lib_{i}_container_path", "")).strip()

        if not any(c.selected for c in state.library_choices):
            return render(request, "panel_libraries.html", mounts=media_mount_candidates(), error="Pick at least one library.")

        return await _validate_panel(request)

    # ---- Step 4: Validate -----------------------------------------------

    async def _validate_panel(request: Request) -> HTMLResponse:
        state: WizardState = request.app.state.wizard
        checks = []

        checks.append(("Discord bot token", True, f"logged in as {state.discord_username}"))

        try:
            PlexServer(state.plex_url, state.plex_account_token, timeout=10).friendlyName
            checks.append(("Plex server reachable", True, state.plex_server_name or state.plex_url))
        except Exception as exc:
            checks.append(("Plex server reachable", False, str(exc)))

        for library in state.selected_libraries():
            mapping = library.path_mappings[0] if library.path_mappings else None
            choice = next((c for c in state.library_choices if c.name == library.name), None)
            if not mapping or not choice or not choice.sample_plex_path:
                checks.append((f"{library.name}: sample file resolves", False, "no sample file found"))
                continue
            from app.worker.path_mapper import NoPathMappingError, resolve_container_path

            try:
                resolved = resolve_container_path(choice.sample_plex_path, library.path_mappings)
                ok = Path(resolved).exists()
                checks.append((f"{library.name}: sample file resolves", ok, resolved if ok else f"{resolved} not found on disk"))
            except NoPathMappingError as exc:
                checks.append((f"{library.name}: sample file resolves", False, str(exc)))

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
        _write_config_files(state)
        on_complete()
        return render(request, "panel_complete.html")

    return app


def _write_config_files(state: WizardState, env_path: Path = Path(".env"), config_path: Path = Path("config.yaml")) -> None:
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

    config = {
        "libraries": [
            {
                "name": lib.name,
                "path_mappings": [
                    {"plex_prefix": m.plex_prefix, "container_path": m.container_path}
                    for m in lib.path_mappings
                ],
            }
            for lib in state.selected_libraries()
        ],
        "render_defaults": {
            "duration_seconds": 4,
            "fps": 15,
            "width": 480,
            "min_duration_seconds": 1,
            "max_duration_seconds": 15,
            "format": "gif",
        },
        "subtitle_defaults": {"extraction_timeout_seconds": 300},
        "quote_match": {
            "candidate_limit": 8,
            "min_score": 50,
            "confident_score": 85,
            "max_window_gap_seconds": 3,
            "context_lines": 1,
        },
        "worker": {"port": 8000},
        "library_sync": {"enabled": False, "interval_hours": 24},
    }
    config_path.write_text(yaml.safe_dump(config, sort_keys=False))
