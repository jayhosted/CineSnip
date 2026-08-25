from __future__ import annotations

import os

from fastapi import FastAPI, HTTPException, Response
from pydantic import BaseModel

from app.settings import Settings
from app.worker.ffmpeg import ClipRenderer, RenderTimeoutError, parse_timecode
from app.worker.path_mapper import NoPathMappingError, resolve_container_path
from app.worker.plex_client import MovieResult, PlexClient
from app.worker.subprocess_utils import SubprocessTimeoutError
from app.worker.subtitles import get_subtitles


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

    @app.get("/resolve/{rating_key}", response_model=ResolveResponse)
    def resolve(rating_key: int) -> ResolveResponse:
        movie = app.state.plex.get_movie(rating_key)
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
        movie = app.state.plex.get_movie(req.rating_key)
        try:
            container_path = resolve_container_path(
                movie.plex_path, settings.path_mappings
            )
        except NoPathMappingError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

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

    # Diagnostic-only endpoint for manually verifying subtitle extraction
    # against the real library. Not wired into the Discord bot — a proper
    # /resolve-quote endpoint with fuzzy matching is a follow-up slice.
    @app.get("/subtitles/{rating_key}", response_model=SubtitleDiagnosticResponse)
    async def subtitles(rating_key: int) -> SubtitleDiagnosticResponse:
        movie = app.state.plex.get_movie(rating_key)
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

    return app
