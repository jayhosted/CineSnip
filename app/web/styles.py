from __future__ import annotations

from typing import Awaitable, Callable

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from app.runtime import SettingsHolder
from app.settings import (
    Settings,
    StylePresetConfig,
    StylePresetError,
    delete_style_preset,
    upsert_style_preset,
    write_config_yaml,
)
from app.web.generate import _WorkerClientCache
from app.worker.font_upload import FontValidationError, delete_font_file, process_font_upload


def _style_fields_from_form(form) -> dict:
    # Shared by save (wrapped in a validated StylePresetConfig, which also
    # needs a `name`) and preview (which needs none of these validated as a
    # StylePresetConfig at all — the worker's preview_style call only reads
    # these visual fields, never a name).
    return {
        "font": str(form["font"]).strip(),
        "font_size": int(form["font_size"]),
        "primary_color": str(form["primary_color"]).strip(),
        "outline_color": str(form["outline_color"]).strip(),
        "back_color": str(form["back_color"]).strip(),
        "border_style": int(form["border_style"]),
        "outline": float(form["outline"]),
        "shadow": float(form["shadow"]),
        "bold": form.get("bold") == "on",
        "uppercase": form.get("uppercase") == "on",
        "margin_v": int(form["margin_v"]),
        "alignment": int(form["alignment"]),
    }


def _style_preset_from_form(form) -> StylePresetConfig:
    return StylePresetConfig(name=str(form["name"]).strip(), **_style_fields_from_form(form))


def register_styles_routes(
    app: FastAPI,
    templates: Jinja2Templates,
    settings_holder: SettingsHolder,
    on_setup_complete: Callable[[], Awaitable[None]],
) -> None:
    # Its own top-level sidebar item, not a Settings tab — editing/adding
    # subtitle style presets (including uploading a font) is content
    # management for an evolving list, not a one-time configuration knob,
    # so it gets the same standalone treatment as /generate rather than
    # living under /settings. A future fallback-font *setting* (if ever
    # added) would belong under /settings instead — this page is for the
    # presets themselves.
    client_cache = _WorkerClientCache()

    def render_page(request: Request, panel: str, **ctx) -> HTMLResponse:
        settings = settings_holder.settings
        context = {"request": request, "settings": settings, **ctx}
        if request.headers.get("HX-Request"):
            return HTMLResponse(templates.env.get_template(panel).render(context))
        context["content_template"] = panel
        return templates.TemplateResponse(
            request, "shell.html",
            {**context, "page_title": "Subtitle Styles", "current_page": "styles"},
        )

    async def apply(new_settings: Settings) -> None:
        write_config_yaml(new_settings)
        await on_setup_complete()

    async def _preview_background_context() -> dict:
        # Picked once per edit-page load, not per debounced preview request
        # (that would mean a fresh Plex/ffmpeg round trip on every
        # keystroke) — the resulting background_id is carried in a hidden
        # form field and reused for every /styles/preview call during this
        # editing session. None/None (silent, no error banner) is a normal
        # outcome, not a failure: an empty library cache or a worker that
        # isn't up yet just means the flat-color background is used instead.
        worker = client_cache.get(settings_holder)
        if worker is None:
            return {"background_id": None, "background_title": None}
        try:
            result = await worker.preview_background()
        except Exception:
            return {"background_id": None, "background_title": None}
        if result is None:
            return {"background_id": None, "background_title": None}
        return {"background_id": result["background_id"], "background_title": result["title"]}

    @app.get("/styles/preview-background-fragment", response_class=HTMLResponse)
    async def styles_preview_background_fragment(request: Request):
        # The shuffle button's own target — always an htmx partial swap of
        # #preview-block, never a full page, so it renders the block
        # template directly rather than going through render_page.
        background = await _preview_background_context()
        template = templates.env.get_template("panel_style_preview_block.html")
        return HTMLResponse(template.render(background))

    @app.get("/styles", response_class=HTMLResponse)
    async def styles_index(request: Request):
        return render_page(request, "panel_styles.html")

    @app.get("/styles/new", response_class=HTMLResponse)
    async def styles_new(request: Request):
        background = await _preview_background_context()
        return render_page(request, "panel_style_edit.html", preset=None, original_name="", **background)

    @app.get("/styles/{name}/edit", response_class=HTMLResponse)
    async def styles_edit(request: Request, name: str):
        settings = settings_holder.settings
        preset = next((cfg for cfg in settings.subtitle_styles if cfg.name == name), None)
        if preset is None:
            return render_page(request, "panel_styles.html", error=f"No such style preset: '{name}'.")
        background = await _preview_background_context()
        return render_page(request, "panel_style_edit.html", preset=preset, original_name=name, **background)

    @app.post("/styles/save", response_class=HTMLResponse)
    async def styles_save(request: Request):
        settings = settings_holder.settings
        form = await request.form()
        original_name = str(form.get("original_name", "")).strip() or None

        try:
            config = _style_preset_from_form(form)
        except ValidationError as exc:
            # pydantic's own str(exc) is a multi-line, developer-facing
            # dump ("1 validation error for StylePresetConfig\nname\n  Value
            # error, ...") — surface just the human-written message(s) from
            # our own field_validators instead.
            messages = "; ".join(err["msg"].removeprefix("Value error, ") for err in exc.errors())
            return render_page(
                request, "panel_style_edit.html",
                preset=None, original_name=original_name or "",
                error=f"Couldn't save — {messages}",
            )
        except (KeyError, ValueError) as exc:
            return render_page(
                request, "panel_style_edit.html",
                preset=None, original_name=original_name or "",
                error=f"Couldn't save — check your values ({exc}).",
            )

        # A rename/new-name carries the prior entry's font_path forward (the
        # uploaded font file itself is keyed by the *original* name on disk,
        # not renamed alongside — font_file_path is a detail the form never
        # needs to know about).
        if original_name:
            prior = next((c for c in settings.subtitle_styles if c.name == original_name), None)
            if prior is not None:
                config = config.model_copy(update={"font_path": prior.font_path})

        try:
            updated = upsert_style_preset(settings, original_name, config)
        except StylePresetError as exc:
            return render_page(
                request, "panel_style_edit.html",
                preset=config, original_name=original_name or "",
                error=str(exc),
            )

        await apply(updated)
        return render_page(request, "panel_styles.html", saved=True)

    @app.post("/styles/{name}/delete", response_class=HTMLResponse)
    async def styles_delete(request: Request, name: str):
        settings = settings_holder.settings
        try:
            updated = delete_style_preset(settings, name)
        except StylePresetError as exc:
            return render_page(request, "panel_styles.html", error=str(exc))

        prior = next((c for c in settings.subtitle_styles if c.name == name), None)
        delete_font_file(prior.font_path if prior else None)
        await apply(updated)
        return render_page(request, "panel_styles.html", saved=True)

    @app.post("/styles/{name}/font", response_class=HTMLResponse)
    async def styles_upload_font(request: Request, name: str):
        settings = settings_holder.settings
        prior = next((cfg for cfg in settings.subtitle_styles if cfg.name == name), None)
        if prior is None:
            # /styles/new has no original_name yet — the template hides the
            # upload control in that case (a not-yet-saved preset must be
            # Saved first), so reaching here with no matching preset is
            # itself the error to report.
            return render_page(
                request, "panel_style_edit.html",
                preset=None, original_name=name,
                error="Save the preset before uploading a custom font.",
            )

        form = await request.form()
        upload = form.get("font_file")
        if upload is None or not getattr(upload, "filename", None):
            return render_page(
                request, "panel_style_edit.html",
                preset=prior, original_name=name, error="Choose a font file first.",
            )

        data = await upload.read()
        try:
            result = await process_font_upload(
                data, upload.filename, name, settings.cache_dir / "fonts",
            )
        except FontValidationError as exc:
            return render_page(
                request, "panel_style_edit.html",
                preset=prior, original_name=name, error=str(exc),
            )

        # A re-upload with a different extension (.ttf -> .otf) writes to a
        # differently-named file (font_file_path includes the extension) —
        # clean up the old one so it doesn't linger orphaned.
        if prior.font_path and prior.font_path != result.font_path:
            delete_font_file(prior.font_path)

        # Persist the upload onto the preset itself — font_path is a
        # write-only field until this assignment happens.
        updated_config = prior.model_copy(update={"font": result.family, "font_path": result.font_path})
        updated_settings = upsert_style_preset(settings, original_name=name, config=updated_config)
        await apply(updated_settings)

        warning = (
            f"This font is missing some characters — e.g. "
            f"{', '.join(result.missing_punctuation)} — they'll render in a "
            f"fallback font instead."
            if result.missing_punctuation else None
        )
        return render_page(
            request, "panel_style_edit.html",
            preset=updated_config, original_name=name, font_warning=warning,
        )

    @app.post("/styles/preview", response_class=HTMLResponse)
    async def styles_preview(request: Request):
        import base64

        form = await request.form()
        try:
            style_fields = _style_fields_from_form(form)
        except (KeyError, ValueError):
            return HTMLResponse('<div class="error-banner">Fill in the style fields to preview.</div>')

        background_id = str(form.get("background_id", "")).strip()
        if background_id:
            style_fields["background_id"] = background_id

        worker = client_cache.get(settings_holder)
        if worker is None:
            return HTMLResponse('<div class="error-banner">Preview failed — is the worker running?</div>')
        try:
            png_bytes = await worker.preview_style(style_fields)
        except Exception:
            return HTMLResponse('<div class="error-banner">Preview failed — is the worker running?</div>')

        encoded = base64.b64encode(png_bytes).decode("ascii")
        return HTMLResponse(f'<img src="data:image/png;base64,{encoded}" alt="Style preview">')
