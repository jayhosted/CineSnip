from __future__ import annotations

import re
import uuid
from pathlib import Path

from app.worker.subprocess_utils import SubprocessTimeoutError, run_and_capture


class RenderTimeoutError(SubprocessTimeoutError):
    pass


# Accepts any subset of hours/minutes/seconds, in that order, with any of
# these unit spellings — e.g. "1h22m12s", "22min 12sec", "1hr22min2sec".
_UNIT_TIMECODE_PATTERN = re.compile(
    r"^\s*"
    r"(?:(?P<hours>\d+(?:\.\d+)?)\s*(?:h|hr|hrs|hour|hours)\s*)?"
    r"(?:(?P<minutes>\d+(?:\.\d+)?)\s*(?:m|min|mins|minute|minutes)\s*)?"
    r"(?:(?P<seconds>\d+(?:\.\d+)?)\s*(?:s|sec|secs|second|seconds)\s*)?"
    r"$",
    re.IGNORECASE,
)


def parse_timecode(text: str) -> float:
    """Parse a timestamp into seconds. Accepts 'HH:MM:SS'/'MM:SS'/a plain
    number of seconds, or unit-suffixed forms like '1h22m12s', '22m12s',
    '22min 12sec' (any subset of hours/minutes/seconds, in that order)."""
    text = text.strip()

    if ":" in text:
        parts = text.split(":")
        if not 1 <= len(parts) <= 3:
            raise ValueError(f"Invalid timecode: '{text}'")
        try:
            numbers = [float(p) for p in parts]
        except ValueError as exc:
            raise ValueError(f"Invalid timecode: '{text}'") from exc

        seconds = 0.0
        for n in numbers:
            seconds = seconds * 60 + n
        return seconds

    unit_match = _UNIT_TIMECODE_PATTERN.match(text)
    if unit_match and any(unit_match.groups()):
        hours = float(unit_match.group("hours") or 0)
        minutes = float(unit_match.group("minutes") or 0)
        seconds = float(unit_match.group("seconds") or 0)
        return hours * 3600 + minutes * 60 + seconds

    try:
        return float(text)
    except ValueError:
        raise ValueError(f"Invalid timecode: '{text}'") from None


def _format_timecode(seconds: float) -> str:
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = seconds % 60
    return f"{hours:02d}:{minutes:02d}:{secs:06.3f}"


def build_seek_args(start: float, duration: float) -> list[str]:
    """Fast input-side seek + duration limit — place immediately before the
    video's `-i`, both together.

    Both must be INPUT options (i.e. precede `-i`), not output options.
    `-t` placed *after* `-i` only bounds the output stream's timestamps —
    which does nothing for filters like palettegen/paletteuse that only
    emit a single frame at the very end of the graph. With `-t` as an
    output option, ffmpeg keeps decoding and feeding frames into the
    filter for the rest of the file, since there's no rolling output PTS
    for it to cut off against. As an input option, `-t` instead bounds how
    much is read from the file directly, which is what actually stops it.
    """
    return ["-ss", _format_timecode(start), "-t", _format_timecode(duration)]


# No audio in any clip format (CLAUDE.md Section 6). mp4/webm are
# single-pass encodes to a scratch file rather than pipe:1 — mp4 in
# particular needs +faststart for Discord/browsers to play it inline
# progressively, which requires seeking back to rewrite the moov atom
# after encoding, something a stdout pipe can't do. webm doesn't strictly
# need this, but a scratch file keeps both non-GIF formats on one path.
_VIDEO_CODEC_ARGS: dict[str, list[str]] = {
    "mp4": ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "+faststart"],
    "webm": [
        "-c:v", "libvpx-vp9", "-b:v", "0", "-crf", "32",
        "-deadline", "realtime", "-cpu-used", "5",
    ],
}


class ClipRenderer:
    def __init__(self, fps: int, width: int, timeout_seconds: float = 60.0):
        self._fps = fps
        self._width = width
        self._timeout_seconds = timeout_seconds

    async def render_clip(
        self,
        input_path: str,
        start: float,
        duration: float,
        scratch_dir: Path,
        fmt: str = "gif",
    ) -> bytes:
        if fmt == "gif":
            return await self._render_gif(input_path, start, duration, scratch_dir)
        return await self._render_video(input_path, start, duration, scratch_dir, fmt)

    async def _render_gif(
        self,
        input_path: str,
        start: float,
        duration: float,
        scratch_dir: Path,
    ) -> bytes:
        scratch_dir.mkdir(parents=True, exist_ok=True)
        palette_path = scratch_dir / f"palette-{uuid.uuid4().hex}.png"
        scale_filter = f"scale={self._width}:-1:flags=lanczos"

        try:
            await self._run(
                [
                    "ffmpeg",
                    "-y",
                    *build_seek_args(start, duration),
                    "-i",
                    input_path,
                    "-vf",
                    f"fps={self._fps},{scale_filter},palettegen",
                    # image2 muxer needs this to write one still image
                    # rather than expecting a %d sequence pattern.
                    "-update",
                    "1",
                    str(palette_path),
                ],
                error_prefix="ffmpeg palette pass",
            )

            gif_bytes = await self._run(
                [
                    "ffmpeg",
                    *build_seek_args(start, duration),
                    "-i",
                    input_path,
                    "-i",
                    str(palette_path),
                    "-lavfi",
                    f"fps={self._fps},{scale_filter}[x];[x][1:v]paletteuse",
                    "-f",
                    "gif",
                    "pipe:1",
                ],
                error_prefix="ffmpeg encode pass",
                capture_stdout=True,
            )
            return gif_bytes or b""
        finally:
            palette_path.unlink(missing_ok=True)

    async def _render_video(
        self,
        input_path: str,
        start: float,
        duration: float,
        scratch_dir: Path,
        fmt: str,
    ) -> bytes:
        scratch_dir.mkdir(parents=True, exist_ok=True)
        out_path = scratch_dir / f"clip-{uuid.uuid4().hex}.{fmt}"
        scale_filter = f"scale={self._width}:-1:flags=lanczos"

        try:
            await self._run(
                [
                    "ffmpeg",
                    "-y",
                    *build_seek_args(start, duration),
                    "-i",
                    input_path,
                    # Explicit video-only mapping, not just -an: without it,
                    # ffmpeg's default stream selection can pull in a
                    # subtitle stream some source files have (Matroska/webm
                    # muxing is more willing to auto-include one than mp4
                    # is). Demuxing that stream has no fast-seek — same
                    # class of bug as embedded-subtitle extraction (Section
                    # 5) — so it stalls trying to read through the whole
                    # file instead of just the requested span, confirmed via
                    # a real hang against this project's own test library.
                    "-map",
                    "0:v:0",
                    # -map alone doesn't stop ffmpeg's mp4 muxer copying the
                    # source's chapter list by default (chapters aren't a
                    # "stream" -map controls) — confirmed on this project's
                    # own library: a 2s clip request produced a second
                    # "data" track whose duration matched the full film's
                    # runtime, not the clip's. -map_metadata -1 also strips
                    # title/encoder tags so the clip file doesn't carry the
                    # source film's metadata either.
                    "-map_chapters",
                    "-1",
                    "-map_metadata",
                    "-1",
                    "-vf",
                    f"fps={self._fps},{scale_filter}",
                    "-an",
                    *_VIDEO_CODEC_ARGS[fmt],
                    str(out_path),
                ],
                error_prefix=f"ffmpeg {fmt} encode",
            )
            return out_path.read_bytes()
        finally:
            out_path.unlink(missing_ok=True)

    async def _run(
        self, args: list[str], error_prefix: str, capture_stdout: bool = False
    ) -> bytes | None:
        try:
            return await run_and_capture(
                args, self._timeout_seconds, error_prefix, capture_stdout
            )
        except SubprocessTimeoutError as exc:
            raise RenderTimeoutError(str(exc)) from None
