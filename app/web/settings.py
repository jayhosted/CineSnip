from __future__ import annotations

from typing import Awaitable, Callable

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.runtime import SettingsHolder
from app.settings import (
    LibrarySyncDefaults,
    QuoteMatchDefaults,
    RenderDefaults,
    Settings,
    write_config_yaml,
)
from app.worker import search_index

_TABS = [
    ("general", "General"),
    ("render", "Render & Subtitles"),
    ("audio", "Audio"),
    ("cache", "Cache & Sync"),
    ("discord", "Discord"),
    ("plex", "Plex"),
    ("libraries", "Libraries"),
]


def register_settings_routes(
    app: FastAPI,
    templates: Jinja2Templates,
    settings_holder: SettingsHolder,
    on_setup_complete: Callable[[], Awaitable[None]],
) -> None:
    def render_tab(request: Request, tab: str, panel: str, **ctx) -> HTMLResponse:
        settings = settings_holder.settings
        context = {
            "request": request, "tabs": _TABS, "current_tab": tab,
            "settings": settings, **ctx,
        }
        if request.headers.get("HX-Request"):
            return HTMLResponse(templates.env.get_template(panel).render(context))
        context["content_template"] = panel
        return templates.TemplateResponse(
            request, "shell.html",
            {**context, "page_title": "Settings", "current_page": "settings"},
        )

    async def apply(new_settings: Settings) -> None:
        # Writes the whole config.yaml-owned surface of `new_settings`, then
        # runs the SAME apply path the wizard's "Finish setup" step uses
        # (app/web/app.py's on_setup_complete, threaded in from app/main.py):
        # it re-reads .env/config.yaml, swaps settings_holder.settings, and
        # signals main()'s live-reload loop — a Settings save is just
        # another producer of that one signal, no new plumbing needed.
        write_config_yaml(new_settings)
        await on_setup_complete()

    @app.get("/settings")
    async def settings_index(request: Request):
        if settings_holder.settings is None:
            return RedirectResponse("/")
        return RedirectResponse("/settings/general")

    # ---- General ---------------------------------------------------------

    @app.get("/settings/general", response_class=HTMLResponse)
    async def settings_general(request: Request):
        return render_tab(request, "general", "panel_settings_general.html")

    @app.post("/settings/general", response_class=HTMLResponse)
    async def settings_general_save(request: Request):
        settings = settings_holder.settings
        form = await request.form()

        dev_guild_id_raw = str(form.get("dev_guild_id", "")).strip()
        try:
            dev_guild_id = int(dev_guild_id_raw) if dev_guild_id_raw else None
        except ValueError:
            return render_tab(
                request, "general", "panel_settings_general.html",
                error="Dev guild ID must be a numeric Discord server ID.",
            )

        updated = settings.model_copy(deep=True)
        updated.dev_guild_id = dev_guild_id
        await apply(updated)
        return render_tab(request, "general", "panel_settings_general.html", saved=True)

    # ---- Render & Subtitles ------------------------------------------------

    @app.get("/settings/render", response_class=HTMLResponse)
    async def settings_render(request: Request):
        return render_tab(request, "render", "panel_settings_render.html")

    @app.post("/settings/render", response_class=HTMLResponse)
    async def settings_render_save(request: Request):
        settings = settings_holder.settings
        form = await request.form()

        try:
            render_defaults = RenderDefaults(
                duration_seconds=float(form["duration_seconds"]),
                fps=int(form["fps"]),
                width=int(form["width"]),
                timeout_seconds=float(form["timeout_seconds"]),
                format=str(form["format"]),
                min_duration_seconds=float(form["min_duration_seconds"]),
                max_duration_seconds=float(form["max_duration_seconds"]),
                # gifsicle_timeout_seconds/max_file_size_bytes/audio_language
                # aren't in this form (audio_language lives on its own Audio
                # tab) — preserved from the existing settings rather than
                # silently reset to RenderDefaults' own Pydantic defaults.
                gifsicle_timeout_seconds=settings.render_defaults.gifsicle_timeout_seconds,
                max_file_size_bytes=settings.render_defaults.max_file_size_bytes,
                audio_language=settings.render_defaults.audio_language,
            )
            quote_match = QuoteMatchDefaults(
                fetch_limit=int(form["fetch_limit"]),
                min_score=float(form["min_score"]),
                confident_score=float(form["confident_score"]),
                max_window_gap_seconds=float(form["max_window_gap_seconds"]),
                context_lines=int(form["context_lines"]),
                library_per_title_limit=int(form["library_per_title_limit"]),
            )
        except (KeyError, ValueError) as exc:
            return render_tab(
                request, "render", "panel_settings_render.html",
                error=f"Couldn't save — check your values ({exc}).",
            )

        updated = settings.model_copy(deep=True)
        updated.render_defaults = render_defaults
        updated.quote_match = quote_match
        await apply(updated)
        return render_tab(request, "render", "panel_settings_render.html", saved=True)

    # ---- Audio ---------------------------------------------------------
    # Split out from Render & Subtitles into its own tab since more
    # audio-specific settings are planned (Discord Soundboard replacement
    # options, issue #10) — kept together here rather than crowding the
    # render tab as that grows.

    @app.get("/settings/audio", response_class=HTMLResponse)
    async def settings_audio(request: Request):
        return render_tab(request, "audio", "panel_settings_audio.html")

    @app.post("/settings/audio", response_class=HTMLResponse)
    async def settings_audio_save(request: Request):
        settings = settings_holder.settings
        form = await request.form()

        audio_language = str(form.get("audio_language", "")).strip()
        if not audio_language:
            return render_tab(
                request, "audio", "panel_settings_audio.html",
                error="Audio language can't be blank.",
            )

        soundboard_replace_scope = str(form.get("soundboard_replace_scope", "")).strip()
        if soundboard_replace_scope not in ("cinesnip_only", "any", "none"):
            return render_tab(
                request, "audio", "panel_settings_audio.html",
                error="Soundboard replace scope must be one of: cinesnip_only, any, none.",
            )

        updated = settings.model_copy(deep=True)
        updated.render_defaults.audio_language = audio_language
        updated.render_defaults.soundboard_replace_scope = soundboard_replace_scope
        await apply(updated)
        return render_tab(request, "audio", "panel_settings_audio.html", saved=True)

    # ---- Cache & Sync ------------------------------------------------------

    @app.get("/settings/cache", response_class=HTMLResponse)
    async def settings_cache(request: Request):
        cached_count = len(search_index.list_titles(settings_holder.settings.quote_index_db_path))
        return render_tab(request, "cache", "panel_settings_cache.html", cached_count=cached_count)

    @app.post("/settings/cache", response_class=HTMLResponse)
    async def settings_cache_save(request: Request):
        settings = settings_holder.settings
        form = await request.form()

        try:
            library_sync = LibrarySyncDefaults(
                enabled=form.get("enabled") == "on",
                interval_hours=float(form["interval_hours"]),
            )
        except (KeyError, ValueError) as exc:
            cached_count = len(search_index.list_titles(settings.quote_index_db_path))
            return render_tab(
                request, "cache", "panel_settings_cache.html", cached_count=cached_count,
                error=f"Couldn't save — check your values ({exc}).",
            )

        updated = settings.model_copy(deep=True)
        updated.library_sync = library_sync
        await apply(updated)
        cached_count = len(search_index.list_titles(updated.quote_index_db_path))
        return render_tab(request, "cache", "panel_settings_cache.html", cached_count=cached_count, saved=True)

    # ---- Discord / Plex / Libraries (read-only summary + link to the
    # existing wizard steps, per the user's explicit choice — reused as-is
    # rather than rebuilding token/PIN/path-mapping forms a second time) --

    @app.get("/settings/discord", response_class=HTMLResponse)
    async def settings_discord(request: Request):
        from app.web.app import _verify_discord_token, discord_invite_url

        settings = settings_holder.settings
        invite_url = None
        if settings.discord_token:
            ok, _msg, payload = await _verify_discord_token(settings.discord_token)
            if ok and payload and payload.get("id"):
                invite_url = discord_invite_url(payload["id"])
        return render_tab(request, "discord", "panel_settings_discord.html", invite_url=invite_url)

    @app.get("/settings/plex", response_class=HTMLResponse)
    async def settings_plex(request: Request):
        return render_tab(request, "plex", "panel_settings_plex.html")

    @app.get("/settings/libraries", response_class=HTMLResponse)
    async def settings_libraries(request: Request):
        return render_tab(request, "libraries", "panel_settings_libraries.html")
