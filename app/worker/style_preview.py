from __future__ import annotations

import asyncio
import os
import random
import uuid
from pathlib import Path

from app.settings import Settings, SettingsError
from app.worker import search_index
from app.worker.ffmpeg import build_seek_args, escape_filter_path, probe_video_duration_seconds
from app.worker.media_client import MediaClient, MovieResult
from app.worker.path_mapper import NoPathMappingError, resolve_container_path
from app.worker.subprocess_utils import run_and_capture
from app.worker.subtitle_render import StylePreset, build_ass_document
from app.worker.subtitles import SubtitleEntry

PREVIEW_WIDTH = 480
PREVIEW_HEIGHT = 270
# A flat background, not a shipped image/video asset — no binary file to
# maintain, and it makes any legibility problem visible (low-contrast
# color, missing outline) attributable to the style itself, not a busy
# backdrop competing with it. Used whenever no real-movie background frame
# is available (empty library cache, or every sampled title failed to
# resolve — see pick_random_movie_frame_source).
PREVIEW_BACKGROUND_COLOR = "0x1a1a2e"
PREVIEW_SAMPLE_TEXT = 'Sample subtitle text: "Hello!" (2026) — 100%'

# A background-frame PNG's filename is style-preview-bg-<32 hex chars>.png —
# this pattern is the only thing accepted back from a client as a
# background_id (see app/worker/api.py's preview route), the same
# defensive-validation posture as font_upload.py's preset_name sanitization
# for any user-influenced string that ends up in a filesystem path.
BACKGROUND_ID_PATTERN = r"^[0-9a-f]{32}$"
_BACKGROUND_FILENAME_PREFIX = "style-preview-bg-"

# How many random cached titles to try before giving up and falling back to
# the flat background — a stale cache entry (path mapping since removed, or
# the file itself moved/deleted) shouldn't make every preview session
# silently retry forever.
_BACKGROUND_PICK_ATTEMPTS = 3


def background_frame_path(scratch_dir: Path, background_id: str) -> Path:
    return scratch_dir / f"{_BACKGROUND_FILENAME_PREFIX}{background_id}.png"


async def pick_random_movie_frame_source(
    media: MediaClient, settings: Settings, quote_index_db_path: Path,
) -> tuple[MovieResult, str] | None:
    """Picks a random already-cached movie — the FTS5 subtitle-search index
    (search_index.list_titles, cache/quote_index.db's current `titles`
    table), not a fresh Plex/Jellyfin library enumeration — and resolves it
    to a real, currently-on-disk container path. Reusing the index instead
    of enumerating the library live matches every other library-wide
    feature here (CLAUDE.md's library-sync section): a fresh enumeration is
    exactly the kind of live-Plex call that once collided with a concurrent
    library_sync pass and stalled a request by ~14s. Returns
    (movie, container_path), or None if no cached movie currently resolves
    (empty cache, or every sampled title's file/path-mapping is gone)."""
    cached = search_index.list_titles(quote_index_db_path)
    candidates = []
    for c in cached:
        if c.library_name not in media.movie_library_names:
            continue
        try:
            # extract_background_frame skips this project's 3D crop handling
            # (a disposable preview backdrop doesn't need it) — a 3D
            # library's side-by-side/over-under frame would come out
            # squished/doubled, so it's excluded from the pick entirely
            # rather than mis-rendered. SettingsError means a stale cache
            # entry whose library was since removed from config.yaml —
            # skip it like any other stale entry, don't crash the pick.
            if settings.three_d_format_for(c.library_name) != "none":
                continue
        except SettingsError:
            continue
        candidates.append(c)
    if not candidates:
        return None

    random.shuffle(candidates)
    for cached_title in candidates[:_BACKGROUND_PICK_ATTEMPTS]:
        try:
            movie = await asyncio.to_thread(media.get_movie, cached_title.media_id)
        except Exception:
            continue
        try:
            mappings = settings.path_mappings_for(movie.library_name)
            container_path = resolve_container_path(movie.source_path, mappings)
        except (SettingsError, NoPathMappingError):
            continue
        if not os.path.exists(container_path):
            continue
        return movie, container_path
    return None


async def extract_background_frame(
    container_path: str, scratch_dir: Path, timeout_seconds: float = 30.0,
) -> Path:
    """Extracts one representative frame from a real movie file, scaled and
    centre-cropped to fill the preview frame exactly, as the style
    preview's backdrop — lets a color/outline choice be judged against real
    footage instead of a flat color. Deliberately skips this project's
    usual HDR-tonemap/3D-crop handling (ffmpeg.py's ClipRenderer): those
    exist for output-quality correctness on a real render, which a
    disposable preview backdrop doesn't need."""
    duration = await probe_video_duration_seconds(container_path)
    # A third of the way in, clamped well clear of both ends — better odds
    # of landing on an actual scene than t=0's frequently black/logo frame.
    timestamp = min(max(duration * 0.3, 30.0), max(duration - 30.0, 0.0))

    scratch_dir.mkdir(parents=True, exist_ok=True)
    output_path = scratch_dir / f"{_BACKGROUND_FILENAME_PREFIX}{uuid.uuid4().hex}.png"
    await run_and_capture(
        [
            "ffmpeg", "-y",
            *build_seek_args(timestamp, 0.1),
            "-i", container_path,
            "-frames:v", "1",
            "-vf", (
                f"scale={PREVIEW_WIDTH}:{PREVIEW_HEIGHT}:force_original_aspect_ratio=increase,"
                f"crop={PREVIEW_WIDTH}:{PREVIEW_HEIGHT}"
            ),
            "-f", "image2",
            "-vcodec", "png",
            str(output_path),
        ],
        timeout_seconds,
        error_prefix="ffmpeg preview background extraction",
    )
    return output_path


async def render_style_preview(
    style: StylePreset,
    fonts_dir: Path,
    scratch_dir: Path,
    timeout_seconds: float = 30.0,
    background_path: Path | None = None,
) -> bytes:
    """Renders one static PNG frame of `style` burned over a sample line,
    via the identical libass subtitles-filter path real clips use — true
    WYSIWYG for font/color/position/box styling, one frame instead of a
    whole clip's worth. `background_path` (a pre-extracted real-movie
    frame, already sized to PREVIEW_WIDTH x PREVIEW_HEIGHT) is used as the
    backdrop when given; otherwise falls back to the flat color source."""
    entry = SubtitleEntry(index=1, start=0.0, end=1.0, text=PREVIEW_SAMPLE_TEXT)
    doc = build_ass_document([entry], style, PREVIEW_WIDTH, PREVIEW_HEIGHT)

    scratch_dir.mkdir(parents=True, exist_ok=True)
    fonts_dir.mkdir(parents=True, exist_ok=True)
    ass_path = scratch_dir / f"preview-{uuid.uuid4().hex}.ass"
    ass_path.write_text(doc, encoding="utf-8")
    if background_path is not None:
        input_args = ["-i", str(background_path)]
    else:
        input_args = [
            "-f", "lavfi",
            "-i", f"color=size={PREVIEW_WIDTH}x{PREVIEW_HEIGHT}:color={PREVIEW_BACKGROUND_COLOR}",
        ]
    try:
        png_bytes = await run_and_capture(
            [
                "ffmpeg", "-y",
                *input_args,
                "-vf", (
                    f"subtitles={escape_filter_path(ass_path)}"
                    f":fontsdir={escape_filter_path(fonts_dir)}"
                ),
                "-frames:v", "1",
                "-f", "image2pipe",
                "-vcodec", "png",
                "pipe:1",
            ],
            timeout_seconds,
            error_prefix="ffmpeg style preview render",
            capture_stdout=True,
        )
        return png_bytes or b""
    finally:
        ass_path.unlink(missing_ok=True)
