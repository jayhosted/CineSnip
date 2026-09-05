from __future__ import annotations

import uuid
from pathlib import Path

from app.worker.ffmpeg import escape_filter_path
from app.worker.subprocess_utils import run_and_capture
from app.worker.subtitle_render import StylePreset, build_ass_document
from app.worker.subtitles import SubtitleEntry

PREVIEW_WIDTH = 480
PREVIEW_HEIGHT = 270
# A flat background, not a shipped image/video asset — no binary file to
# maintain, and it makes any legibility problem visible (low-contrast
# color, missing outline) attributable to the style itself, not a busy
# backdrop competing with it.
PREVIEW_BACKGROUND_COLOR = "0x1a1a2e"
PREVIEW_SAMPLE_TEXT = 'Sample subtitle text: "Hello!" (2026) — 100%'


async def render_style_preview(
    style: StylePreset,
    fonts_dir: Path,
    scratch_dir: Path,
    timeout_seconds: float = 30.0,
) -> bytes:
    """Renders one static PNG frame of `style` burned over a fixed sample
    line, via the identical libass subtitles-filter path real clips use —
    true WYSIWYG for font/color/position/box styling, one frame instead of
    a whole clip's worth."""
    entry = SubtitleEntry(index=1, start=0.0, end=1.0, text=PREVIEW_SAMPLE_TEXT)
    doc = build_ass_document([entry], style, PREVIEW_WIDTH, PREVIEW_HEIGHT)

    scratch_dir.mkdir(parents=True, exist_ok=True)
    fonts_dir.mkdir(parents=True, exist_ok=True)
    ass_path = scratch_dir / f"preview-{uuid.uuid4().hex}.ass"
    ass_path.write_text(doc, encoding="utf-8")
    try:
        png_bytes = await run_and_capture(
            [
                "ffmpeg", "-y",
                "-f", "lavfi",
                "-i", f"color=size={PREVIEW_WIDTH}x{PREVIEW_HEIGHT}:color={PREVIEW_BACKGROUND_COLOR}",
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
