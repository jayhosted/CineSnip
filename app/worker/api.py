from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.settings import Settings, SettingsError
from app.worker import quote_index, search_index
from app.worker.ffmpeg import ClipRenderer, RenderTimeoutError, parse_timecode
from app.worker.gif_optimize import GifOptimizeError, optimize_gif as _real_optimize_gif
from app.worker.library_search import LibraryQuoteMatch, pick_random_quote, search_cached_library
from app.worker.path_mapper import NoPathMappingError, resolve_container_path
from app.worker.media_client import (
    EpisodeNotFoundError,
    MovieNotFoundError,
    MovieResult,
    ShowNotFoundError,
    create_media_client,
)
from app.worker.quote_index import CachedTitle
from app.worker.quotes import find_quote_matches
from app.worker.subprocess_utils import SubprocessTimeoutError
from app.worker.subtitle_render import STYLE_PRESETS
from app.worker.subtitles import (
    SubtitleResult,
    SubtitleSource,
    find_sidecar_subtitle,
    get_subtitles,
)

logger = logging.getLogger(__name__)


class MovieResultOut(BaseModel):
    media_id: str
    title: str
    year: int | None
    duration_ms: int
    thumb_url: str | None
    library_name: str


class SearchResponse(BaseModel):
    results: list[MovieResultOut]


class SubtitleStatusResponse(BaseModel):
    media_id: str
    # True when neither a sidecar file nor a cached result exists, meaning
    # a subtitle-needing request is about to fall through to a cold
    # embedded-stream extraction (no fast-seek, can take minutes on a large
    # file — CLAUDE.md Section 5/6). A cheap, ffmpeg-free hint so the bot
    # can warn the user before committing to that wait, not a guarantee —
    # the real request still does its own freshness-checked cache read.
    likely_slow: bool


class ResolveResponse(BaseModel):
    media_id: str
    title: str
    year: int | None
    duration_ms: int
    thumb_url: str | None
    library_name: str


class RenderRequest(BaseModel):
    media_id: str
    # Either (start, end) or timecode[, end_timecode] must be given — never
    # both forms at once. A ClipEditView re-render (issue #5) already has an
    # exact numeric span and sends start/end directly, skipping timecode
    # parsing on every nudge/merge; every other caller still sends timecode.
    timecode: str | None = None
    # Set by a quote-driven clip to the matched line's own span (end -
    # start), so the render is exactly that line rather than a fixed
    # duration. Mutually exclusive with end_timecode in practice (the bot
    # only ever sends one or the other) — duration takes priority if both
    # are somehow set. Only meaningful alongside timecode.
    duration: float | None = None
    # Set for a direct timecode request with an explicit end, so the user
    # can pick a custom span instead of the fixed render_defaults.duration_seconds.
    # A raw string (not a pre-computed float) since it needs the same
    # parse_timecode() as `timecode` — kept server-side so timecode format
    # support only lives in one place. Only meaningful alongside timecode.
    end_timecode: str | None = None
    # Explicit numeric span in seconds — a ClipEditView edit action's
    # already-resolved start/end. Takes priority over timecode/end_timecode
    # when both are given (never happens from a real caller, but priority
    # is defined so behavior isn't ambiguous).
    start: float | None = None
    end: float | None = None
    # None uses render_defaults.format. mp3/ogg (issue #6) are audio-only —
    # the bot always sends one of those explicitly for /snip audio, never
    # None, so an audio request never falls through to render_defaults.format
    # (which stays video-only, see Settings.render_defaults).
    format: Literal["gif", "mp4", "webm", "mp3", "ogg"] | None = None
    # None/"none" means no subtitle burn-in. A style requested on a title
    # with no usable subtitles for the clip's own window degrades to plain
    # (no burn-in) rather than erroring — echoed back via X-Clip-Style so
    # the caller can tell the difference from what it asked for.
    style: Literal["classic", "boxed", "cinematic", "meme", "none"] | None = None
    # Per-line text overrides/suppressions for a clip-edit session, keyed by
    # the subtitle entry's own index (SubtitleEntry.index / GET /subtitles'
    # entries[].index) — JSON object keys are always strings on the wire,
    # converted to int below. A value of None suppresses that line.
    subtitle_overrides: dict[str, str | None] | None = None


class SubtitleEntryOut(BaseModel):
    index: int
    start: float
    end: float
    text: str


class SubtitleDiagnosticResponse(BaseModel):
    media_id: str
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
    media_id: str
    title: str
    subtitle_source: str
    confident_score: float
    min_score: float
    # True when the engine found more matches than quote_match.fetch_limit
    # and this response was cut off at that cap — lets the bot tell "this
    # is every match" apart from "there may be more" without guessing off
    # a raw count that could coincidentally equal fetch_limit on its own.
    truncated: bool
    matches: list[QuoteMatchOut]


class LibraryQuoteMatchOut(BaseModel):
    media_id: str
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
    # Same truncation signal as ResolveQuoteResponse.truncated, see there.
    truncated: bool


class RandomQuoteResponse(BaseModel):
    media_id: str
    title: str
    library_name: str
    start: float
    end: float
    timecode: str
    text: str
    # Opaque per-pick identity the bot echoes back as exclude/most_recent on
    # a reroll, so a shuffle journey never repeats a line already shown.
    entry_id: int
    # Size of the eligible candidate pool for this exact scope/filter,
    # ignoring exclusion — lets the bot disable Shuffle and say so up front
    # when there's only one match (CLAUDE.md's "Celina" fix), instead of a
    # button that silently does nothing.
    pool_size: int
    # True if the pool had to reset (every candidate already excluded) to
    # produce this pick.
    exhausted: bool


# Tried in order, only once the configured-settings render already
# exceeds render_defaults.max_file_size_bytes — each entry is (fps,
# width), applied as a cap (min(configured, tier value), never upscaling
# past what was actually requested). FPS drops before width: a moderate
# frame-rate cut is usually less visually noticeable than shrinking the
# frame itself, especially for the short dialogue-driven clips this
# project generates, and a GIF's file size scales roughly with frame
# count — cutting fps buys real size reduction before touching
# resolution at all. Width only comes down once fps alone isn't enough,
# and even then only as far as needed.
_DOWNSCALE_TIERS: list[tuple[int, int]] = [
    (12, 480),
    (10, 400),
    (8, 320),
    (8, 240),
]


async def _render_within_size_limit(
    renderer: ClipRenderer,
    container_path: str,
    start: float,
    clip_duration: float,
    scratch_dir: Path,
    clip_format: str,
    subtitle_entries,
    style_preset,
    three_d_format: str,
    subtitle_overrides: dict[int, str | None] | None,
    configured_fps: int,
    configured_width: int,
    max_bytes: int,
    optimize_gif=_real_optimize_gif,
    gifsicle_timeout_seconds: float = 60.0,
    audio_language: str = "eng",
    full_parallel_gifsicle: bool = False,
) -> bytes:
    """Renders at the configured fps/width first. If the result exceeds
    max_bytes and the format is GIF, tries gifsicle recompression
    (app/worker/gif_optimize.py) next — resolution and frame rate are
    never touched for this step. Only if that's still not enough (or the
    format isn't GIF) does it retry at progressively smaller settings
    from _DOWNSCALE_TIERS, stopping as soon as one fits. If every tier is
    exhausted and still too large (a very long or high-motion clip), the
    smallest attempt made is returned rather than erroring — a
    still-oversized clip is more useful than none, and Discord's own
    upload rejection (surfaced to the user as a clear error, not a raw
    500) is the actual final backstop CLAUDE.md Section 7 falls back on.

    optimize_gif is an injectable seam (defaults to the real gifsicle
    wrapper) purely for testability — mirrors how `renderer` is already
    swapped for a fake in tests/test_render_size_limit.py."""
    clip_bytes = await renderer.render_clip(
        container_path, start, clip_duration, scratch_dir, clip_format,
        subtitle_entries=subtitle_entries, style=style_preset,
        three_d_format=three_d_format, subtitle_overrides=subtitle_overrides,
        audio_language=audio_language,
    )
    if len(clip_bytes) <= max_bytes:
        return clip_bytes

    smallest = clip_bytes

    if clip_format == "gif":
        try:
            gifsicle_result = await optimize_gif(
                clip_bytes,
                max_bytes,
                timeout_seconds=gifsicle_timeout_seconds,
                full_parallel=full_parallel_gifsicle,
            )
        except GifOptimizeError as exc:
            logger.warning("gifsicle tier unavailable, falling back to downscaling: %s", exc)
        else:
            if len(gifsicle_result) < len(smallest):
                smallest = gifsicle_result
            if len(gifsicle_result) <= max_bytes:
                return gifsicle_result

    # _DOWNSCALE_TIERS is fps/width pairs — meaningless for an audio-only
    # format (issue #6), and a re-encode at the same bitrate would just
    # reproduce the same size four times over. Audio clips are also far
    # under max_bytes in practice, so this path is a defensive skip, not
    # a real-world gap.
    if clip_format in ("mp3", "ogg"):
        return smallest

    for tier_fps, tier_width in _DOWNSCALE_TIERS:
        fps = min(configured_fps, tier_fps)
        width = min(configured_width, tier_width)
        attempt = await renderer.render_clip(
            container_path, start, clip_duration, scratch_dir, clip_format,
            subtitle_entries=subtitle_entries, style=style_preset,
            three_d_format=three_d_format, subtitle_overrides=subtitle_overrides,
            fps=fps, width=width,
        )
        if len(attempt) < len(smallest):
            smallest = attempt
        if len(attempt) <= max_bytes:
            return attempt

        # A downscale tier landing just short of budget doesn't have to
        # mean stepping down to an even smaller/choppier tier — a gifsicle
        # pass on *this* tier's own output is far cheaper than another
        # full ffmpeg re-encode, and can clear budget without dropping
        # resolution any further than this tier already has. This is a
        # fresh, never-before-optimized GIF at this resolution (each
        # tier's `attempt` is straight from render_clip), not a second
        # gifsicle pass stacked on already-`-O3`'d bytes — confirmed by
        # measurement that gifsicle's lossy compression barely helps once
        # applied on top of its own prior lossless pass, so this must
        # never receive `attempt` after it's already been through
        # optimize_gif once.
        if clip_format == "gif":
            try:
                gifsicle_attempt = await optimize_gif(
                    attempt,
                    max_bytes,
                    timeout_seconds=gifsicle_timeout_seconds,
                    full_parallel=full_parallel_gifsicle,
                )
            except GifOptimizeError as exc:
                logger.warning(
                    "gifsicle tier unavailable for downscaled attempt, moving to next tier: %s",
                    exc,
                )
            else:
                if len(gifsicle_attempt) < len(smallest):
                    smallest = gifsicle_attempt
                if len(gifsicle_attempt) <= max_bytes:
                    return gifsicle_attempt
    return smallest


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
        return resolve_container_path(movie.source_path, mappings)
    except NoPathMappingError as exc:
        # exc's own message is already sanitized (path_mapper.py) — this
        # logs the raw source_path server-side, since a Discord/web-facing
        # 422 must not echo it back (pre-publication audit finding).
        logger.warning(
            "No path mapping for '%s' (library=%s)", exc.source_path, movie.library_name
        )
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _safe_runtime_error(exc: Exception, status_code: int = 500) -> HTTPException:
    # A bare RuntimeError here (renderer/subtitle-extraction failure) may
    # still carry container paths or other internal detail in its message
    # even after subprocess_utils.py's own sanitizing (e.g. a renderer
    # failing before ever reaching a subprocess call) — never put it in a
    # Discord/web-facing error response (pre-publication audit finding).
    # Logged in full server-side instead, where it's actually useful.
    logger.error("Request failed: %s", exc)
    return HTTPException(
        status_code=status_code,
        detail="Failed to process this request. See worker logs for details.",
    )


def _file_not_found_error(container_path: str) -> HTTPException:
    # The mapped container path is internal/filesystem detail that must
    # never reach a Discord/web-facing error response (pre-publication
    # audit finding) — logged server-side instead, where it's actually
    # useful for troubleshooting a stale/incomplete path mapping.
    logger.warning("File not found on disk at mapped path: %s", container_path)
    return HTTPException(
        status_code=422,
        detail="File not found on disk at the mapped path. Check path_mappings in config.yaml.",
    )


def _index_if_searchable(settings: Settings, movie: MovieResult, result: SubtitleResult) -> None:
    # Every title actually checked gets *some* record — a searchable
    # result goes into cached_titles (with its source type, for the
    # dashboard's coverage stats), a NONE result goes into
    # no_subtitle_titles instead (Section 5's documented gap: not
    # searchable, but still worth knowing "this was checked").
    #
    # Deliberately does nothing to search_index here: get_subtitles()
    # (app/worker/subtitles.py) already unconditionally calls
    # search_index.upsert_title(...) for all three outcomes, and every
    # real caller here calls get_subtitles() immediately before this
    # function with the same movie/result. Writing here too would be a
    # redundant full delete+reinsert of every subtitle line/FTS row on
    # every /render, /resolve-quote, and episode-cache request for data
    # that didn't change.
    if result.source is SubtitleSource.NONE:
        quote_index.upsert_no_subtitle_title(
            settings.quote_index_db_path,
            result.guid,
            movie.media_id,
            movie.title,
            movie.library_name,
        )


def _to_out(movie: MovieResult) -> MovieResultOut:
    return MovieResultOut(
        media_id=movie.media_id,
        title=movie.title,
        year=movie.year,
        duration_ms=movie.duration_ms,
        thumb_url=movie.thumb_url,
        library_name=movie.library_name,
    )


async def _movie_library_matches(
    app: FastAPI, settings: Settings, quote: str
) -> tuple[list[CachedTitle], list, bool]:
    # Shared by /search-quote and /search-quote-extend: both search exactly
    # "every cached movie-library title" — the TV episodes sharing this same
    # search_index (CLAUDE.md Section 4) are filtered out here, once.
    movie_library_names = app.state.media.movie_library_names
    cached_titles = [
        t
        for t in search_index.list_titles(settings.quote_index_db_path)
        if t.library_name in movie_library_names
    ]
    qm = settings.quote_match
    # Runs sqlite FTS5 queries + rapidfuzz scoring, which can take several
    # seconds (up to ~9.6s in a full-scan fallback per search_cached_library's
    # docstring). Must go through to_thread: bot and worker share one process
    # and one event loop (CLAUDE.md Section 8), so a synchronous call here
    # freezes Discord's own interaction dispatch for the same duration —
    # confirmed as the root cause of "The application did not respond"
    # errors on /snip tv.
    #
    # Fetching fetch_limit + 1 (rather than exactly fetch_limit) is what
    # lets the caller tell "this is genuinely every match" apart from "the
    # cap was hit, there may be more" — a plain len(matches) == fetch_limit
    # check can't distinguish those two cases (issue #7 follow-up).
    matches = await asyncio.to_thread(
        search_cached_library,
        settings.quote_index_db_path,
        cached_titles,
        quote,
        result_limit=qm.fetch_limit + 1,
        min_score=qm.min_score,
        max_window_gap_seconds=qm.max_window_gap_seconds,
        context_lines=qm.context_lines,
        per_title_limit=qm.library_per_title_limit,
    )
    truncated = len(matches) > qm.fetch_limit
    return cached_titles, matches[: qm.fetch_limit], truncated


def _random_quote_response(result) -> RandomQuoteResponse:
    """Shared by /random-quote, /random-line, and /random-line-show — turns
    a library_search.RandomPick into the wire response, echoing entry_id/
    pool_size/exhausted so the bot can track a reroll journey's history."""
    pick = result.pick
    return RandomQuoteResponse(
        media_id=pick.media_id,
        title=pick.title,
        library_name=pick.library_name,
        start=pick.match.start,
        end=pick.match.end,
        timecode=_format_display_timecode(pick.match.start),
        text=pick.match.text,
        entry_id=pick.entry_id,
        pool_size=result.pool_size,
        exhausted=result.exhausted,
    )


def _library_search_payload(matches: list[LibraryQuoteMatch], qm, truncated: bool) -> dict:
    return {
        "matches": [
            {
                "media_id": m.media_id,
                "title": m.title,
                "library_name": m.library_name,
                "start": m.match.start,
                "end": m.match.end,
                "timecode": _format_display_timecode(m.match.start),
                "text": m.match.text,
                "score": m.match.score,
                "context_before": list(m.match.context_before),
                "context_after": list(m.match.context_after),
            }
            for m in matches
        ],
        "confident_score": qm.confident_score,
        "min_score": qm.min_score,
        "truncated": truncated,
    }


def create_app(settings: Settings) -> FastAPI:
    app = FastAPI()
    app.state.settings = settings
    app.state.media = create_media_client(settings)
    app.state.renderer = ClipRenderer(
        fps=settings.render_defaults.fps,
        width=settings.render_defaults.width,
        timeout_seconds=settings.render_defaults.timeout_seconds,
        crop_cache_db_path=settings.quote_index_db_path,
    )
    # Global cap on simultaneous ffmpeg+gifsicle render work (issue #17) —
    # layered on top of, not a replacement for, the single-pass GIF
    # encoding and bounded-parallelism gifsicle search that same benchmark
    # produced. A render beyond the limit waits for a free slot rather
    # than failing.
    app.state.render_semaphore = asyncio.Semaphore(settings.render_defaults.max_concurrent_renders)
    # Plain int, not derived from the semaphore's own internals — tracks how
    # many renders are inside the semaphore block *right now*, so gifsicle
    # optimization can tell "am I the only one" apart from "others are
    # contending" and skip _GROUP_SIZE's pacing when it is. Safe unguarded
    # (no lock) since every mutation below happens on the single event loop
    # thread with no `await` between the increment/decrement and the read.
    app.state.active_renders = 0

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/search", response_model=SearchResponse)
    def search(query: str) -> SearchResponse:
        results = app.state.media.search_movies(query)
        return SearchResponse(results=[_to_out(m) for m in results])

    @app.get("/search-shows", response_model=SearchResponse)
    def search_shows(query: str) -> SearchResponse:
        results = app.state.media.search_shows(query)
        return SearchResponse(results=[_to_out(m) for m in results])

    async def _get_movie(media_id: str) -> MovieResult:
        # get_movie() is a synchronous plexapi/requests call — always offload
        # it so a slow Plex response doesn't stall the whole event loop
        # (this worker has no other way to serve concurrent requests).
        try:
            return await asyncio.to_thread(app.state.media.get_movie, media_id)
        except MovieNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/resolve/{media_id}", response_model=ResolveResponse)
    async def resolve(media_id: str) -> ResolveResponse:
        movie = await _get_movie(media_id)
        container_path = _resolve_container_path(movie, settings)

        if not os.path.exists(container_path):
            raise _file_not_found_error(container_path)

        return ResolveResponse(
            media_id=movie.media_id,
            title=movie.title,
            year=movie.year,
            duration_ms=movie.duration_ms,
            thumb_url=movie.thumb_url,
            library_name=movie.library_name,
        )

    @app.get("/subtitle-status/{media_id}", response_model=SubtitleStatusResponse)
    async def subtitle_status(media_id: str) -> SubtitleStatusResponse:
        movie = await _get_movie(media_id)
        try:
            container_path = _resolve_container_path(movie, settings)
        except HTTPException:
            # Best-effort hint endpoint — a path-mapping problem is the real
            # request's job to report clearly, not this one's.
            return SubtitleStatusResponse(media_id=media_id, likely_slow=False)

        sidecar = find_sidecar_subtitle(Path(container_path))
        cached = search_index.get_entries(settings.quote_index_db_path, movie.guid) is not None
        likely_slow = sidecar is None and not cached
        return SubtitleStatusResponse(media_id=media_id, likely_slow=likely_slow)

    @app.get("/resolve-episode/{show_media_id}", response_model=ResolveResponse)
    async def resolve_episode(show_media_id: str, season: int, episode: int) -> ResolveResponse:
        # Turns show+season+episode into a concrete media_id + display
        # info — everything downstream (/render, /resolve-quote, etc.) is
        # the unmodified movie flow from here, since it's already generic
        # over any media_id.
        try:
            ep = await asyncio.to_thread(
                app.state.media.get_episode, show_media_id, season, episode
            )
        except (EpisodeNotFoundError, ShowNotFoundError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        container_path = _resolve_container_path(ep, settings)
        if not os.path.exists(container_path):
            raise _file_not_found_error(container_path)

        return ResolveResponse(
            media_id=ep.media_id,
            title=ep.title,
            year=ep.year,
            duration_ms=ep.duration_ms,
            thumb_url=ep.thumb_url,
            library_name=ep.library_name,
        )

    @app.post("/render")
    async def render(req: RenderRequest) -> Response:
        movie = await _get_movie(req.media_id)
        container_path = _resolve_container_path(movie, settings)

        if not os.path.exists(container_path):
            raise _file_not_found_error(container_path)

        rd = settings.render_defaults
        duration_s = movie.duration_ms / 1000

        if req.start is not None or req.end is not None:
            if req.start is None or req.end is None:
                raise HTTPException(
                    status_code=422, detail="start and end must both be given together."
                )
            start = req.start
            end = req.end
            if start < 0 or start >= duration_s:
                raise HTTPException(
                    status_code=422,
                    detail=f"Start ({start:.1f}s) is outside the film's runtime.",
                )
            clip_duration = end - start
            if clip_duration <= 0:
                raise HTTPException(
                    status_code=422, detail=f"end ({end}) must be after start ({start})."
                )
            if not (rd.min_duration_seconds <= clip_duration <= rd.max_duration_seconds):
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"That's a {clip_duration:.1f}s clip; clips must be "
                        f"between {rd.min_duration_seconds:.0f}s and "
                        f"{rd.max_duration_seconds:.0f}s."
                    ),
                )
        elif req.timecode is not None:
            try:
                start = parse_timecode(req.timecode)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc

            if start < 0 or start >= duration_s:
                raise HTTPException(
                    status_code=422,
                    detail=f"Timecode {req.timecode} is outside the film's runtime.",
                )

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
        else:
            raise HTTPException(
                status_code=422, detail="Give either start and end, or a timecode."
            )

        clip_format = req.format if req.format is not None else rd.format
        # Audio has no frame to burn subtitles into — force "none" server-side
        # rather than trusting every caller to omit style (defense in depth,
        # not just a bot-side convention).
        requested_style = (req.style or "none") if clip_format not in ("mp3", "ogg") else "none"
        subtitle_overrides: dict[int, str | None] = (
            {int(k): v for k, v in req.subtitle_overrides.items()} if req.subtitle_overrides else {}
        )

        subtitle_entries = None
        style_preset = None
        if requested_style != "none":
            timeout = settings.subtitle_defaults.extraction_timeout_seconds
            try:
                subtitle_result = await get_subtitles(
                    movie,
                    container_path,
                    settings.cache_dir,
                    settings.quote_index_db_path,
                    ffprobe_timeout=timeout,
                    ffmpeg_timeout=timeout,
                )
            except (SubprocessTimeoutError, RuntimeError) as exc:
                raise _safe_runtime_error(exc) from exc

            _index_if_searchable(settings, movie, subtitle_result)

            # A title with no usable subtitles (Section 5's documented gap)
            # degrades to a plain render rather than erroring — the user
            # still gets *a* clip, just without burn-in text.
            if subtitle_result.source is not SubtitleSource.NONE and subtitle_result.entries:
                subtitle_entries = subtitle_result.entries
                style_preset = STYLE_PRESETS[requested_style]

        resolved_style = requested_style if style_preset is not None else "none"

        try:
            # Only the actual ffmpeg+gifsicle work is gated — everything
            # above (Plex fetch, subtitle extraction/lookup) already
            # happened outside the semaphore, so a request waiting for a
            # render slot isn't also holding one up over unrelated I/O.
            async with app.state.render_semaphore:
                app.state.active_renders += 1
                try:
                    clip_bytes = await _render_within_size_limit(
                        app.state.renderer,
                        container_path,
                        start,
                        clip_duration,
                        settings.scratch_dir,
                        clip_format,
                        subtitle_entries,
                        style_preset,
                        settings.three_d_format_for(movie.library_name),
                        subtitle_overrides or None,
                        settings.render_defaults.fps,
                        settings.render_defaults.width,
                        settings.render_defaults.max_file_size_bytes,
                        # Only meaningful once inside the semaphore, since
                        # that's the whole population active_renders counts —
                        # ==1 means this render is currently the only one.
                        full_parallel_gifsicle=app.state.active_renders == 1,
                        gifsicle_timeout_seconds=settings.render_defaults.gifsicle_timeout_seconds,
                        audio_language=settings.render_defaults.audio_language,
                    )
                finally:
                    app.state.active_renders -= 1
        except RenderTimeoutError as exc:
            # Safe to echo verbatim — SubprocessTimeoutError's message is a
            # fixed, generic template (error_prefix + elapsed seconds), never
            # subprocess stderr or a file path.
            raise HTTPException(status_code=504, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise _safe_runtime_error(exc) from exc

        media_type = {
            "gif": "image/gif",
            "mp4": "video/mp4",
            "webm": "video/webm",
            "mp3": "audio/mpeg",
            "ogg": "audio/ogg",
        }[clip_format]
        # Bot doesn't know settings.render_defaults.format/style, so it
        # can't infer what was actually used for a request that left them
        # unset, or whether a requested style got silently downgraded to
        # "none" — echo both back instead of the bot guessing/duplicating
        # config defaults client-side.
        return Response(
            content=clip_bytes,
            media_type=media_type,
            headers={
                "X-Clip-Format": clip_format,
                "X-Clip-Style": resolved_style,
                # Bot doesn't know render_defaults.duration_seconds or its
                # min/max clamp either — echo the actual start/duration used
                # so a "Posted by" message (CLAUDE.md issue #9) can show the
                # real span instead of guessing at config it can't see.
                "X-Clip-Start": str(start),
                "X-Clip-Duration": str(clip_duration),
            },
        )

    async def _load_subtitles(media_id: str) -> tuple[MovieResult, SubtitleResult]:
        movie = await _get_movie(media_id)
        container_path = _resolve_container_path(movie, settings)

        if not os.path.exists(container_path):
            raise _file_not_found_error(container_path)

        timeout = settings.subtitle_defaults.extraction_timeout_seconds
        try:
            result = await get_subtitles(
                movie,
                container_path,
                settings.cache_dir,
                settings.quote_index_db_path,
                ffprobe_timeout=timeout,
                ffmpeg_timeout=timeout,
            )
        except (SubprocessTimeoutError, RuntimeError) as exc:
            raise _safe_runtime_error(exc) from exc

        _index_if_searchable(settings, movie, result)

        return movie, result

    # Diagnostic endpoint for manually inspecting the raw parsed cues for a
    # title — useful on its own for verifying extraction against the real
    # library, and as a companion to /resolve-quote for picking/verifying
    # test quotes and finding cue boundaries.
    @app.get("/subtitles/{media_id}", response_model=SubtitleDiagnosticResponse)
    async def subtitles(media_id: str) -> SubtitleDiagnosticResponse:
        movie, result = await _load_subtitles(media_id)

        return SubtitleDiagnosticResponse(
            media_id=movie.media_id,
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

    @app.get("/resolve-quote/{media_id}", response_model=ResolveQuoteResponse)
    async def resolve_quote(media_id: str, quote: str) -> ResolveQuoteResponse:
        movie, result = await _load_subtitles(media_id)

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
        # Single-title, in-request computation — no O(corpus) cost and
        # nothing to pre-filter (it's already scoped to this one title's
        # entries), so this just calls find_quote_matches() directly and
        # lets it build its own candidates internally (its default
        # `precomputed=None` path) rather than duplicating that logic here.
        #
        # Fetching fetch_limit + 1 (not exactly fetch_limit) is what lets
        # `truncated` below tell "this is genuinely every match" apart from
        # "the cap was hit, there may be more" (issue #7 follow-up).
        matches = find_quote_matches(
            result.entries,
            quote,
            limit=qm.fetch_limit + 1,
            min_score=qm.min_score,
            max_window_gap_seconds=qm.max_window_gap_seconds,
            context_lines=qm.context_lines,
        )
        truncated = len(matches) > qm.fetch_limit
        matches = matches[: qm.fetch_limit]

        if not matches:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"No subtitle line in '{movie.title}' resembled that quote. "
                    "Try a shorter, more distinctive phrase."
                ),
            )

        return ResolveQuoteResponse(
            media_id=movie.media_id,
            title=movie.title,
            subtitle_source=result.source.value,
            confident_score=qm.confident_score,
            min_score=qm.min_score,
            truncated=truncated,
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

    @app.get("/random-quote", response_model=RandomQuoteResponse)
    async def random_quote(
        quote: str | None = None,
        media: Literal["movie", "tv", "all"] = "all",
        exclude: list[int] = Query(default=[]),
        most_recent: int | None = None,
    ) -> RandomQuoteResponse:
        # Tier 1 (already-cached titles) only, deliberately — no auto-extend,
        # same reasoning as elsewhere: extracting subtitles for random titles
        # just to serve a for-fun command isn't worth the cost.
        movie_library_names = app.state.media.movie_library_names
        show_library_names = app.state.media.show_library_names
        if media == "movie":
            allowed_libraries = movie_library_names
        elif media == "tv":
            allowed_libraries = show_library_names
        else:
            allowed_libraries = movie_library_names | show_library_names

        cached_titles = [
            t
            for t in search_index.list_titles(settings.quote_index_db_path)
            if t.library_name in allowed_libraries
        ]
        result = pick_random_quote(
            settings.quote_index_db_path,
            cached_titles,
            quote,
            exclude_entry_ids=frozenset(exclude),
            most_recent_entry_id=most_recent,
        )
        if result is None:
            raise HTTPException(status_code=404, detail="No matching cached line found.")

        return _random_quote_response(result)

    @app.get("/random-line/{media_id}", response_model=RandomQuoteResponse)
    async def random_line(
        media_id: str,
        exclude: list[int] = Query(default=[]),
        most_recent: int | None = None,
    ) -> RandomQuoteResponse:
        # /snip movie with no quote/timecode given: a "filtered random" pick
        # (min-word-count quality filter) scoped to just this one title,
        # extracting on demand if not yet cached — mirrors /resolve-quote.
        movie, result = await _load_subtitles(media_id)

        if result.source is SubtitleSource.NONE or not result.entries:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"No usable subtitles for '{movie.title}' (no sidecar .srt "
                    "and no text subtitle stream). Random pick isn't available "
                    "for this title — use timecode instead."
                ),
            )

        cached_titles = [
            CachedTitle(
                guid=movie.guid,
                media_id=movie.media_id,
                title=movie.title,
                library_name=movie.library_name,
            )
        ]
        picked = pick_random_quote(
            settings.quote_index_db_path,
            cached_titles,
            quote=None,
            exclude_entry_ids=frozenset(exclude),
            most_recent_entry_id=most_recent,
            min_words=settings.quote_match.random_min_words,
        )
        if picked is None:
            raise HTTPException(status_code=404, detail="No usable line found for this title.")

        return _random_quote_response(picked)

    @app.get("/random-line-show/{show_media_id}", response_model=RandomQuoteResponse)
    async def random_line_show(
        show_media_id: str,
        season: int | None = None,
        episode: int | None = None,
        exclude: list[int] = Query(default=[]),
        most_recent: int | None = None,
    ) -> RandomQuoteResponse:
        # /snip tv with no quote/timecode given: whole-show scope by default
        # (mirrors whole-show quote search), or a single episode when
        # season+episode are both given.
        if (season is None) != (episode is None):
            raise HTTPException(
                status_code=422, detail="season and episode must be given together or not at all."
            )

        if season is not None:
            try:
                ep = await asyncio.to_thread(
                    app.state.media.get_episode, show_media_id, season, episode
                )
            except (EpisodeNotFoundError, ShowNotFoundError) as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            episodes = [ep]
        else:
            try:
                episodes = await asyncio.to_thread(app.state.media.list_episodes, show_media_id)
            except ShowNotFoundError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc

        # Sequential, not gathered concurrently — same reasoning as
        # /search-episodes-quote: avoids hammering ffmpeg/Plex with a
        # dozen-plus simultaneous extractions for a never-touched show.
        for ep in episodes:
            await _ensure_episode_cached(ep)

        cached_titles = [
            CachedTitle(guid=ep.guid, media_id=ep.media_id, title=ep.title, library_name=ep.library_name)
            for ep in episodes
        ]
        picked = pick_random_quote(
            settings.quote_index_db_path,
            cached_titles,
            quote=None,
            exclude_entry_ids=frozenset(exclude),
            most_recent_entry_id=most_recent,
            min_words=settings.quote_match.random_min_words,
        )
        if picked is None:
            raise HTTPException(status_code=404, detail="No usable line found for this show.")

        return _random_quote_response(picked)

    # Tier 2: extends /search-quote into not-yet-cached movie titles, gated
    # behind library_sync.enabled (CLAUDE.md Roadmap / issue #2 design spec) —
    # with sync disabled this behaves identically to /search-quote, just over
    # a streamed single-event body, so the bot never needs two code paths.
    @app.get("/search-quote-extend")
    async def search_quote_extend(quote: str) -> StreamingResponse:
        # Cache-only by design: library_sync (the 24h scheduled pass, or a
        # manual "Sync now" click) is solely responsible for keeping
        # quote_index.db current. This used to also do an on-demand live
        # Plex re-check + extraction ("Tier 2") when library_sync.enabled
        # was true, but real measurement showed that live check could
        # collide with library_sync's own concurrent pass over the same
        # library — both independently re-enumerating a ~1400-title
        # section at once — stalling a search by ~14s. A brand-new title
        # now only becomes searchable after the next sync rather than
        # instantly, which is the deliberate tradeoff for never blocking a
        # search on live Plex work. Kept as a streamed endpoint (not a
        # plain GET) for wire-format compatibility with the bot's existing
        # NDJSON client, even though it now only ever emits two events.
        async def event_stream():
            _, matches, truncated = await _movie_library_matches(app, settings, quote)
            yield json.dumps({
                "type": "cached",
                **_library_search_payload(matches, settings.quote_match, truncated),
            }) + "\n"
            yield json.dumps({
                "type": "final",
                "remaining_uncached": None,
                **_library_search_payload(matches, settings.quote_match, truncated),
            }) + "\n"

        return StreamingResponse(event_stream(), media_type="application/x-ndjson")

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
                settings.quote_index_db_path,
                ffprobe_timeout=timeout,
                ffmpeg_timeout=timeout,
            )
        except (SubprocessTimeoutError, RuntimeError) as exc:
            logger.warning("Skipping %s in show-wide search: %s", episode.title, exc)
            return

        _index_if_searchable(settings, episode, result)

    # Show-wide search (CLAUDE.md Section 4): mirrors /search-quote's
    # diversity-first ranking but scoped to one show's episodes. Unlike
    # /search-quote, this DOES touch the live filesystem/Plex for episodes
    # not yet cached — acceptable inline within one request since a show's
    # episode count is small (no progress/ETA UI needed here, unlike
    # /search-quote's still-unbuilt Tier 2).
    @app.get("/search-episodes-quote/{show_media_id}", response_model=LibrarySearchResponse)
    async def search_episodes_quote(show_media_id: str, quote: str) -> LibrarySearchResponse:
        try:
            episodes = await asyncio.to_thread(app.state.media.list_episodes, show_media_id)
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
                media_id=ep.media_id,
                title=ep.title,
                library_name=ep.library_name,
            )
            for ep in episodes
        ]

        qm = settings.quote_match
        # Fetching fetch_limit + 1 (not exactly fetch_limit) is what lets
        # `truncated` below tell "this is genuinely every match" apart from
        # "the cap was hit, there may be more" (issue #7 follow-up).
        matches = await asyncio.to_thread(
            search_cached_library,
            settings.quote_index_db_path,
            cached_titles,
            quote,
            result_limit=qm.fetch_limit + 1,
            min_score=qm.min_score,
            max_window_gap_seconds=qm.max_window_gap_seconds,
            context_lines=qm.context_lines,
            per_title_limit=qm.library_per_title_limit,
        )
        truncated = len(matches) > qm.fetch_limit
        matches = matches[: qm.fetch_limit]

        return LibrarySearchResponse(
            matches=[
                LibraryQuoteMatchOut(
                    media_id=m.media_id,
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
            truncated=truncated,
        )

    return app
