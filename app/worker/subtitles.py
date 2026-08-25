from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

import srt

from app.worker.plex_client import MovieResult
from app.worker.subprocess_utils import run_and_capture

logger = logging.getLogger("cinesnip.subtitles")

_LANGUAGE_PREFERENCE = ("en", "eng")
_HI_MARKERS = {"hi", "sdh", "cc"}
_ENCODING_FALLBACKS = ("utf-8-sig", "utf-8", "cp1252", "latin-1")

# Bitmap subtitle formats (PGS, VOBSUB, DVB) can't be muxed to SRT text by
# ffmpeg — common on Blu-ray rips, and correctly excluded here rather than
# producing a garbage extraction.
_TEXT_SUBTITLE_CODECS = {"subrip", "srt", "ass", "ssa", "mov_text", "webvtt", "text"}

_SDH_PATTERN = re.compile(r"\bsdh\b|\bcc\b", re.IGNORECASE)


class SubtitleSource(str, Enum):
    SIDECAR = "sidecar"
    EMBEDDED = "embedded"
    NONE = "none"


class SubtitleParseError(RuntimeError):
    pass


@dataclass
class SubtitleEntry:
    index: int
    start: float
    end: float
    text: str


@dataclass
class SubtitleStreamInfo:
    relative_index: int
    codec_name: str
    language: str | None
    title: str | None
    forced: bool
    hearing_impaired: bool


@dataclass
class SubtitleResult:
    guid: str
    source: SubtitleSource
    entries: list[SubtitleEntry] = field(default_factory=list)
    sidecar_path: str | None = None
    stream_index: int | None = None


# --- SRT parsing -------------------------------------------------------


def parse_srt(text: str) -> list[SubtitleEntry]:
    try:
        subs = list(srt.parse(text))
    except Exception as exc:
        raise SubtitleParseError(f"Failed to parse SRT content: {exc}") from exc

    return [
        SubtitleEntry(
            index=sub.index,
            start=sub.start.total_seconds(),
            end=sub.end.total_seconds(),
            text=sub.content,
        )
        for sub in subs
    ]


# --- Sidecar subtitle discovery -----------------------------------------


def _parse_sidecar_suffix(sidecar_path: Path, video_stem: str) -> tuple[str | None, bool]:
    """Parse a sidecar filename's suffix into (language, is_hearing_impaired).

    Bazarr-style sidecars often chain multiple dot-separated markers after
    the language code, e.g. "Film.en.hi.srt" (English, hearing-impaired/SDH)
    as distinct from a plain "Film.en.srt". The language is always the
    first segment; any later segment matching a known hi/SDH marker flags
    the file as hearing-impaired.
    """
    name = sidecar_path.name
    middle = name[len(video_stem) : -len(".srt")].strip(".")
    if not middle:
        return None, False
    parts = [p.lower() for p in middle.split(".") if p]
    language = parts[0] if parts else None
    hearing_impaired = any(p in _HI_MARKERS for p in parts[1:])
    return language, hearing_impaired


def find_sidecar_subtitle(video_path: Path) -> Path | None:
    folder = video_path.parent
    if not folder.is_dir():
        return None

    exact = video_path.with_suffix(".srt")
    if exact.exists():
        return exact
    exact_upper = video_path.with_suffix(".SRT")
    if exact_upper.exists():
        return exact_upper

    stem = video_path.stem
    candidates = [
        p
        for p in folder.iterdir()
        if p.is_file()
        and p.name.lower().startswith(f"{stem.lower()}.")
        and p.name.lower().endswith(".srt")
    ]
    if not candidates:
        return None

    parsed = [(c, *_parse_sidecar_suffix(c, stem)) for c in candidates]

    # Prefer a preferred-language match; among those (or, failing that,
    # the whole pool), prefer a non-hearing-impaired/non-SDH file, since
    # that's the documented default (CLAUDE.md Section 5).
    pool = [p for p in parsed if p[1] in _LANGUAGE_PREFERENCE] or parsed
    pool = [p for p in pool if not p[2]] or pool

    return sorted(pool, key=lambda p: p[0].name)[0][0]


def read_sidecar_srt(path: Path) -> str:
    raw = path.read_bytes()
    for encoding in _ENCODING_FALLBACKS:
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    # latin-1 above never fails (maps every byte 0-255), so this is
    # unreachable in practice — kept as a defensive final fallback.
    return raw.decode("latin-1", errors="replace")


# --- Embedded subtitle stream probing/extraction ------------------------


def build_ffprobe_subtitle_args(video_path: str) -> list[str]:
    return [
        "ffprobe",
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_streams",
        "-select_streams",
        "s",
        video_path,
    ]


async def probe_subtitle_streams(
    video_path: str, timeout_seconds: float = 180.0
) -> list[SubtitleStreamInfo]:
    stdout = await run_and_capture(
        build_ffprobe_subtitle_args(video_path),
        timeout_seconds,
        error_prefix="ffprobe subtitle probe",
        capture_stdout=True,
    )
    data = json.loads(stdout or b"{}")
    streams = data.get("streams", [])

    result = []
    for relative_index, stream in enumerate(streams):
        tags = stream.get("tags", {})
        disposition = stream.get("disposition", {})
        result.append(
            SubtitleStreamInfo(
                relative_index=relative_index,
                codec_name=stream.get("codec_name", ""),
                language=tags.get("language"),
                title=tags.get("title"),
                forced=bool(disposition.get("forced")),
                hearing_impaired=bool(disposition.get("hearing_impaired")),
            )
        )
    return result


def choose_subtitle_stream(
    streams: list[SubtitleStreamInfo],
) -> SubtitleStreamInfo | None:
    for stream in streams:
        if stream.codec_name.lower() not in _TEXT_SUBTITLE_CODECS:
            continue
        if stream.forced:
            continue
        if stream.hearing_impaired:
            continue
        if stream.title and _SDH_PATTERN.search(stream.title):
            continue
        return stream
    return None


def build_ffmpeg_extract_args(video_path: str, stream_relative_index: int) -> list[str]:
    return [
        "ffmpeg",
        "-v",
        "error",
        "-i",
        video_path,
        "-map",
        f"0:s:{stream_relative_index}",
        "-f",
        "srt",
        "pipe:1",
    ]


async def extract_embedded_subtitle(
    video_path: str, stream_relative_index: int, timeout_seconds: float = 180.0
) -> str:
    stdout = await run_and_capture(
        build_ffmpeg_extract_args(video_path, stream_relative_index),
        timeout_seconds,
        error_prefix="ffmpeg subtitle extraction",
        capture_stdout=True,
    )
    return (stdout or b"").decode("utf-8", errors="replace")


# --- Cache ----------------------------------------------------------------


def _cache_path_for_guid(cache_dir: Path, guid: str) -> Path:
    digest = hashlib.sha256(guid.encode("utf-8")).hexdigest()
    return cache_dir / f"{digest}.json"


def read_cached_subtitles(cache_dir: Path, guid: str) -> SubtitleResult | None:
    path = _cache_path_for_guid(cache_dir, guid)
    if not path.exists():
        return None

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return SubtitleResult(
            guid=payload["guid"],
            source=SubtitleSource(payload["source"]),
            entries=[SubtitleEntry(**e) for e in payload["entries"]],
            sidecar_path=payload.get("sidecar_path"),
            stream_index=payload.get("stream_index"),
        )
    except (json.JSONDecodeError, KeyError, ValueError, TypeError) as exc:
        logger.warning("Ignoring corrupt subtitle cache file %s: %s", path, exc)
        return None


def write_cached_subtitles(cache_dir: Path, result: SubtitleResult) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    final_path = _cache_path_for_guid(cache_dir, result.guid)

    payload = {
        "guid": result.guid,
        "source": result.source.value,
        "sidecar_path": result.sidecar_path,
        "stream_index": result.stream_index,
        "cached_at": datetime.now(timezone.utc).isoformat(),
        "entries": [
            {"index": e.index, "start": e.start, "end": e.end, "text": e.text}
            for e in result.entries
        ],
    }

    tmp_path = final_path.with_suffix(f".json.tmp-{uuid.uuid4().hex}")
    tmp_path.write_text(json.dumps(payload), encoding="utf-8")
    tmp_path.replace(final_path)


# --- Orchestration ----------------------------------------------------


async def get_subtitles(
    movie: MovieResult,
    container_video_path: str,
    cache_dir: Path,
    ffprobe_timeout: float = 180.0,
    ffmpeg_timeout: float = 180.0,
) -> SubtitleResult:
    cached = read_cached_subtitles(cache_dir, movie.guid)
    if cached is not None:
        return cached

    video_path = Path(container_video_path)

    sidecar = find_sidecar_subtitle(video_path)
    if sidecar is not None:
        try:
            entries = parse_srt(read_sidecar_srt(sidecar))
        except SubtitleParseError as exc:
            logger.warning("Sidecar subtitle %s failed to parse: %s", sidecar, exc)
            entries = []

        if entries:
            result = SubtitleResult(
                guid=movie.guid,
                source=SubtitleSource.SIDECAR,
                entries=entries,
                sidecar_path=str(sidecar),
            )
            write_cached_subtitles(cache_dir, result)
            return result

    streams = await probe_subtitle_streams(container_video_path, ffprobe_timeout)
    chosen = choose_subtitle_stream(streams)
    if chosen is not None:
        srt_text = await extract_embedded_subtitle(
            container_video_path, chosen.relative_index, ffmpeg_timeout
        )
        try:
            entries = parse_srt(srt_text)
        except SubtitleParseError as exc:
            logger.warning(
                "Embedded subtitle stream %s failed to parse: %s",
                chosen.relative_index,
                exc,
            )
            entries = []

        if entries:
            result = SubtitleResult(
                guid=movie.guid,
                source=SubtitleSource.EMBEDDED,
                entries=entries,
                stream_index=chosen.relative_index,
            )
            write_cached_subtitles(cache_dir, result)
            return result

    result = SubtitleResult(guid=movie.guid, source=SubtitleSource.NONE, entries=[])
    write_cached_subtitles(cache_dir, result)
    return result
