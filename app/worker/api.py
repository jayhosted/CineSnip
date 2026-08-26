from __future__ import annotations

import asyncio
import os

from fastapi import FastAPI, HTTPException, Response
from pydantic import BaseModel

from app.settings import Settings
from app.worker.ffmpeg import ClipRenderer, RenderTimeoutError, parse_timecode
from app.worker.path_mapper import NoPathMappingError, resolve_container_path
from app.worker.plex_client import MovieNotFoundError, MovieResult, PlexClient
from app.worker.quotes import find_quote_matches
from app.worker.subprocess_utils import SubprocessTimeoutError
from app.worker.subtitles import SubtitleResult, SubtitleSource, get_subtitles


class MovieResultOut(BaseModel):
    rating_key: int
    title: str
    year: int | None
    duration_ms: int
    thumb_url: str | None


class SearchResponse(BaseModel):
    results: list[MovieResultOut]


class ResolveResponse(BaseModel):
    rating_key: int
    title: str
    year: int | None
    duration_ms: int
    thumb_url: str | None


class RenderRequest(BaseModel):
    rating_key: int
    timecode: str


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


def _to_out(movie: MovieResult) -> MovieResultOut:
    return MovieResultOut(
        rating_key=movie.rating_key,
        title=movie.title,
        year=movie.year,
        duration_ms=movie.duration_ms,
        thumb_url=movie.thumb_url,
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
        try:
            container_path = resolve_container_path(
                movie.plex_path, settings.path_mappings
            )
        except NoPathMappingError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

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
        )

    @app.post("/render")
    async def render(req: RenderRequest) -> Response:
        movie = await _get_movie(req.rating_key)
        try:
            container_path = resolve_container_path(
                movie.plex_path, settings.path_mappings
            )
        except NoPathMappingError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

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

        clip_duration = settings.render_defaults.duration_seconds

        try:
            gif_bytes = await app.state.renderer.render_gif(
                container_path, start, clip_duration, settings.scratch_dir
            )
        except RenderTimeoutError as exc:
            raise HTTPException(status_code=504, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

        return Response(content=gif_bytes, media_type="image/gif")

    async def _load_subtitles(rating_key: int) -> tuple[MovieResult, SubtitleResult]:
        movie = await _get_movie(rating_key)
        try:
            container_path = resolve_container_path(
                movie.plex_path, settings.path_mappings
            )
        except NoPathMappingError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

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
