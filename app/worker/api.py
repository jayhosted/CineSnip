from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Response
from pydantic import BaseModel

from app.settings import Settings, SettingsError
from app.worker import quote_index
from app.worker.ffmpeg import ClipRenderer, RenderTimeoutError, parse_timecode
from app.worker.library_search import search_cached_library
from app.worker.path_mapper import NoPathMappingError, resolve_container_path
from app.worker.plex_client import (
    EpisodeNotFoundError,
    MovieNotFoundError,
    MovieResult,
    PlexClient,
    ShowNotFoundError,
)
from app.worker.quote_index import CachedTitle
from app.worker.quotes import find_quote_matches, get_or_build_candidates
from app.worker.subprocess_utils import SubprocessTimeoutError
from app.worker.subtitle_render import STYLE_PRESETS
from app.worker.subtitles import (
    SubtitleResult,
    SubtitleSource,
    find_sidecar_subtitle,
    get_subtitles,
    read_cached_subtitles,
)

logger = logging.getLogger(__name__)


class MovieResultOut(BaseModel):
    rating_key: int
    title: str
    year: int | None
    duration_ms: int
    thumb_url: str | None
    library_name: str


class SearchResponse(BaseModel):
    results: list[MovieResultOut]


class SubtitleStatusResponse(BaseModel):
    rating_key: int
    # True when neither a sidecar file nor a cached result exists for this
    # title, meaning a subtitle-needing request (quote search, or a styled
    # render) is about to fall through to a cold embedded-stream extraction
    # — which has no fast-seek and can take minutes on a large file (Section
    # 5/6 in CLAUDE.md). A cheap, ffmpeg-free hint so the bot can warn the
    # user *before* committing to that wait, not a guarantee: the real
    # request still does its own proper freshness-checked cache read and can
    # legitimately reach a different outcome (e.g. the file changed).
    likely_slow: bool


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


class LibraryQuoteMatchOut(BaseModel):
    rating_key: int
    title: str
    library_name: str
    start: float
    end: float
    timecode: str
    text: str
    score: float
    context_before: list[str]
    context_after: list[str]


class LibrarySearchResponse(BaseModel):
    matches: list[LibraryQuoteMatchOut]
    confident_score: float
    min_score: float


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


def _index_if_searchable(settings: Settings, movie: MovieResult, result: SubtitleResult) -> None:
    # Every title actually checked gets *some* record — a searchable
    # result goes into cached_titles (with its source type, for the
    # dashboard's coverage stats), a NONE result goes into
    # no_subtitle_titles instead (Section 5's documented gap: not
    # searchable, but still worth knowing "this was checked").
    if result.source is not SubtitleSource.NONE and result.entries:
        quote_index.upsert_cached_title(
            settings.quote_index_db_path,
            result.guid,
            movie.rating_key,
            movie.title,
            movie.library_name,
            result.source.value,
        )
    elif result.source is SubtitleSource.NONE:
        quote_index.upsert_no_subtitle_title(
            settings.quote_index_db_path,
            result.guid,
            movie.rating_key,
            movie.title,
            movie.library_name,
        )


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

    @app.get("/search-shows", response_model=SearchResponse)
    def search_shows(query: str) -> SearchResponse:
        results = app.state.plex.search_shows(query)
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

    @app.get("/subtitle-status/{rating_key}", response_model=SubtitleStatusResponse)
    async def subtitle_status(rating_key: int) -> SubtitleStatusResponse:
        movie = await _get_movie(rating_key)
        try:
            container_path = _resolve_container_path(movie, settings)
        except HTTPException:
            # Best-effort hint endpoint — a path-mapping problem is the real
            # request's job to report clearly, not this one's.
            return SubtitleStatusResponse(rating_key=rating_key, likely_slow=False)

        sidecar = find_sidecar_subtitle(Path(container_path))
        cached = read_cached_subtitles(settings.cache_dir, movie.guid)
        likely_slow = sidecar is None and cached is None
        return SubtitleStatusResponse(rating_key=rating_key, likely_slow=likely_slow)

    @app.get("/resolve-episode/{show_rating_key}", response_model=ResolveResponse)
    async def resolve_episode(show_rating_key: int, season: int, episode: int) -> ResolveResponse:
        # Turns show+season+episode into a concrete rating_key + display
        # info — everything downstream (/render, /resolve-quote, etc.) is
        # the unmodified movie flow from here, since it's already generic
        # over any rating_key.
        try:
            ep = await asyncio.to_thread(
                app.state.plex.get_episode, show_rating_key, season, episode
            )
        except EpisodeNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        container_path = _resolve_container_path(ep, settings)
        if not os.path.exists(container_path):
            raise HTTPException(
                status_code=422,
                detail=f"File not found on disk at mapped path: {container_path}",
            )

        return ResolveResponse(
            rating_key=ep.rating_key,
            title=ep.title,
            year=ep.year,
            duration_ms=ep.duration_ms,
            thumb_url=ep.thumb_url,
            library_name=ep.library_name,
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

            _index_if_searchable(settings, movie, subtitle_result)

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
                three_d_format=settings.three_d_format_for(movie.library_name),
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

        _index_if_searchable(settings, movie, result)

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
        precomputed = get_or_build_candidates(
            settings.cache_dir, result.guid, result.entries, qm.max_window_gap_seconds
        )
        matches = find_quote_matches(
            result.entries,
            quote,
            limit=qm.candidate_limit,
            min_score=qm.min_score,
            max_window_gap_seconds=qm.max_window_gap_seconds,
            context_lines=qm.context_lines,
            precomputed=precomputed,
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

    # Library-wide search (CLAUDE.md Section 5's /snip-search, Tier 1
    # only): searches the subtitle cache via the quote_index, never the live
    # filesystem/Plex — so it's fast regardless of library size, but its
    # scope is exactly "titles CineSnip has already read via any flow". An
    # empty index/no matches is a normal outcome, not an error.
    @app.get("/search-quote", response_model=LibrarySearchResponse)
    async def search_quote(quote: str) -> LibrarySearchResponse:
        # The index also holds TV episodes (indexed via the same generic
        # /render and /resolve-quote endpoints /snip-tv uses) — filter back
        # down to movie libraries, since /search-quote is documented and
        # surfaced to users as "every film", not the whole index verbatim.
        movie_library_names = app.state.plex.movie_library_names
        cached_titles = [
            t
            for t in quote_index.list_cached_titles(settings.quote_index_db_path)
            if t.library_name in movie_library_names
        ]

        qm = settings.quote_match
        matches = search_cached_library(
            settings.cache_dir,
            cached_titles,
            quote,
            result_limit=qm.candidate_limit,
            min_score=qm.min_score,
            max_window_gap_seconds=qm.max_window_gap_seconds,
            context_lines=qm.context_lines,
            per_title_limit=qm.library_per_title_limit,
        )

        return LibrarySearchResponse(
            matches=[
                LibraryQuoteMatchOut(
                    rating_key=m.rating_key,
                    title=m.title,
                    library_name=m.library_name,
                    start=m.match.start,
                    end=m.match.end,
                    timecode=_format_display_timecode(m.match.start),
                    text=m.match.text,
                    score=m.match.score,
                    context_before=list(m.match.context_before),
                    context_after=list(m.match.context_after),
                )
                for m in matches
            ],
            confident_score=qm.confident_score,
            min_score=qm.min_score,
        )

    async def _ensure_episode_cached(episode: MovieResult) -> None:
        # Best-effort: one broken/unmapped episode file must not fail the
        # whole show-wide search, so failures here are logged and skipped
        # rather than raised.
        try:
            container_path = _resolve_container_path(episode, settings)
        except HTTPException as exc:
            logger.warning(
                "Skipping %s in show-wide search: %s", episode.title, exc.detail
            )
            return

        if not os.path.exists(container_path):
            logger.warning(
                "Skipping %s in show-wide search: file not found on disk at %s",
                episode.title,
                container_path,
            )
            return

        timeout = settings.subtitle_defaults.extraction_timeout_seconds
        try:
            result = await get_subtitles(
                episode,
                container_path,
                settings.cache_dir,
                ffprobe_timeout=timeout,
                ffmpeg_timeout=timeout,
            )
        except (SubprocessTimeoutError, RuntimeError) as exc:
            logger.warning("Skipping %s in show-wide search: %s", episode.title, exc)
            return

        _index_if_searchable(settings, episode, result)

    # Show-wide search (CLAUDE.md Section 4): mirrors /search-quote's
    # diversity-first ranking but scoped to one show's episodes instead of
    # the whole library. Unlike /search-quote, this DOES touch the live
    # filesystem/Plex for episodes not yet cached — deliberately on-demand
    # per Section 4 ("never pre-indexing an entire show's library
    # proactively"), but acceptable to do inline within one request since a
    # show's episode count is small compared to a whole library (no
    # progress/ETA UI needed here, unlike /search-quote's still-unbuilt
    # Tier 2).
    @app.get("/search-episodes-quote/{show_rating_key}", response_model=LibrarySearchResponse)
    async def search_episodes_quote(show_rating_key: int, quote: str) -> LibrarySearchResponse:
        try:
            episodes = await asyncio.to_thread(app.state.plex.list_episodes, show_rating_key)
        except ShowNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        # Sequential, not gathered concurrently — avoids hammering ffmpeg/Plex
        # with a dozen-plus simultaneous extractions for a never-touched show.
        for episode in episodes:
            await _ensure_episode_cached(episode)

        # Built straight from the episodes list already in hand rather than
        # re-reading the SQLite quote_index — that index exists to avoid a
        # live Plex enumeration, which list_episodes() above already did.
        cached_titles = [
            CachedTitle(
                guid=ep.guid,
                rating_key=ep.rating_key,
                title=ep.title,
                library_name=ep.library_name,
            )
            for ep in episodes
        ]

        qm = settings.quote_match
        matches = search_cached_library(
            settings.cache_dir,
            cached_titles,
            quote,
            result_limit=qm.candidate_limit,
            min_score=qm.min_score,
            max_window_gap_seconds=qm.max_window_gap_seconds,
            context_lines=qm.context_lines,
            per_title_limit=qm.library_per_title_limit,
        )

        return LibrarySearchResponse(
            matches=[
                LibraryQuoteMatchOut(
                    rating_key=m.rating_key,
                    title=m.title,
                    library_name=m.library_name,
                    start=m.match.start,
                    end=m.match.end,
                    timecode=_format_display_timecode(m.match.start),
                    text=m.match.text,
                    score=m.match.score,
                    context_before=list(m.match.context_before),
                    context_after=list(m.match.context_after),
                )
                for m in matches
            ],
            confident_score=qm.confident_score,
            min_score=qm.min_score,
        )

    return app
