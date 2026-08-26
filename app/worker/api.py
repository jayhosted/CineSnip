from __future__ import annotations

import asyncio
import os
from typing import Literal

from fastapi import FastAPI, HTTPException, Response
from pydantic import BaseModel

from app.settings import Settings, SettingsError
from app.worker.ffmpeg import ClipRenderer, RenderTimeoutError, parse_timecode
from app.worker.path_mapper import NoPathMappingError, resolve_container_path
from app.worker.plex_client import MovieNotFoundError, MovieResult, PlexClient
from app.worker.quotes import find_quote_matches
from app.worker.subprocess_utils import SubprocessTimeoutError
from app.worker.subtitle_render import STYLE_PRESETS
from app.worker.subtitles import SubtitleResult, SubtitleSource, get_subtitles


class MovieResultOut(BaseModel):
    rating_key: int
    title: str
    year: int | None
    duration_ms: int
    thumb_url: str | None
    library_name: str


class SearchResponse(BaseModel):
    results: list[MovieResultOut]


class ResolveResponse(BaseModel):
    rating_key: int
    title: str
    year: int | None
    duration_ms: int
    thumb_url: str | None
    library_name: str


class RenderRequest(BaseModel):
    rating_key: int
    timecode: str
    # Set by a quote-driven clip to the matched line's own span (end -
    # start), so the render is exactly that line rather than a fixed
    # duration. Mutually exclusive with end_timecode in practice (the bot
    # only ever sends one or the other) — duration takes priority if both
    # are somehow set.
    duration: float | None = None
    # Set for a direct timecode request with an explicit end, so the user
    # can pick a custom span instead of the fixed render_defaults.duration_seconds.
    # A raw string (not a pre-computed float) since it needs the same
    # parse_timecode() as `timecode` — kept server-side so timecode format
    # support only lives in one place.
    end_timecode: str | None = None
    # None uses render_defaults.format.
    format: Literal["gif", "mp4", "webm"] | None = None
    # None/"none" means no subtitle burn-in. A style requested on a title
    # with no usable subtitles for the clip's own window degrades to plain
    # (no burn-in) rather than erroring — echoed back via X-Clip-Style so
    # the caller can tell the difference from what it asked for.
    style: Literal["classic", "boxed", "cinematic", "meme", "original", "none"] | None = None


class SubtitleEntryOut(BaseModel):
    index: int
    start: float
    end: float
    text: str


class SubtitleDiagnosticResponse(BaseModel):
    rating_key: int
    guid: str
    source: str
    sidecar_path: str | None
    stream_index: int | None
    entry_count: int
    entries: list[SubtitleEntryOut]


class QuoteMatchOut(BaseModel):
    start: float
    end: float
    timecode: str
    text: str
    score: float
    entry_indices: list[int]
    context_before: list[str]
    context_after: list[str]


class ResolveQuoteResponse(BaseModel):
    rating_key: int
    title: str
    subtitle_source: str
    confident_score: float
    min_score: float
    matches: list[QuoteMatchOut]


def _format_display_timecode(seconds: float) -> str:
    total_seconds = int(seconds)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}"


def _resolve_container_path(movie: MovieResult, settings: Settings) -> str:
    try:
        mappings = settings.path_mappings_for(movie.library_name)
    except SettingsError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    try:
        return resolve_container_path(movie.plex_path, mappings)
    except NoPathMappingError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _to_out(movie: MovieResult) -> MovieResultOut:
    return MovieResultOut(
        rating_key=movie.rating_key,
        title=movie.title,
        year=movie.year,
        duration_ms=movie.duration_ms,
        thumb_url=movie.thumb_url,
        library_name=movie.library_name,
    )


def create_app(settings: Settings) -> FastAPI:
    app = FastAPI()
    app.state.settings = settings
    app.state.plex = PlexClient(settings)
    app.state.renderer = ClipRenderer(
        fps=settings.render_defaults.fps,
        width=settings.render_defaults.width,
        timeout_seconds=settings.render_defaults.timeout_seconds,
    )

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/search", response_model=SearchResponse)
    def search(query: str) -> SearchResponse:
        results = app.state.plex.search_movies(query)
        return SearchResponse(results=[_to_out(m) for m in results])

    async def _get_movie(rating_key: int) -> MovieResult:
        # get_movie() is a synchronous plexapi/requests call — always offload
        # it so a slow Plex response doesn't stall the whole event loop
        # (this worker has no other way to serve concurrent requests).
        try:
            return await asyncio.to_thread(app.state.plex.get_movie, rating_key)
        except MovieNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/resolve/{rating_key}", response_model=ResolveResponse)
    async def resolve(rating_key: int) -> ResolveResponse:
        movie = await _get_movie(rating_key)
        container_path = _resolve_container_path(movie, settings)

        if not os.path.exists(container_path):
            raise HTTPException(
                status_code=422,
                detail=f"File not found on disk at mapped path: {container_path}",
            )

        return ResolveResponse(
            rating_key=movie.rating_key,
            title=movie.title,
            year=movie.year,
            duration_ms=movie.duration_ms,
            thumb_url=movie.thumb_url,
            library_name=movie.library_name,
        )

    @app.post("/render")
    async def render(req: RenderRequest) -> Response:
        movie = await _get_movie(req.rating_key)
        container_path = _resolve_container_path(movie, settings)

        if not os.path.exists(container_path):
            raise HTTPException(
                status_code=422,
                detail=f"File not found on disk at mapped path: {container_path}",
            )

        try:
            start = parse_timecode(req.timecode)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        duration_s = movie.duration_ms / 1000
        if start < 0 or start >= duration_s:
            raise HTTPException(
                status_code=422,
                detail=f"Timecode {req.timecode} is outside the film's runtime.",
            )

        rd = settings.render_defaults
        if req.end_timecode is not None:
            # An explicit end is a deliberate choice the user typed — clamp
            # duration/timecode-only paths silently instead (a UX nicety),
            # but reject an explicit request outside the configured bounds
            # with a clear error rather than silently giving them something
            # shorter or longer than they asked for.
            try:
                end = parse_timecode(req.end_timecode)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            clip_duration = end - start
            if clip_duration <= 0:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"end_timecode ({req.end_timecode}) must be after "
                        f"timecode ({req.timecode})."
                    ),
                )
            if not (rd.min_duration_seconds <= clip_duration <= rd.max_duration_seconds):
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"That's a {clip_duration:.1f}s clip; clips must be "
                        f"between {rd.min_duration_seconds:.0f}s and "
                        f"{rd.max_duration_seconds:.0f}s. Pick a closer end_timecode."
                    ),
                )
        else:
            clip_duration = req.duration if req.duration is not None else rd.duration_seconds
            clip_duration = max(rd.min_duration_seconds, min(rd.max_duration_seconds, clip_duration))

        clip_format = req.format if req.format is not None else rd.format
        requested_style = req.style or "none"

        subtitle_entries = None
        style_preset = None
        if requested_style != "none":
            timeout = settings.subtitle_defaults.extraction_timeout_seconds
            try:
                subtitle_result = await get_subtitles(
                    movie,
                    container_path,
                    settings.cache_dir,
                    ffprobe_timeout=timeout,
                    ffmpeg_timeout=timeout,
                )
            except (SubprocessTimeoutError, RuntimeError) as exc:
                raise HTTPException(status_code=500, detail=str(exc)) from exc

            # A title with no usable subtitles (Section 5's documented gap)
            # degrades to a plain render rather than erroring — the user
            # still gets *a* clip, just without burn-in text.
            if subtitle_result.source is not SubtitleSource.NONE and subtitle_result.entries:
                subtitle_entries = subtitle_result.entries
                style_preset = STYLE_PRESETS[requested_style]

        resolved_style = requested_style if style_preset is not None else "none"

        try:
            clip_bytes = await app.state.renderer.render_clip(
                container_path,
                start,
                clip_duration,
                settings.scratch_dir,
                clip_format,
                subtitle_entries=subtitle_entries,
                style=style_preset,
            )
        except RenderTimeoutError as exc:
            raise HTTPException(status_code=504, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

        media_type = {
            "gif": "image/gif",
            "mp4": "video/mp4",
            "webm": "video/webm",
        }[clip_format]
        # Bot doesn't know settings.render_defaults.format/style, so it
        # can't infer what was actually used for a request that left them
        # unset, or whether a requested style got silently downgraded to
        # "none" — echo both back instead of the bot guessing/duplicating
        # config defaults client-side.
        return Response(
            content=clip_bytes,
            media_type=media_type,
            headers={"X-Clip-Format": clip_format, "X-Clip-Style": resolved_style},
        )

    async def _load_subtitles(rating_key: int) -> tuple[MovieResult, SubtitleResult]:
        movie = await _get_movie(rating_key)
        container_path = _resolve_container_path(movie, settings)

        if not os.path.exists(container_path):
            raise HTTPException(
                status_code=422,
                detail=f"File not found on disk at mapped path: {container_path}",
            )

        timeout = settings.subtitle_defaults.extraction_timeout_seconds
        try:
            result = await get_subtitles(
                movie,
                container_path,
                settings.cache_dir,
                ffprobe_timeout=timeout,
                ffmpeg_timeout=timeout,
            )
        except (SubprocessTimeoutError, RuntimeError) as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

        return movie, result

    # Diagnostic endpoint for manually inspecting the raw parsed cues for a
    # title — useful on its own for verifying extraction against the real
    # library, and as a companion to /resolve-quote for picking/verifying
    # test quotes and finding cue boundaries.
    @app.get("/subtitles/{rating_key}", response_model=SubtitleDiagnosticResponse)
    async def subtitles(rating_key: int) -> SubtitleDiagnosticResponse:
        movie, result = await _load_subtitles(rating_key)

        return SubtitleDiagnosticResponse(
            rating_key=movie.rating_key,
            guid=result.guid,
            source=result.source.value,
            sidecar_path=result.sidecar_path,
            stream_index=result.stream_index,
            entry_count=len(result.entries),
            entries=[
                SubtitleEntryOut(
                    index=e.index, start=e.start, end=e.end, text=e.text
                )
                for e in result.entries
            ],
        )

    @app.get("/resolve-quote/{rating_key}", response_model=ResolveQuoteResponse)
    async def resolve_quote(rating_key: int, quote: str) -> ResolveQuoteResponse:
        movie, result = await _load_subtitles(rating_key)

        if result.source is SubtitleSource.NONE or not result.entries:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"No usable subtitles for '{movie.title}' (no sidecar .srt "
                    "and no text subtitle stream). Quote search isn't available "
                    "for this title — use timecode instead."
                ),
            )

        qm = settings.quote_match
        matches = find_quote_matches(
            result.entries,
            quote,
            limit=qm.candidate_limit,
            min_score=qm.min_score,
            max_window_gap_seconds=qm.max_window_gap_seconds,
            context_lines=qm.context_lines,
        )

        if not matches:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"No subtitle line in '{movie.title}' resembled that quote. "
                    "Try a shorter, more distinctive phrase."
                ),
            )

        return ResolveQuoteResponse(
            rating_key=movie.rating_key,
            title=movie.title,
            subtitle_source=result.source.value,
            confident_score=qm.confident_score,
            min_score=qm.min_score,
            matches=[
                QuoteMatchOut(
                    start=m.start,
                    end=m.end,
                    timecode=_format_display_timecode(m.start),
                    text=m.text,
                    score=m.score,
                    entry_indices=list(m.entry_indices),
                    context_before=list(m.context_before),
                    context_after=list(m.context_after),
                )
                for m in matches
            ],
        )

    return app
