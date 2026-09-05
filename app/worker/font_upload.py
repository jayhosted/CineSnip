from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from pathlib import Path

MAX_FONT_UPLOAD_BYTES = 5 * 1024 * 1024
_ALLOWED_EXTENSIONS = (".ttf", ".otf")
_MAGIC_PREFIXES: tuple[bytes, ...] = (
    b"OTTO",              # OpenType with CFF outlines
    b"\x00\x01\x00\x00",  # TrueType
    b"true", b"ttcf",     # older/collection TrueType variants
)
# Common dialogue punctuation a novelty/display font (the exact case this
# feature exists for — issue #20's "Simpsons font") often skips, drawing
# only letters+digits. Missing coverage degrades gracefully via libass's
# existing per-character fontconfig fallback (see the design spec's
# "Missing-glyph warning" section) — this is a warning, never a rejection.
_COMMON_DIALOGUE_PUNCTUATION = "!?.,'\"-@&()"

_FC_SCAN_TIMEOUT_SECONDS = 10.0


class FontValidationError(RuntimeError):
    pass


@dataclass(frozen=True)
class FontUploadResult:
    font_path: str
    family: str
    missing_punctuation: str


def _check_magic_bytes(data: bytes) -> None:
    if not any(data.startswith(prefix) for prefix in _MAGIC_PREFIXES):
        raise FontValidationError("That doesn't look like a .ttf/.otf font file.")


async def _fc_scan_format(path: Path, fmt: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        "fc-scan", "--format", fmt, str(path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, _ = await asyncio.wait_for(
            proc.communicate(), timeout=_FC_SCAN_TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise FontValidationError("Font validation timed out.") from None
    return stdout.decode("utf-8", errors="replace").strip()


def _decode_fc_charset(charset_output: str) -> set[int]:
    """fc-scan's %{charset} prints space-separated hex ranges ('lo-hi' or a
    bare 'lo'). Parsed defensively — an unrecognized token is skipped
    rather than raising, so an unexpected fc-scan output format degrades to
    'missing everything' (a visible warning) instead of crashing the
    upload."""
    covered: set[int] = set()
    for token in charset_output.split():
        bounds = token.split("-")
        try:
            if len(bounds) == 1:
                covered.add(int(bounds[0], 16))
            elif len(bounds) == 2:
                lo, hi = int(bounds[0], 16), int(bounds[1], 16)
                covered.update(range(lo, hi + 1))
        except ValueError:
            continue
    return covered


async def _scan_font(path: Path) -> tuple[str, str]:
    """Runs fc-scan against `path` — the same freetype parsing path libass
    itself uses — to (a) confirm it's structurally valid (catches a corrupt
    body that merely has the right magic bytes) and (b) read its charset
    for the missing-punctuation warning. Returns (family, missing_punctuation)."""
    family = await _fc_scan_format(path, "%{family}")
    if not family:
        raise FontValidationError(
            "This file couldn't be read as a font — it may be corrupt or "
            "not actually a font file."
        )
    charset_output = await _fc_scan_format(path, "%{charset}")
    covered = _decode_fc_charset(charset_output)
    missing = "".join(
        ch for ch in _COMMON_DIALOGUE_PUNCTUATION if ord(ch) not in covered
    )
    return family.split(",")[0], missing


def font_file_path(fonts_dir: Path, preset_name: str, extension: str) -> Path:
    return fonts_dir / f"{preset_name}{extension}"


async def process_font_upload(
    data: bytes, filename: str, preset_name: str, fonts_dir: Path
) -> FontUploadResult:
    if len(data) > MAX_FONT_UPLOAD_BYTES:
        raise FontValidationError(
            f"Font file is too large ({len(data) / 1_000_000:.1f}MB; max "
            f"{MAX_FONT_UPLOAD_BYTES // 1_000_000}MB)."
        )
    extension = Path(filename).suffix.lower()
    if extension not in _ALLOWED_EXTENSIONS:
        raise FontValidationError("Only .ttf and .otf font files are supported.")
    _check_magic_bytes(data)

    fonts_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = fonts_dir / f".upload-{uuid.uuid4().hex}{extension}"
    tmp_path.write_bytes(data)
    try:
        family, missing_punctuation = await _scan_font(tmp_path)
    except FontValidationError:
        tmp_path.unlink(missing_ok=True)
        raise

    final_path = font_file_path(fonts_dir, preset_name, extension)
    tmp_path.replace(final_path)
    return FontUploadResult(
        font_path=str(final_path), family=family, missing_punctuation=missing_punctuation
    )


def delete_font_file(font_path: str | None) -> None:
    if font_path:
        Path(font_path).unlink(missing_ok=True)
