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

_MEDIA_TYPES = {"gif": "image/gif", "mp4": "video/mp4", "webm": "video/webm"}


def _error_detail(exc: httpx.HTTPError) -> str:
    try:
        return exc.response.json().get("detail", exc.response.text)
    except Exception:
        return str(exc) or "the worker didn't respond in time"


def _error_html(exc: httpx.HTTPError) -> HTMLResponse:
    return HTMLResponse(f'<p class="error-banner">{_error_detail(exc)}</p>')


class _WorkerClientCache:
    # A single long-lived httpx connection pool (same reasoning as
    # app.bot.client.CineSnipBot's own WorkerClient), rebuilt only if
    # settings.worker.port ever actually changes across a reconfiguration —
    # which the wizard doesn't currently let a user edit, but this stays
    # correct if that ever changes.
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
            rating_key=rating_key,
            timecode=timecode,
            duration=duration,
            end_timecode=end_timecode,
            format=format,
            style_options=_STYLE_OPTIONS,
        )

    @app.get("/generate", response_class=HTMLResponse)
    async def generate_index(request: Request):
        if settings_holder.settings is None:
            # Setup hasn't completed yet — nothing here can work without a
            # running worker. Send them into the wizard instead.
            return RedirectResponse("/")
        return templates.TemplateResponse(
            request,
            "page.html",
            {
                "request": request,
                "panel_template": "panel_generate.html",
                "page_subtitle": "Generate",
                "show_step_nav": False,
            },
        )

    @app.get("/generate/search", response_class=HTMLResponse)
    async def generate_search(request: Request, query: str = "", kind: str = "film"):
        worker = client_cache.get(settings_holder)
        if worker is None or not query.strip():
            return HTMLResponse("")
        try:
            results = await (worker.search_shows(query) if kind == "show" else worker.search(query))
        except httpx.HTTPError:
            return HTMLResponse('<p class="error-banner">Search failed — is the worker running?</p>')
        return fragment("panel_generate_results.html", results=results[:25], kind=kind)

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
        # (panel_generate_results.html) rather than re-resolving via the
        # worker — a show has no media file of its own to resolve (only its
        # episodes do), and re-fetching what's already in hand is exactly
        # the redundant-Plex-fetch class of bug CLAUDE.md's Section 3 fix
        # already covers for the bot side.
        resolved = {"title": title, "year": int(year) if year else None, "library_name": library_name}
        return fragment(
            "panel_generate_options.html", resolved=resolved, rating_key=rating_key, kind=kind
        )

    @app.post("/generate/render", response_class=HTMLResponse)
    async def generate_render(request: Request):
        worker = client_cache.get(settings_holder)
        if worker is None:
            return HTMLResponse("")
        form = await request.form()
        rating_key = int(form["rating_key"])
        timecode = str(form["timecode"])
        duration = float(form["duration"]) if form.get("duration") else None
        end_timecode = str(form["end_timecode"]) if form.get("end_timecode") else None
        format = str(form["format"]) if form.get("format") else None
        style = str(form.get("style") or "none")
        return await do_render(worker, rating_key, timecode, duration, end_timecode, format, style)

    @app.post("/generate/render-start", response_class=HTMLResponse)
    async def generate_render_start(request: Request):
        # Mirrors GifCog._generate's call order in app/bot/cogs/gif.py (film
        # branch) plus GifCog.snip_tv's episode-resolve/whole-show-search
        # branches — same worker endpoints, same order, just rendered as
        # htmx fragments instead of Discord views.
        worker = client_cache.get(settings_holder)
        if worker is None:
            return HTMLResponse("")
        form = await request.form()

        kind = str(form.get("kind", "film"))
        quote = str(form.get("quote", "")).strip() or None
        timecode = str(form.get("timecode", "")).strip() or None
        end_timecode = str(form.get("end_timecode", "")).strip() or None
        format = str(form.get("format", "")).strip() or None
        season_raw = str(form.get("season", "")).strip()
        episode_raw = str(form.get("episode", "")).strip()
        season = int(season_raw) if season_raw else None
        episode = int(episode_raw) if episode_raw else None

        if not quote and not timecode:
            return HTMLResponse('<p class="error-banner">Enter a quote or a timecode.</p>')
        if end_timecode and not timecode:
            return HTMLResponse('<p class="error-banner">End timecode needs a timecode to start from.</p>')

        if kind == "show":
            show_rating_key = int(form["rating_key"])
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
                    matches=result.matches,
                    show_titles=True,
                    rating_key=None,
                    format=format,
                )
        else:
            rating_key = int(form["rating_key"])

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
                    worker, rating_key, str(top.start), top.end - top.start, None, format, "classic"
                )
            return fragment(
                "panel_generate_matches.html",
                matches=resolved_quote.matches,
                show_titles=False,
                rating_key=rating_key,
                format=format,
            )

        # Bare timecode: no known subtitle availability, default burn-in off
        # (same as GifCog._generate's timecode branch).
        return await do_render(worker, rating_key, timecode, None, end_timecode, format, "none")
