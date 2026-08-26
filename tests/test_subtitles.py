import json

import pytest

from app.worker.subtitles import (
    SubtitleParseError,
    SubtitleStreamInfo,
    build_ffmpeg_extract_args,
    build_ffprobe_subtitle_args,
    choose_subtitle_stream,
    find_sidecar_subtitle,
    parse_srt,
    read_cached_subtitles,
    read_sidecar_srt,
    write_cached_subtitles,
)
from app.worker.subtitles import SubtitleResult, SubtitleSource, SubtitleEntry


# --- parse_srt -----------------------------------------------------------


def test_parse_srt_returns_entries_in_seconds():
    text = (
        "1\n00:00:01,000 --> 00:00:02,500\nHello world\n\n"
        "2\n00:00:03,000 --> 00:00:05,000\nGoodbye\n"
    )
    entries = parse_srt(text)
    assert [e.text for e in entries] == ["Hello world", "Goodbye"]
    assert entries[0].start == 1.0
    assert entries[0].end == 2.5


def test_parse_srt_preserves_multiline_cue():
    text = "1\n00:00:01,000 --> 00:00:02,000\nLine one\nLine two\n"
    entries = parse_srt(text)
    assert entries[0].text == "Line one\nLine two"


def test_parse_srt_preserves_out_of_order_entries_without_reordering():
    text = (
        "1\n00:00:10,000 --> 00:00:11,000\nLater line\n\n"
        "2\n00:00:01,000 --> 00:00:02,000\nEarlier line\n"
    )
    entries = parse_srt(text)
    assert [e.text for e in entries] == ["Later line", "Earlier line"]


def test_parse_srt_rejects_garbage():
    with pytest.raises(SubtitleParseError):
        parse_srt("this is not an srt file at all")


# --- find_sidecar_subtitle -------------------------------------------------


def test_find_sidecar_subtitle_prefers_exact_match(tmp_path):
    video = tmp_path / "Film.mkv"
    video.touch()
    (tmp_path / "Film.srt").write_text("exact")
    (tmp_path / "Film.en.srt").write_text("suffixed")

    result = find_sidecar_subtitle(video)
    assert result == tmp_path / "Film.srt"


def test_find_sidecar_subtitle_prefers_english_among_suffixed_variants(tmp_path):
    video = tmp_path / "Film.mkv"
    video.touch()
    (tmp_path / "Film.fr.srt").write_text("french")
    (tmp_path / "Film.en.srt").write_text("english")

    result = find_sidecar_subtitle(video)
    assert result == tmp_path / "Film.en.srt"


def test_find_sidecar_subtitle_prefers_plain_over_hearing_impaired_variant(tmp_path):
    video = tmp_path / "Film.mkv"
    video.touch()
    (tmp_path / "Film.en.hi.srt").write_text("hearing impaired")
    (tmp_path / "Film.en.srt").write_text("plain")

    result = find_sidecar_subtitle(video)
    assert result == tmp_path / "Film.en.srt"


def test_find_sidecar_subtitle_accepts_hearing_impaired_variant_when_its_the_only_option(
    tmp_path,
):
    video = tmp_path / "Film.mkv"
    video.touch()
    (tmp_path / "Film.en.hi.srt").write_text("hearing impaired only")

    result = find_sidecar_subtitle(video)
    assert result == tmp_path / "Film.en.hi.srt"


def test_find_sidecar_subtitle_falls_back_to_alphabetical_order(tmp_path):
    video = tmp_path / "Film.mkv"
    video.touch()
    (tmp_path / "Film.fr.srt").write_text("french")
    (tmp_path / "Film.de.srt").write_text("german")

    result = find_sidecar_subtitle(video)
    assert result == tmp_path / "Film.de.srt"


def test_find_sidecar_subtitle_returns_none_when_nothing_matches(tmp_path):
    video = tmp_path / "Film.mkv"
    video.touch()

    assert find_sidecar_subtitle(video) is None


# --- read_sidecar_srt ------------------------------------------------------


def test_read_sidecar_srt_strips_utf8_bom(tmp_path):
    path = tmp_path / "Film.srt"
    path.write_bytes("﻿1\n00:00:01,000 --> 00:00:02,000\nHi\n".encode("utf-8"))

    text = read_sidecar_srt(path)
    assert not text.startswith("﻿")


def test_read_sidecar_srt_falls_back_to_latin1(tmp_path):
    path = tmp_path / "Film.srt"
    path.write_bytes("café".encode("latin-1"))

    text = read_sidecar_srt(path)
    assert "caf" in text


# --- cache round-trip -------------------------------------------------------


def test_cache_round_trip(tmp_path):
    result = SubtitleResult(
        guid="plex://movie/abc",
        source=SubtitleSource.SIDECAR,
        entries=[SubtitleEntry(index=1, start=1.0, end=2.0, text="Hi")],
        sidecar_path="/media/movies-d/Film.srt",
    )
    write_cached_subtitles(tmp_path, result)

    loaded = read_cached_subtitles(tmp_path, "plex://movie/abc")
    assert loaded == result


def test_cache_miss_for_unwritten_guid(tmp_path):
    assert read_cached_subtitles(tmp_path, "plex://movie/never-written") is None


def test_cache_ignores_corrupt_file(tmp_path):
    from app.worker.subtitles import _cache_path_for_guid

    path = _cache_path_for_guid(tmp_path, "plex://movie/corrupt")
    path.write_text("{not valid json")

    assert read_cached_subtitles(tmp_path, "plex://movie/corrupt") is None


def test_cache_is_invalidated_when_the_sidecar_file_changes(tmp_path):
    # A Bazarr re-sync or a manual fix (e.g. alass) replaces the sidecar's
    # content without necessarily touching its filename or the cached GUID
    # key — without a freshness check, the stale cached entries would be
    # served forever. See CLAUDE.md's subtitle-extraction build notes.
    sidecar = tmp_path / "Film.srt"
    sidecar.write_text("original")
    video = tmp_path / "Film.mkv"
    video.write_text("video bytes")

    result = SubtitleResult(
        guid="plex://movie/abc",
        source=SubtitleSource.SIDECAR,
        entries=[SubtitleEntry(index=1, start=1.0, end=2.0, text="Hi")],
        sidecar_path=str(sidecar),
    )
    write_cached_subtitles(tmp_path, result, sidecar)

    assert read_cached_subtitles(tmp_path, "plex://movie/abc", sidecar, video) == result

    sidecar.write_text("resynced content, different mtime/size")
    assert read_cached_subtitles(tmp_path, "plex://movie/abc", sidecar, video) is None


def test_cache_is_invalidated_when_the_sidecar_is_removed(tmp_path):
    sidecar = tmp_path / "Film.srt"
    sidecar.write_text("original")
    video = tmp_path / "Film.mkv"
    video.write_text("video bytes")

    result = SubtitleResult(
        guid="plex://movie/abc",
        source=SubtitleSource.SIDECAR,
        entries=[SubtitleEntry(index=1, start=1.0, end=2.0, text="Hi")],
        sidecar_path=str(sidecar),
    )
    write_cached_subtitles(tmp_path, result, sidecar)

    sidecar.unlink()
    assert read_cached_subtitles(tmp_path, "plex://movie/abc", None, video) is None


def test_cache_for_embedded_source_is_not_invalidated_by_an_unrelated_sidecar_appearing(
    tmp_path,
):
    # A title cached as EMBEDDED has no sidecar involved in producing it —
    # a sidecar showing up afterwards shouldn't be treated as making that
    # cache stale (a different, deliberate re-check would be needed to
    # actually prefer the new sidecar).
    video = tmp_path / "Film.mkv"
    video.write_text("video bytes")

    result = SubtitleResult(
        guid="plex://movie/abc",
        source=SubtitleSource.EMBEDDED,
        entries=[SubtitleEntry(index=1, start=1.0, end=2.0, text="Hi")],
        stream_index=0,
    )
    write_cached_subtitles(tmp_path, result, video)

    new_sidecar = tmp_path / "Film.srt"
    new_sidecar.write_text("a sidecar that didn't exist when this was cached")

    assert (
        read_cached_subtitles(tmp_path, "plex://movie/abc", new_sidecar, video) == result
    )


def test_cache_for_embedded_source_is_invalidated_when_the_video_changes(tmp_path):
    video = tmp_path / "Film.mkv"
    video.write_text("video bytes")

    result = SubtitleResult(
        guid="plex://movie/abc",
        source=SubtitleSource.EMBEDDED,
        entries=[SubtitleEntry(index=1, start=1.0, end=2.0, text="Hi")],
        stream_index=0,
    )
    write_cached_subtitles(tmp_path, result, video)

    video.write_text("a re-remuxed file with different bytes")
    assert read_cached_subtitles(tmp_path, "plex://movie/abc", None, video) is None


def test_cache_paths_for_special_character_guids_are_filesystem_safe(tmp_path):
    from app.worker.subtitles import _cache_path_for_guid

    guid_a = "plex://movie/abc"
    guid_b = "com.plexapp.agents.imdb://tt0111161?lang=en"

    path_a = _cache_path_for_guid(tmp_path, guid_a)
    path_b = _cache_path_for_guid(tmp_path, guid_b)

    assert path_a != path_b
    for path in (path_a, path_b):
        assert "/" not in path.name
        assert ":" not in path.name
        assert "?" not in path.name


# --- choose_subtitle_stream -------------------------------------------------


def _stream(**overrides):
    defaults = dict(
        relative_index=0,
        codec_name="subrip",
        language="eng",
        title=None,
        forced=False,
        hearing_impaired=False,
    )
    defaults.update(overrides)
    return SubtitleStreamInfo(**defaults)


def test_choose_subtitle_stream_skips_forced():
    streams = [_stream(relative_index=0, forced=True), _stream(relative_index=1)]
    assert choose_subtitle_stream(streams).relative_index == 1


def test_choose_subtitle_stream_skips_hearing_impaired():
    streams = [
        _stream(relative_index=0, hearing_impaired=True),
        _stream(relative_index=1),
    ]
    assert choose_subtitle_stream(streams).relative_index == 1


def test_choose_subtitle_stream_skips_sdh_titled_stream_without_flag():
    streams = [
        _stream(relative_index=0, title="English SDH"),
        _stream(relative_index=1),
    ]
    assert choose_subtitle_stream(streams).relative_index == 1


def test_choose_subtitle_stream_skips_bitmap_codec():
    streams = [
        _stream(relative_index=0, codec_name="hdmv_pgs_subtitle"),
        _stream(relative_index=1, codec_name="subrip"),
    ]
    assert choose_subtitle_stream(streams).relative_index == 1


def test_choose_subtitle_stream_returns_none_when_all_filtered():
    streams = [_stream(forced=True), _stream(hearing_impaired=True)]
    assert choose_subtitle_stream(streams) is None


# --- argv builders -----------------------------------------------------


def test_build_ffprobe_subtitle_args():
    assert build_ffprobe_subtitle_args("/media/movies-d/film.mkv") == [
        "ffprobe",
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_streams",
        "-select_streams",
        "s",
        "/media/movies-d/film.mkv",
    ]


def test_build_ffmpeg_extract_args():
    args = build_ffmpeg_extract_args("/media/movies-d/film.mkv", 2)
    assert args == [
        "ffmpeg",
        "-v",
        "error",
        "-i",
        "/media/movies-d/film.mkv",
        "-map",
        "0:s:2",
        "-f",
        "srt",
        "pipe:1",
    ]
    map_index = args.index("-map")
    assert args[map_index + 1] == "0:s:2"
