from __future__ import annotations

import base64

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.bot.worker_client import RenderResult, WorkerClient
from app.runtime import SettingsHolder

# Mirrors app/bot/cogs/gif.py's _STYLE_OPTIONS exactly (same worker `style`
# values, same catalog, same order per CLAUDE.md Section 2) — the web app is
# a second thin client of the same worker API (decision #3), not a
# reimplementation, so this list must never drift from the bot's.
_STYLE_OPTIONS: list[tuple[str, str]] = [
    ("classic", "Classic (white, black outline)"),
    ("boxed", "Boxed (white on black box)"),
    ("cinematic", "Cinematic (yellow)"),
    ("meme", "Meme (bold caps)"),
    ("original", "Original (mirrors source style)"),
    ("none", "No Subtitles"),
]
_STYLE_LABELS = {value: label.split(" (")[0] for value, label in _STYLE_OPTIONS}

_MEDIA_TYPES = {"gif": "image/gif", "mp4": "video/mp4", "webm": "video/webm"}


def _style_label(value: str) -> str:
    return _STYLE_LABELS.get(value, value.title())


def _size_label(num_bytes: int) -> str:
    if num_bytes >= 1024 * 1024:
        return f"{num_bytes / (1024 * 1024):.1f} MB"
    return f"{num_bytes / 1024:.0f} KB"


async def _subtitle_slow_warning(worker: WorkerClient, rating_key: int) -> str:
    # Mirrors app/bot/cogs/gif.py's _slow_subtitle_warning — a cheap
    # (no ffmpeg) best-effort hint that a quote search is about to fall
    # through to a cold embedded-subtitle extraction, which can take
    # several minutes on a large file. Kept as this module's own copy
    # rather than importing the bot-package helper directly, same reasoning
    # as _STYLE_OPTIONS above: the web app is a thin second client of the
    # worker API, not a place to reach into the bot package's internals.
    try:
        status = await worker.subtitle_status(rating_key)
    except httpx.HTTPError:
        return ""
    if not status.likely_slow:
        return ""
    return (
        "First time reading this title's subtitles from the video file itself — "
        "can take a few minutes for a large file. Future searches for it will be instant."
    )


def _no_subtitles_note(requested_style: str, resolved_style: str) -> str:
    # Mirrors app/bot/cogs/gif.py's _no_subtitles_note exactly — the worker
    # silently degrades a styled request to no-burn-in when a title has no
    # usable subtitles for the clip's own window (CLAUDE.md Section 7), and
    # every client needs to surface that rather than let it pass unnoticed.
    if requested_style != "none" and resolved_style == "none":
        return "No subtitles available for this title — generated without burn-in."
    return ""


def _error_detail(exc: httpx.HTTPError) -> str:
    try:
        return exc.response.json().get("detail", exc.response.text)
    except Exception:
        return str(exc) or "the worker didn't respond in time"


def _error_html(exc: httpx.HTTPError) -> HTMLResponse:
    return HTMLResponse(f'<p class="error-banner">{_error_detail(exc)}</p>')


def _left_error_html(message: str) -> HTMLResponse:
    # Any /generate/search|select|reset response must keep the
    # #generate-left wrapper (htmx targets it with hx-swap="outerHTML") —
    # a bare error fragment without that id would remove the target
    # element from the DOM entirely and break every subsequent interaction.
    return HTMLResponse(f'<div id="generate-left" class="col-left"><div class="card"><p class="error-banner" style="margin:0;">{message}</p></div></div>')


class _WorkerClientCache:
    # A single long-lived httpx connection pool (same reasoning as
    # app.bot.client.CineSnipBot's own WorkerClient), rebuilt only if
    # settings.worker.port ever actually changes across a reconfiguration.
    def __init__(self) -> None:
        self._client: WorkerClient | None = None
        self._port: int | None = None

    def get(self, settings_holder: SettingsHolder) -> WorkerClient | None:
        settings = settings_holder.settings
        if settings is None:
            return None
        if self._client is None or self._port != settings.worker.port:
            self._client = WorkerClient(f"http://127.0.0.1:{settings.worker.port}")
            self._port = settings.worker.port
        return self._client


def register_generate_routes(
    app: FastAPI, templates: Jinja2Templates, settings_holder: SettingsHolder
) -> None:
    client_cache = _WorkerClientCache()

    def fragment(panel: str, **ctx) -> HTMLResponse:
        template = templates.env.get_template(panel)
        return HTMLResponse(template.render(ctx))

    async def do_render(
        worker: WorkerClient,
        rating_key: int,
        timecode: str,
        duration: float | None,
        end_timecode: str | None,
        format: str | None,
        style: str,
        *,
        title: str,
        display_timecode: str,
        caption: str | None,
    ) -> HTMLResponse:
        try:
            result: RenderResult = await worker.render(
                rating_key, timecode, duration=duration, end_timecode=end_timecode,
                format=format, style=style,
            )
        except httpx.HTTPError as exc:
            return _error_html(exc)
        content_b64 = base64.b64encode(result.content).decode("ascii")
        return fragment(
            "panel_generate_result.html",
            render=result,
            content_b64=content_b64,
            media_type=_MEDIA_TYPES[result.format],
            size_label=_size_label(len(result.content)),
            title=title,
            display_timecode=display_timecode,
            caption=caption,
            style_label=_style_label(result.style),
            no_subtitles_note=_no_subtitles_note(style, result.style),
        )

    @app.get("/generate", response_class=HTMLResponse)
    async def generate_index(request: Request):
        if settings_holder.settings is None:
            # Setup hasn't completed yet — nothing here can work without a
            # running worker. Send them into the wizard instead.
            return RedirectResponse("/")
        return templates.TemplateResponse(
            request,
            "shell.html",
            {
                "request": request,
                "content_template": "panel_generate.html",
                "page_title": "Generate",
                "current_page": "generate",
                "style_options": _STYLE_OPTIONS,
            },
        )

    @app.get("/generate/search", response_class=HTMLResponse)
    async def generate_search(request: Request, query: str = "", kind: str = "film"):
        worker = client_cache.get(settings_holder)
        results = []
        if worker is not None and query.strip():
            try:
                results = await (worker.search_shows(query) if kind == "show" else worker.search(query))
            except httpx.HTTPError:
                return _left_error_html("Search failed — is the worker running?")
        return fragment(
            "panel_generate_left.html",
            kind=kind, query=query, results=results[:25], selected=None,
            style_options=_STYLE_OPTIONS,
        )

    @app.get("/generate/select", response_class=HTMLResponse)
    async def generate_select(
        request: Request,
        rating_key: int,
        kind: str = "film",
        title: str = "",
        year: str = "",
        library_name: str = "",
    ):
        # Reuses the title/year/library the search results already carried
        # rather than re-resolving via the worker — a show has no media
        # file of its own to resolve (only its episodes do), and
        # re-fetching what's already in hand is exactly the redundant-
        # Plex-fetch class of bug CLAUDE.md's Section 3 fix already covers
        # for the bot side.
        selected = {"rating_key": rating_key, "title": title, "year": int(year) if year else None, "library_name": library_name}
        return fragment(
            "panel_generate_left.html",
            kind=kind, query=None, results=None, selected=selected,
            style_options=_STYLE_OPTIONS,
        )

    @app.get("/generate/reset", response_class=HTMLResponse)
    async def generate_reset(request: Request, kind: str = "film"):
        return fragment(
            "panel_generate_left.html",
            kind=kind, query="", results=None, selected=None,
            style_options=_STYLE_OPTIONS,
        )

    @app.post("/generate/render", response_class=HTMLResponse)
    async def generate_render(request: Request):
        worker = client_cache.get(settings_holder)
        if worker is None:
            return HTMLResponse("")
        form = await request.form()
        rating_key = int(form["rating_key"])
        title = str(form.get("title", ""))
        timecode = str(form["timecode"])
        display_timecode = str(form.get("display_timecode", "")) or timecode
        duration = float(form["duration"]) if form.get("duration") else None
        format = str(form["format"]) if form.get("format") else None
        style = str(form.get("style") or "none")
        caption = str(form.get("caption", "")) or None
        return await do_render(
            worker, rating_key, timecode, duration, None, format, style,
            title=title, display_timecode=display_timecode, caption=caption,
        )

    @app.post("/generate/render-start", response_class=HTMLResponse)
    async def generate_render_start(request: Request):
        # Mirrors GifCog._generate's call order in app/bot/cogs/gif.py (film
        # branch) plus GifCog.snip_tv's episode-resolve/whole-show-search
        # branches — same worker endpoints, same order, just rendered as
        # htmx fragments instead of Discord views. Unlike the bot, the
        # style is whatever the user picked in the pill grid (default
        # "classic") rather than a zero-click default forced per branch —
        # this UI puts the choice in front of the user up front, so honor it.
        worker = client_cache.get(settings_holder)
        if worker is None:
            return HTMLResponse("")
        form = await request.form()

        kind = str(form.get("kind", "film"))
        rating_key_posted = int(form["rating_key"])
        title = str(form.get("title", ""))
        library_name = str(form.get("library_name", "")) or None
        quote = str(form.get("quote", "")).strip() or None
        timecode = str(form.get("timecode", "")).strip() or None
        end_timecode = str(form.get("end_timecode", "")).strip() or None
        format = str(form.get("format", "")).strip() or None
        style = str(form.get("style") or "none")
        season_raw = str(form.get("season", "")).strip()
        episode_raw = str(form.get("episode", "")).strip()
        season = int(season_raw) if season_raw else None
        episode = int(episode_raw) if episode_raw else None

        if not quote and not timecode:
            return HTMLResponse('<p class="error-banner">Enter a quote or a timecode.</p>')
        if end_timecode and not timecode:
            return HTMLResponse('<p class="error-banner">End timecode needs a timecode to start from.</p>')

        if kind == "show":
            show_rating_key = rating_key_posted
            if (season is None) != (episode is None):
                return HTMLResponse('<p class="error-banner">Give both season and episode, or neither.</p>')
            if timecode and season is None:
                return HTMLResponse(
                    '<p class="error-banner">A timecode needs a specific episode — give season and '
                    "episode, or use a quote to search the whole show.</p>"
                )

            if season is not None:
                try:
                    resolved = await worker.resolve_episode(show_rating_key, season, episode)
                except httpx.HTTPError as exc:
                    return _error_html(exc)
                rating_key = resolved.rating_key
                title = resolved.title
                library_name = resolved.library_name
            else:
                # No episode given — quote is guaranteed at this point, same
                # as GifCog.snip_tv's own validation order.
                try:
                    result = await worker.search_episodes_quote(show_rating_key, quote)
                except httpx.HTTPError as exc:
                    return _error_html(exc)
                if not result.matches:
                    return HTMLResponse('<p class="error-banner">No matching line found in that show.</p>')
                return fragment(
                    "panel_generate_matches.html",
                    matches=result.matches, show_titles=True, rating_key=None,
                    title=None, library_name=None, format=format, style=style,
                )
        else:
            rating_key = rating_key_posted

        if quote and not form.get("_slow_ack"):
            # Cheap upfront check (no ffmpeg) before the potentially
            # multi-minute cold-extraction call below — if it's likely to
            # be slow, hand back an interim "searching…" fragment that
            # immediately re-posts itself (hx-trigger="load") with
            # _slow_ack set, so the user sees a real status message instead
            # of a spinner-less button that looks dead. Mirrors the
            # Discord bot's own _slow_subtitle_warning, reached here via
            # htmx's load-triggered self-repost instead of message editing.
            warning = await _subtitle_slow_warning(worker, rating_key)
            if warning:
                hidden_fields = {k: str(v) for k, v in form.items()}
                hidden_fields["_slow_ack"] = "1"
                return fragment(
                    "panel_generate_loading.html",
                    message=warning,
                    continue_url="/generate/render-start",
                    hidden_fields=hidden_fields,
                )

        if quote:
            try:
                resolved_quote = await worker.resolve_quote(rating_key, quote)
            except httpx.HTTPError as exc:
                return _error_html(exc)
            top = resolved_quote.matches[0]
            if len(resolved_quote.matches) == 1 and top.score >= resolved_quote.confident_score:
                # A single confident match: same "no separate confirm step"
                # reasoning as decision #4 — render it directly.
                return await do_render(
                    worker, rating_key, str(top.start), top.end - top.start, None, format, style,
                    title=resolved_quote.title, display_timecode=top.timecode, caption=top.text,
                )
            return fragment(
                "panel_generate_matches.html",
                matches=resolved_quote.matches, show_titles=False, rating_key=rating_key,
                title=resolved_quote.title, library_name=library_name, format=format, style=style,
            )

        # Bare timecode: whatever style the user picked in the form.
        return await do_render(
            worker, rating_key, timecode, None, end_timecode, format, style,
            title=title, display_timecode=timecode, caption=None,
        )
