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
from app.worker import quote_index

_TABS = [
    ("general", "General"),
    ("render", "Render & Subtitles"),
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
            )
            quote_match = QuoteMatchDefaults(
                candidate_limit=int(form["candidate_limit"]),
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

    # ---- Cache & Sync ------------------------------------------------------

    @app.get("/settings/cache", response_class=HTMLResponse)
    async def settings_cache(request: Request):
        cached_count = len(quote_index.list_cached_titles(settings_holder.settings.quote_index_db_path))
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
            cached_count = len(quote_index.list_cached_titles(settings.quote_index_db_path))
            return render_tab(
                request, "cache", "panel_settings_cache.html", cached_count=cached_count,
                error=f"Couldn't save — check your values ({exc}).",
            )

        updated = settings.model_copy(deep=True)
        updated.library_sync = library_sync
        await apply(updated)
        cached_count = len(quote_index.list_cached_titles(updated.quote_index_db_path))
        return render_tab(request, "cache", "panel_settings_cache.html", cached_count=cached_count, saved=True)

    # ---- Discord / Plex / Libraries (read-only summary + link to the
    # existing wizard steps, per the user's explicit choice — reused as-is
    # rather than rebuilding token/PIN/path-mapping forms a second time) --

    @app.get("/settings/discord", response_class=HTMLResponse)
    async def settings_discord(request: Request):
        return render_tab(request, "discord", "panel_settings_discord.html")

    @app.get("/settings/plex", response_class=HTMLResponse)
    async def settings_plex(request: Request):
        return render_tab(request, "plex", "panel_settings_plex.html")

    @app.get("/settings/libraries", response_class=HTMLResponse)
    async def settings_libraries(request: Request):
        return render_tab(request, "libraries", "panel_settings_libraries.html")
