from __future__ import annotations

import json
import os
import re
import uuid
from pathlib import Path

from app.worker.crop_cache import get_cached_crop, set_cached_crop
from app.worker.subprocess_utils import SubprocessTimeoutError, run_and_capture
from app.worker.subtitle_render import StylePreset, apply_overrides, build_ass_document, entries_in_window
from app.worker.subtitles import SubtitleEntry


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

_PROBE_TIMEOUT_SECONDS = 30.0

# ffmpeg's plain `scale` filter does not tonemap — an HDR-tagged source
# (PQ/smpte2084 or HLG/arib-std-b67 transfer) passes its HDR pixel values
# through unchanged, which then render washed-out/wrong once treated as SDR
# downstream (every output format: GIF, mp4, webm). Confirmed against real
# HDR10/DV-tagged remuxes in this library (Dune (2021), Blade Runner 2049,
# Blade Runner) — see docs/build-notes/ffmpeg-rendering.md before touching
# this filter chain.
_HDR_TRANSFERS = {"smpte2084", "arib-std-b67"}
_HDR_TONEMAP_FILTER = (
    "zscale=transfer=linear:npl=100,format=gbrpf32le,zscale=primaries=bt709,"
    "tonemap=tonemap=hable:desat=0,zscale=transfer=bt709:matrix=bt709,format=yuv420p"
)

# 3D encodes pack both eyes into one frame; crop to a single eye before any
# scaling/subtitle work so the rest of the pipeline sees a normal flat frame.
# Defaults to the left/top eye — no per-request override yet (CLAUDE.md
# Section 3 flags picking the other eye as a follow-on design question, not
# blocking for getting a usable flat clip out at all).
_THREE_D_CROP_FILTERS: dict[str, str] = {
    "side_by_side": "crop=iw/2:ih:0:0",
    "over_under": "crop=iw:ih/2:0:0",
}

# A 3D pack comes in two flavors per axis, and a crop alone only produces a
# correct flat frame for one of them:
#   - "full": each eye stored at its own native resolution, so the packed
#     frame is simply double-width (side-by-side) or double-height
#     (over/under). Crop is all that's needed.
#   - "half"/squeezed: each eye compressed to fit within a normal
#     single-frame canvas (the far more common space-saving rip format),
#     so a crop alone leaves a squished eye — it must also be stretched
#     back out (2x on the squeeze axis) to its true native size.
# Confirmed against this project's own library (real bug: crop alone
# fixed Ready Player One, a Full-SBS release, but left Dune — an untagged,
# squeezed over/under rip — still squished after cropping). Neither file
# self-tags which flavor it is, but the packed frame's own raw pixel
# aspect ratio gives it away: a real single flat frame is never this wide
# (side-by-side) or this tall/square (over-under), so a pack past these
# ratios can only be two native-resolution eyes, not two squeezed ones.
# Verified: Ready Player One's Full-SBS pack is 3840x1080 (ratio 3.56,
# unambiguously full); Dune's squeezed over/under pack is 1920x1080
# (ratio 1.78, unambiguously half).
_SIDE_BY_SIDE_FULL_RATIO = 2.6
_OVER_UNDER_FULL_RATIO = 0.8


def _three_d_plan(
    three_d_format: str, width: int, height: int
) -> tuple[str | None, int, int]:
    """Returns (filter_prefix, eye_width, eye_height) for a source tagged
    with the given 3D packing. filter_prefix is None (and eye_width/
    eye_height echo the input) for flat video."""
    if three_d_format == "side_by_side":
        crop = _THREE_D_CROP_FILTERS["side_by_side"]
        if height > 0 and (width / height) >= _SIDE_BY_SIDE_FULL_RATIO:
            return f"{crop},setsar=1", width // 2, height
        # Squeezed: cropping leaves a half-width eye that's actually the
        # full canvas width's worth of image compressed into it — stretch
        # it back out. The unsqueezed eye's true width equals the packed
        # frame's own total width.
        return f"{crop},scale=iw*2:ih,setsar=1", width, height
    if three_d_format == "over_under":
        crop = _THREE_D_CROP_FILTERS["over_under"]
        if width > 0 and (height / width) >= _OVER_UNDER_FULL_RATIO:
            return f"{crop},setsar=1", width, height // 2
        return f"{crop},scale=iw:ih*2,setsar=1", width, height
    return None, width, height

# ffmpeg's own Stereo3D side-data type names, when a file actually carries
# them (e.g. Matroska's StereoMode element). Real-world per-file packing
# varies within one library (CLAUDE.md Section 3), so a per-file tag, when
# present, is trusted over the library's configured default.
_STEREO3D_TYPE_MAP: dict[str, str] = {
    "side by side": "side_by_side",
    "top and bottom": "over_under",
}


async def probe_stereo_format(input_path: str) -> str | None:
    """Returns the file's own tagged stereo packing ('side_by_side' /
    'over_under'), or None if the file carries no such tag (most rips
    don't) — callers should fall back to the library's configured default
    in that case."""
    stdout = await run_and_capture(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream_side_data_list",
            "-of",
            "json",
            input_path,
        ],
        _PROBE_TIMEOUT_SECONDS,
        error_prefix="ffprobe stereo mode",
        capture_stdout=True,
    )
    data = json.loads(stdout or b"{}")
    streams = data.get("streams") or [{}]
    for side_data in streams[0].get("side_data_list", []):
        if side_data.get("side_data_type") == "Stereo 3D":
            detected = _STEREO3D_TYPE_MAP.get(side_data.get("type"))
            if detected is not None:
                return detected
    return None


async def probe_color_transfer(input_path: str) -> str | None:
    """Returns the file's tagged color transfer function (e.g. 'smpte2084'
    for PQ, 'arib-std-b67' for HLG, 'bt709' for ordinary SDR), or None if
    the stream carries no such tag."""
    stdout = await run_and_capture(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=color_transfer",
            "-of",
            "json",
            input_path,
        ],
        _PROBE_TIMEOUT_SECONDS,
        error_prefix="ffprobe color transfer",
        capture_stdout=True,
    )
    data = json.loads(stdout or b"{}")
    streams = data.get("streams") or [{}]
    return streams[0].get("color_transfer")


def is_hdr_transfer(color_transfer: str | None) -> bool:
    return color_transfer in _HDR_TRANSFERS


# Auto-crop (issue #14): some sources bake real black letterbox/pillarbox
# bars into their own pixel data (e.g. Dune (2021)'s 4K remux, mastered as
# a 16:9 frame with a 2.39:1 image composited inside it) — a plain `scale`
# renders them faithfully since they aren't a display artifact, unlike a
# DAR flag. cropdetect's own default `limit` is already a bit-depth-aware
# fraction (not an 8-bit absolute value), so it correctly finds these bars
# on a raw 10-bit/HDR stream with no bit-depth/tonemap normalization first —
# confirmed against this library's real Dune (2021), In Bruges, Snatch
# (2000), and Magical Mystery Tour (pillarboxed, not letterboxed — same
# code path, cropdetect reports the crop in whichever axis has the bars).
# An earlier investigation's false negative on Dune came from passing an
# explicit absolute `limit=24` (an 8-bit tutorial constant) against the
# 10-bit source, not from anything HDR-specific about cropdetect itself.
_CROPDETECT_PATTERN = re.compile(r"crop=(\d+):(\d+):(\d+):(\d+)")
_CROPDETECT_PROBE_TIMEOUT_SECONDS = 30.0
_CROPDETECT_PROBE_WINDOW_SECONDS = 5.0
# Skip the first couple of minutes when possible — studio logos/black
# intro cards are a poor sample for a mastering-wide bar that's otherwise
# constant for the whole runtime.
_CROPDETECT_PROBE_SKIP_INTRO_SECONDS = 120.0


def parse_cropdetect_output(stderr_text: str) -> tuple[int, int, int, int] | None:
    """Extracts the LAST `crop=W:H:X:Y` line from cropdetect's stderr —
    cropdetect logs one (converging) line per decoded frame, so only the
    final line is the settled result."""
    matches = _CROPDETECT_PATTERN.findall(stderr_text)
    if not matches:
        return None
    w, h, x, y = matches[-1]
    return int(w), int(h), int(x), int(y)


def _crop_probe_window(
    duration: float, window: float = _CROPDETECT_PROBE_WINDOW_SECONDS
) -> tuple[float, float]:
    """Picks a representative (start, window) to sample for cropdetect.
    Never probes past the end of the file, and falls back to probing the
    whole thing (from the start) when the file is shorter than the default
    window or its duration couldn't be determined."""
    if duration <= window:
        return 0.0, duration if duration > 0 else window
    offset = min(max(duration * 0.25, _CROPDETECT_PROBE_SKIP_INTRO_SECONDS), duration - window)
    return offset, window


def _crop_box_or_none(
    box: tuple[int, int, int, int], width: int, height: int, margin: int = 2
) -> tuple[int, int, int, int] | None:
    """None when the detected box is materially the full frame (within a
    small rounding margin on every side) — cropdetect finding no bars must
    be treated as "no crop needed", not an inert crop=iw:ih filter."""
    w, h, x, y = box
    if x <= margin and y <= margin and (width - (x + w)) <= margin and (height - (y + h)) <= margin:
        return None
    return box


def _crop_adjusted_dims(
    crop_box: tuple[int, int, int, int] | None, width: int, height: int
) -> tuple[int, int]:
    """The actual pre-scale frame size scale/subtitles will draw against,
    accounting for a content crop the same way _three_d_plan's eye
    dimensions already account for a 3D crop — libass sizes burned-in text
    relative to this, so a mismatch here is the same class of bug as the
    3D squeeze issue (see build notes)."""
    if crop_box is None:
        return width, height
    return crop_box[0], crop_box[1]


async def probe_video_duration_seconds(input_path: str) -> float:
    stdout = await run_and_capture(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            input_path,
        ],
        _PROBE_TIMEOUT_SECONDS,
        error_prefix="ffprobe duration",
        capture_stdout=True,
    )
    data = json.loads(stdout or b"{}")
    duration = data.get("format", {}).get("duration")
    return float(duration) if duration is not None else 0.0


async def probe_crop(
    input_path: str, three_d_prefix: str | None = None
) -> tuple[int, int, int, int] | None:
    """Runs a cropdetect probe over a short, representative window of the
    file and returns the raw detected box (not yet checked against the
    full-frame margin — see _crop_box_or_none), or None if the probe
    couldn't determine one (timeout, failure, or no crop lines emitted).
    Any failure here degrades to "no crop", never blocks a render — same
    style as a style preset degrading to a plain render when no usable
    subtitles exist for the clip's window."""
    duration = await probe_video_duration_seconds(input_path)
    offset, window = _crop_probe_window(duration)
    crop_filter = f"{three_d_prefix},cropdetect=round=2" if three_d_prefix else "cropdetect=round=2"
    try:
        _, stderr = await run_and_capture(
            [
                "ffmpeg",
                *build_seek_args(offset, window),
                "-i",
                input_path,
                "-vf",
                crop_filter,
                "-f",
                "null",
                "-",
            ],
            _CROPDETECT_PROBE_TIMEOUT_SECONDS,
            error_prefix="ffmpeg cropdetect probe",
            capture_stderr=True,
        )
    except (SubprocessTimeoutError, RuntimeError):
        return None
    return parse_cropdetect_output(stderr.decode(errors="replace"))


async def probe_video_dimensions(input_path: str) -> tuple[int, int]:
    stdout = await run_and_capture(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-of",
            "json",
            input_path,
        ],
        _PROBE_TIMEOUT_SECONDS,
        error_prefix="ffprobe video dimensions",
        capture_stdout=True,
    )
    stream = json.loads(stdout or b"{}")["streams"][0]
    return int(stream["width"]), int(stream["height"])


def _escape_filter_path(path: Path) -> str:
    # ffmpeg's filtergraph syntax treats ':' as an option separator, so a
    # bare path breaks parsing the moment it hits one — wrapping in single
    # quotes is the standard fix, but backslashes/colons inside still need
    # escaping for the quoting itself to parse correctly.
    escaped = str(path).replace("\\", "\\\\").replace(":", "\\:")
    return f"'{escaped}'"


class ClipRenderer:
    def __init__(
        self,
        fps: int,
        width: int,
        timeout_seconds: float = 60.0,
        crop_cache_db_path: Path | None = None,
    ):
        self._fps = fps
        self._width = width
        self._timeout_seconds = timeout_seconds
        # None disables auto-crop entirely (used by tests that construct a
        # ClipRenderer directly) — the real app always passes
        # Settings.quote_index_db_path, so auto-crop is unconditionally on
        # in production (CLAUDE.md: no per-library/config gate for this,
        # unlike 3D — it doesn't change extraction semantics).
        self._crop_cache_db_path = crop_cache_db_path

    async def render_clip(
        self,
        input_path: str,
        start: float,
        duration: float,
        scratch_dir: Path,
        fmt: str = "gif",
        subtitle_entries: list[SubtitleEntry] | None = None,
        style: StylePreset | None = None,
        three_d_format: str = "none",
        subtitle_overrides: dict[int, str | None] | None = None,
        fps: int | None = None,
        width: int | None = None,
    ) -> bytes:
        # fps/width default to this renderer's configured values but can be
        # overridden per call — used by /render's size-downscale retry loop
        # (app/worker/api.py) to re-encode at progressively smaller
        # settings without needing a second ClipRenderer instance.
        fps = fps if fps is not None else self._fps
        width = width if width is not None else self._width

        # Only probe files in a library that's configured as 3D at all —
        # avoids an extra ffprobe call on every render for the (much more
        # common) normal flat-video libraries. When a file *is* tagged,
        # trust the tag over the library default (files within one 3D
        # library can use different packings — see build notes).
        if three_d_format != "none":
            detected = await probe_stereo_format(input_path)
            if detected is not None:
                three_d_format = detected

        three_d_prefix: str | None = None
        eye_width = eye_height = None
        if three_d_format != "none":
            src_width, src_height = await probe_video_dimensions(input_path)
            three_d_prefix, eye_width, eye_height = _three_d_plan(
                three_d_format, src_width, src_height
            )

        # Every source gets this check (unlike the 3D probe above, which is
        # gated behind a per-library flag) — HDR tagging is unrelated to 3D
        # and there's no equivalent per-library signal to gate it on.
        is_hdr = is_hdr_transfer(await probe_color_transfer(input_path))

        # Same "every source" treatment as HDR above — a baked-in
        # letterbox/pillarbox bar (issue #14) is unrelated to 3D and has no
        # per-library signal to gate on either.
        crop_box = await self._get_crop_box(input_path, three_d_prefix, eye_width, eye_height)
        # _crop_adjusted_dims ignores eye_width/eye_height entirely when
        # crop_box is set, so this is safe even for a flat (non-3D) source
        # where they're still None here — it falls through to crop_box's
        # own dimensions rather than losing the crop.
        frame_width, frame_height = _crop_adjusted_dims(crop_box, eye_width, eye_height)

        ass_path: Path | None = None
        if subtitle_entries and style is not None:
            ass_path = await self._write_ass_file(
                input_path, start, duration, subtitle_entries, style, scratch_dir,
                width, frame_width, frame_height, subtitle_overrides=subtitle_overrides,
            )

        try:
            if fmt == "gif":
                return await self._render_gif(
                    input_path, start, duration, scratch_dir, fps, width, ass_path,
                    three_d_prefix, is_hdr, crop_box,
                )
            return await self._render_video(
                input_path, start, duration, scratch_dir, fmt, fps, width, ass_path,
                three_d_prefix, is_hdr, crop_box,
            )
        finally:
            if ass_path is not None:
                ass_path.unlink(missing_ok=True)

    async def _get_crop_box(
        self,
        input_path: str,
        three_d_prefix: str | None,
        eye_width: int | None,
        eye_height: int | None,
    ) -> tuple[int, int, int, int] | None:
        if self._crop_cache_db_path is None:
            return None
        try:
            stat = os.stat(input_path)
        except OSError:
            return None
        fingerprint = (stat.st_mtime, stat.st_size)

        cached = get_cached_crop(self._crop_cache_db_path, input_path, fingerprint)
        if cached is not None:
            return cached.crop_box

        frame_width, frame_height = eye_width, eye_height
        if frame_width is None or frame_height is None:
            frame_width, frame_height = await probe_video_dimensions(input_path)

        detected = await probe_crop(input_path, three_d_prefix)
        crop_box = _crop_box_or_none(detected, frame_width, frame_height) if detected else None

        set_cached_crop(self._crop_cache_db_path, input_path, crop_box, fingerprint)
        return crop_box

    async def _write_ass_file(
        self,
        input_path: str,
        start: float,
        duration: float,
        entries: list[SubtitleEntry],
        style: StylePreset,
        scratch_dir: Path,
        width: int,
        eye_width: int | None = None,
        eye_height: int | None = None,
        subtitle_overrides: dict[int, str | None] | None = None,
    ) -> Path:
        # A 3D source's *encoded* frame packs both eyes together — the
        # single-eye frame the crop (and, for a squeezed pack, unsqueeze)
        # filter actually hands to scale/subtitles has different dimensions,
        # and PlayResY must match what libass will actually render against,
        # not the raw source frame. render_clip works this out via
        # _three_d_plan() and passes it down; a flat (non-3D) source has no
        # pre-computed dimensions, so probe directly.
        if eye_width is None or eye_height is None:
            eye_width, eye_height = await probe_video_dimensions(input_path)
        out_width = width
        out_height = round(out_width * eye_height / eye_width)
        out_height -= out_height % 2  # matches the -2 (even-height) scale filter below

        window = entries_in_window(entries, start, start + duration)
        if subtitle_overrides:
            window = apply_overrides(window, subtitle_overrides)
        doc = build_ass_document(window, style, out_width, out_height)

        scratch_dir.mkdir(parents=True, exist_ok=True)
        ass_path = scratch_dir / f"subs-{uuid.uuid4().hex}.ass"
        ass_path.write_text(doc, encoding="utf-8")
        return ass_path

    def _scale_and_subtitle_filter(
        self,
        width: int,
        ass_path: Path | None,
        three_d_prefix: str | None = None,
        is_hdr: bool = False,
        crop_box: tuple[int, int, int, int] | None = None,
    ) -> str:
        # -2 (not -1) guarantees an even output height, matching the
        # rounding _write_ass_file uses to compute PlayResY — a mismatch
        # there would make burned-in text the wrong size relative to the
        # actual frame, not just misplaced.
        filters = []
        if three_d_prefix:
            filters.append(three_d_prefix)
        if crop_box is not None:
            w, h, x, y = crop_box
            filters.append(f"crop={w}:{h}:{x}:{y}")
        if is_hdr:
            filters.append(_HDR_TONEMAP_FILTER)
        filters.append(f"scale={width}:-2:flags=lanczos")
        if ass_path is not None:
            filters.append(f"subtitles={_escape_filter_path(ass_path)}")
        return ",".join(filters)

    async def _render_gif(
        self,
        input_path: str,
        start: float,
        duration: float,
        scratch_dir: Path,
        fps: int,
        width: int,
        ass_path: Path | None = None,
        three_d_prefix: str | None = None,
        is_hdr: bool = False,
        crop_box: tuple[int, int, int, int] | None = None,
    ) -> bytes:
        scratch_dir.mkdir(parents=True, exist_ok=True)
        palette_path = scratch_dir / f"palette-{uuid.uuid4().hex}.png"
        scale_filter = self._scale_and_subtitle_filter(width, ass_path, three_d_prefix, is_hdr, crop_box)

        try:
            await self._run(
                [
                    "ffmpeg",
                    "-y",
                    *build_seek_args(start, duration),
                    "-i",
                    input_path,
                    "-vf",
                    f"fps={fps},{scale_filter},palettegen",
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
                    f"fps={fps},{scale_filter}[x];[x][1:v]paletteuse",
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
        fps: int,
        width: int,
        ass_path: Path | None = None,
        three_d_prefix: str | None = None,
        is_hdr: bool = False,
        crop_box: tuple[int, int, int, int] | None = None,
    ) -> bytes:
        scratch_dir.mkdir(parents=True, exist_ok=True)
        out_path = scratch_dir / f"clip-{uuid.uuid4().hex}.{fmt}"
        scale_filter = self._scale_and_subtitle_filter(width, ass_path, three_d_prefix, is_hdr, crop_box)

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
                    # muxing is more willing to auto-include one than mp4).
                    # Demuxing it has no fast-seek — same class of bug as
                    # embedded-subtitle extraction (Section 5) — and stalls
                    # reading the whole file instead of the requested span.
                    "-map",
                    "0:v:0",
                    # -map alone doesn't stop ffmpeg's mp4 muxer copying the
                    # source's chapter list by default (chapters aren't a
                    # "stream" -map controls) — leaks the full film's
                    # runtime into a short clip's metadata otherwise.
                    # -map_metadata -1 also strips title/encoder tags.
                    "-map_chapters",
                    "-1",
                    "-map_metadata",
                    "-1",
                    "-vf",
                    f"fps={fps},{scale_filter}",
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
