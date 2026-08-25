from __future__ import annotations

import asyncio
import uuid
from pathlib import Path


class RenderTimeoutError(RuntimeError):
    pass


def parse_timecode(text: str) -> float:
    """Parse 'HH:MM:SS', 'MM:SS', or a plain number of seconds into seconds."""
    text = text.strip()
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


class ClipRenderer:
    def __init__(self, fps: int, width: int, timeout_seconds: float = 60.0):
        self._fps = fps
        self._width = width
        self._timeout_seconds = timeout_seconds

    async def render_gif(
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

    async def _run(
        self, args: list[str], error_prefix: str, capture_stdout: bool = False
    ) -> bytes | None:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            # communicate() drains stdout and stderr concurrently, avoiding
            # the deadlock that follows from only reading one pipe while
            # ffmpeg blocks writing to the other once its buffer fills.
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=self._timeout_seconds
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise RenderTimeoutError(
                f"{error_prefix} timed out after {self._timeout_seconds:.0f}s "
                f"— this source file may be unusually slow to seek/decode."
            ) from None

        if proc.returncode != 0:
            raise RuntimeError(
                f"{error_prefix} failed: {stderr.decode(errors='replace')}"
            )
        return stdout if capture_stdout else None
