from __future__ import annotations

from typing import Awaitable, Callable

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.bot.worker_client import WorkerClient
from app.runtime import SettingsHolder
from app.settings import (
    LibrarySyncDefaults,
    QuoteMatchDefaults,
    RenderDefaults,
    Settings,
    StylePresetConfig,
    StylePresetError,
    delete_style_preset,
    upsert_style_preset,
    write_config_yaml,
)
from app.worker import search_index
from app.worker.font_upload import FontValidationError, delete_font_file, process_font_upload

def _tabs_for(settings: Settings) -> list[tuple[str, str]]:
    # The "plex" tab slug is kept stable regardless of backend (nothing
    # outside this module needs to know it's really "whichever media server
    # is configured") — only its label changes, same convention as the
    # step-nav's step-2 label in the wizard.
    return [
        ("general", "General"),
        ("render", "Render & Subtitles"),
        ("styles", "Subtitle Styles"),
        ("audio", "Audio"),
        ("cache", "Cache & Sync"),
        ("discord", "Discord"),
        ("plex", "Jellyfin" if settings.media_server == "jellyfin" else "Plex"),
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
            "request": request, "tabs": _tabs_for(settings), "current_tab": tab,
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
                # gifsicle_timeout_seconds/max_file_size_bytes/audio_language/
                # max_concurrent_renders aren't in this form (audio_language
                # lives on its own Audio tab; max_concurrent_renders has no
                # UI yet) — preserved from the existing settings rather than
                # silently reset to RenderDefaults' own Pydantic defaults.
                gifsicle_timeout_seconds=settings.render_defaults.gifsicle_timeout_seconds,
                max_file_size_bytes=settings.render_defaults.max_file_size_bytes,
                audio_language=settings.render_defaults.audio_language,
                max_concurrent_renders=settings.render_defaults.max_concurrent_renders,
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

    # ---- Subtitle Styles ---------------------------------------------------

    def _style_preset_from_form(form) -> StylePresetConfig:
        return StylePresetConfig(
            name=str(form["name"]).strip(),
            font=str(form["font"]).strip(),
            font_size=int(form["font_size"]),
            primary_color=str(form["primary_color"]).strip(),
            outline_color=str(form["outline_color"]).strip(),
            back_color=str(form["back_color"]).strip(),
            border_style=int(form["border_style"]),
            outline=float(form["outline"]),
            shadow=float(form["shadow"]),
            bold=form.get("bold") == "on",
            uppercase=form.get("uppercase") == "on",
            margin_v=int(form["margin_v"]),
            alignment=int(form["alignment"]),
        )

    @app.get("/settings/styles", response_class=HTMLResponse)
    async def settings_styles(request: Request):
        return render_tab(request, "styles", "panel_settings_styles.html")

    @app.get("/settings/styles/new", response_class=HTMLResponse)
    async def settings_styles_new(request: Request):
        return render_tab(
            request, "styles", "panel_settings_style_edit.html",
            preset=None, original_name="",
        )

    @app.get("/settings/styles/{name}/edit", response_class=HTMLResponse)
    async def settings_styles_edit(request: Request, name: str):
        settings = settings_holder.settings
        preset = next((cfg for cfg in settings.subtitle_styles if cfg.name == name), None)
        if preset is None:
            return render_tab(request, "styles", "panel_settings_styles.html", error=f"No such style preset: '{name}'.")
        return render_tab(
            request, "styles", "panel_settings_style_edit.html",
            preset=preset, original_name=name,
        )

    @app.post("/settings/styles/save", response_class=HTMLResponse)
    async def settings_styles_save(request: Request):
        settings = settings_holder.settings
        form = await request.form()
        original_name = str(form.get("original_name", "")).strip() or None

        try:
            config = _style_preset_from_form(form)
        except (KeyError, ValueError) as exc:
            return render_tab(
                request, "styles", "panel_settings_style_edit.html",
                preset=None, original_name=original_name or "",
                error=f"Couldn't save — check your values ({exc}).",
            )

        # A rename/new-name carries the prior entry's font_path forward
        # (the uploaded font file itself is keyed by the *original* name on
        # disk, not renamed alongside — Task 3's font_file_path is a
        # detail the form never needs to know about).
        if original_name:
            prior = next((c for c in settings.subtitle_styles if c.name == original_name), None)
            if prior is not None:
                config = config.model_copy(update={"font_path": prior.font_path})

        try:
            updated = upsert_style_preset(settings, original_name, config)
        except StylePresetError as exc:
            return render_tab(
                request, "styles", "panel_settings_style_edit.html",
                preset=config, original_name=original_name or "",
                error=str(exc),
            )

        await apply(updated)
        return render_tab(request, "styles", "panel_settings_styles.html", saved=True)

    @app.post("/settings/styles/{name}/delete", response_class=HTMLResponse)
    async def settings_styles_delete(request: Request, name: str):
        settings = settings_holder.settings
        try:
            updated = delete_style_preset(settings, name)
        except StylePresetError as exc:
            return render_tab(request, "styles", "panel_settings_styles.html", error=str(exc))

        prior = next((c for c in settings.subtitle_styles if c.name == name), None)
        delete_font_file(prior.font_path if prior else None)
        await apply(updated)
        return render_tab(request, "styles", "panel_settings_styles.html", saved=True)

    @app.post("/settings/styles/{name}/font", response_class=HTMLResponse)
    async def settings_styles_upload_font(request: Request, name: str):
        settings = settings_holder.settings
        form = await request.form()
        upload = form.get("font_file")
        if upload is None or not getattr(upload, "filename", None):
            return render_tab(
                request, "styles", "panel_settings_style_edit.html",
                preset=None, original_name=name, error="Choose a font file first.",
            )

        prior = next((cfg for cfg in settings.subtitle_styles if cfg.name == name), None)
        data = await upload.read()
        try:
            result = await process_font_upload(
                data, upload.filename, name, settings.cache_dir / "fonts",
            )
        except FontValidationError as exc:
            return render_tab(
                request, "styles", "panel_settings_style_edit.html",
                preset=None, original_name=name, error=str(exc),
            )

        # A re-upload with a different extension (.ttf -> .otf) writes to a
        # differently-named file (Task 3's font_file_path includes the
        # extension) — clean up the old one so it doesn't linger orphaned.
        if prior is not None and prior.font_path and prior.font_path != result.font_path:
            delete_font_file(prior.font_path)

        warning = (
            f"This font is missing some characters — e.g. "
            f"{', '.join(result.missing_punctuation)} — they'll render in a "
            f"fallback font instead."
            if result.missing_punctuation else None
        )
        return render_tab(
            request, "styles", "panel_settings_style_edit.html",
            preset=None, original_name=name,
            uploaded_font_family=result.family, font_warning=warning,
        )

    @app.post("/settings/styles/preview", response_class=HTMLResponse)
    async def settings_styles_preview(request: Request):
        import base64

        settings = settings_holder.settings
        form = await request.form()
        try:
            config = _style_preset_from_form({**form, "name": "__preview__"})
        except (KeyError, ValueError):
            return HTMLResponse('<div class="error-banner">Fill in the style fields to preview.</div>')

        worker = WorkerClient(f"http://127.0.0.1:{settings.worker.port}")
        try:
            png_bytes = await worker.preview_style(
                {
                    "font": config.font, "font_size": config.font_size,
                    "primary_color": config.primary_color,
                    "outline_color": config.outline_color,
                    "back_color": config.back_color,
                    "border_style": config.border_style,
                    "outline": config.outline, "shadow": config.shadow,
                    "bold": config.bold, "uppercase": config.uppercase,
                    "margin_v": config.margin_v, "alignment": config.alignment,
                }
            )
        except Exception:
            return HTMLResponse('<div class="error-banner">Preview failed — is the worker running?</div>')

        encoded = base64.b64encode(png_bytes).decode("ascii")
        return HTMLResponse(f'<img src="data:image/png;base64,{encoded}" alt="Style preview">')
