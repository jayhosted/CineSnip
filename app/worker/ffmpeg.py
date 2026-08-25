from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import AsyncIterator


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


def build_seek_args(start: float) -> list[str]:
    """Fast input-side seek — place immediately before the video's `-i`."""
    return ["-ss", _format_timecode(start)]


def build_duration_args(duration: float) -> list[str]:
    """Output-side duration limit — place after ALL `-i` inputs.

    This must not sit between two `-i` flags: ffmpeg binds an option to
    whichever input follows it, so `-t` placed between the video and a
    second input (e.g. the palette image) would silently limit the wrong
    input instead of the output.
    """
    return ["-t", _format_timecode(duration)]


class ClipRenderer:
    def __init__(self, fps: int, width: int):
        self._fps = fps
        self._width = width

    async def render_gif(
        self,
        input_path: str,
        start: float,
        duration: float,
        scratch_dir: Path,
    ) -> AsyncIterator[bytes]:
        scratch_dir.mkdir(parents=True, exist_ok=True)
        palette_path = scratch_dir / f"palette-{uuid.uuid4().hex}.png"
        scale_filter = f"scale={self._width}:-1:flags=lanczos"

        try:
            await self._run(
                [
                    "ffmpeg",
                    "-y",
                    *build_seek_args(start),
                    "-i",
                    input_path,
                    *build_duration_args(duration),
                    "-vf",
                    f"fps={self._fps},{scale_filter},palettegen",
                    str(palette_path),
                ]
            )

            encode_args = [
                "ffmpeg",
                *build_seek_args(start),
                "-i",
                input_path,
                "-i",
                str(palette_path),
                *build_duration_args(duration),
                "-lavfi",
                f"fps={self._fps},{scale_filter}[x];[x][1:v]paletteuse",
                "-f",
                "gif",
                "pipe:1",
            ]
            proc = await asyncio.create_subprocess_exec(
                *encode_args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            assert proc.stdout is not None
            while True:
                chunk = await proc.stdout.read(65536)
                if not chunk:
                    break
                yield chunk

            returncode = await proc.wait()
            if returncode != 0:
                stderr = await proc.stderr.read() if proc.stderr else b""
                raise RuntimeError(
                    f"ffmpeg encode failed: {stderr.decode(errors='replace')}"
                )
        finally:
            palette_path.unlink(missing_ok=True)

    @staticmethod
    async def _run(args: list[str]) -> None:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(
                f"ffmpeg palette pass failed: {stderr.decode(errors='replace')}"
            )
