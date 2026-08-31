import asyncio
import json

import pytest

from app.worker import search_index
from app.worker.plex_client import MovieResult
from app.worker.subtitles import (
    SubtitleParseError,
    SubtitleStreamInfo,
    build_ffmpeg_extract_args,
    build_ffprobe_subtitle_args,
    choose_subtitle_stream,
    delete_cached_subtitles,
    find_sidecar_subtitle,
    get_subtitles,
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


# --- cache round-trip (legacy JSON-file helpers) ---------------------------
#
# cache_path_for_guid/read_cached_subtitles/write_cached_subtitles/
# delete_cached_subtitles themselves are untouched by the search_index
# migration (Task 3 only swaps get_subtitles()'s own persistence — see the
# "get_subtitles() orchestration" section below) because library_sync.py,
# quotes.py, library_search.py, and api.py's /subtitles diagnostic route
# still call them directly for their own JSON-cache-based logic; migrating
# those is later tasks' scope. These tests keep covering that they still
# work exactly as before.


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
    from app.worker.subtitles import cache_path_for_guid

    path = cache_path_for_guid(tmp_path, "plex://movie/corrupt")
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
    from app.worker.subtitles import cache_path_for_guid

    guid_a = "plex://movie/abc"
    guid_b = "com.plexapp.agents.imdb://tt0111161?lang=en"

    path_a = cache_path_for_guid(tmp_path, guid_a)
    path_b = cache_path_for_guid(tmp_path, guid_b)

    assert path_a != path_b
    for path in (path_a, path_b):
        assert "/" not in path.name
        assert ":" not in path.name
        assert "?" not in path.name


# --- get_subtitles() orchestration (search_index-backed) -------------------


def _movie(guid="plex://movie/abc", media_id="101", title="Film", library_name="Movies"):
    return MovieResult(
        media_id=media_id,
        title=title,
        year=2000,
        duration_ms=1000,
        thumb_url=None,
        source_path="D:\\Movies\\Film.mkv",
        guid=guid,
        library_name=library_name,
    )


async def _no_embedded_streams(*args, **kwargs):
    return []


def test_get_subtitles_caches_sidecar_result_in_search_index(tmp_path):
    video = tmp_path / "Film.mkv"
    video.write_text("video bytes")
    sidecar = tmp_path / "Film.srt"
    sidecar.write_text("1\n00:00:01,000 --> 00:00:02,000\nHello\n")
    db_path = tmp_path / "search_index.db"
    movie = _movie()

    result = asyncio.run(get_subtitles(movie, str(video), tmp_path, db_path))

    assert result.source is SubtitleSource.SIDECAR
    assert [e.text for e in result.entries] == ["Hello"]
    assert result.sidecar_path == str(sidecar)

    # Written straight into search_index, not a JSON file.
    assert search_index.get_entries(db_path, movie.guid) == result.entries


def test_get_subtitles_sidecar_result_is_a_true_cache_hit_on_second_call(
    tmp_path, monkeypatch
):
    video = tmp_path / "Film.mkv"
    video.write_text("video bytes")
    sidecar = tmp_path / "Film.srt"
    sidecar.write_text("1\n00:00:01,000 --> 00:00:02,000\nHello\n")
    db_path = tmp_path / "search_index.db"
    movie = _movie()

    first = asyncio.run(get_subtitles(movie, str(video), tmp_path, db_path))

    def _boom(path):
        raise AssertionError("sidecar should not be re-read on a cache hit")

    monkeypatch.setattr("app.worker.subtitles.read_sidecar_srt", _boom)

    second = asyncio.run(get_subtitles(movie, str(video), tmp_path, db_path))
    assert second == first


def test_get_subtitles_sidecar_cache_invalidated_when_sidecar_changes(tmp_path):
    video = tmp_path / "Film.mkv"
    video.write_text("video bytes")
    sidecar = tmp_path / "Film.srt"
    sidecar.write_text("1\n00:00:01,000 --> 00:00:02,000\nOriginal\n")
    db_path = tmp_path / "search_index.db"
    movie = _movie()

    first = asyncio.run(get_subtitles(movie, str(video), tmp_path, db_path))
    assert [e.text for e in first.entries] == ["Original"]

    sidecar.write_text("1\n00:00:01,000 --> 00:00:02,000\nResynced\n")
    second = asyncio.run(get_subtitles(movie, str(video), tmp_path, db_path))
    assert [e.text for e in second.entries] == ["Resynced"]


def test_get_subtitles_sidecar_cache_invalidated_when_sidecar_removed(
    tmp_path, monkeypatch
):
    video = tmp_path / "Film.mkv"
    video.write_text("video bytes")
    sidecar = tmp_path / "Film.srt"
    sidecar.write_text("1\n00:00:01,000 --> 00:00:02,000\nHello\n")
    db_path = tmp_path / "search_index.db"
    movie = _movie()

    first = asyncio.run(get_subtitles(movie, str(video), tmp_path, db_path))
    assert first.source is SubtitleSource.SIDECAR

    sidecar.unlink()
    monkeypatch.setattr(
        "app.worker.subtitles.probe_subtitle_streams", _no_embedded_streams
    )

    second = asyncio.run(get_subtitles(movie, str(video), tmp_path, db_path))
    assert second.source is SubtitleSource.NONE
    assert second.entries == []


def test_get_subtitles_embedded_cache_not_invalidated_by_unrelated_sidecar_appearing(
    tmp_path, monkeypatch
):
    video = tmp_path / "Film.mkv"
    video.write_text("video bytes")
    db_path = tmp_path / "search_index.db"
    movie = _movie()

    stream = SubtitleStreamInfo(
        relative_index=0,
        codec_name="subrip",
        language="eng",
        title=None,
        forced=False,
        hearing_impaired=False,
    )

    async def _one_stream(*args, **kwargs):
        return [stream]

    async def _extracted_text(*args, **kwargs):
        return "1\n00:00:01,000 --> 00:00:02,000\nEmbedded line\n"

    monkeypatch.setattr("app.worker.subtitles.probe_subtitle_streams", _one_stream)
    monkeypatch.setattr(
        "app.worker.subtitles.extract_embedded_subtitle", _extracted_text
    )

    first = asyncio.run(get_subtitles(movie, str(video), tmp_path, db_path))
    assert first.source is SubtitleSource.EMBEDDED

    # A sidecar shows up afterwards, unrelated to what produced the cached
    # EMBEDDED result — must not be treated as invalidating it.
    (tmp_path / "Film.srt").write_text("a sidecar that didn't exist when cached")

    async def _boom(*args, **kwargs):
        raise AssertionError("embedded subtitle should not be re-extracted")

    monkeypatch.setattr("app.worker.subtitles.extract_embedded_subtitle", _boom)

    second = asyncio.run(get_subtitles(movie, str(video), tmp_path, db_path))
    assert second == first


def test_get_subtitles_embedded_cache_invalidated_when_video_changes(
    tmp_path, monkeypatch
):
    video = tmp_path / "Film.mkv"
    video.write_text("video bytes")
    db_path = tmp_path / "search_index.db"
    movie = _movie()

    stream = SubtitleStreamInfo(
        relative_index=0,
        codec_name="subrip",
        language="eng",
        title=None,
        forced=False,
        hearing_impaired=False,
    )

    async def _one_stream(*args, **kwargs):
        return [stream]

    async def _extracted_text(*args, **kwargs):
        return "1\n00:00:01,000 --> 00:00:02,000\nEmbedded line\n"

    monkeypatch.setattr("app.worker.subtitles.probe_subtitle_streams", _one_stream)
    monkeypatch.setattr(
        "app.worker.subtitles.extract_embedded_subtitle", _extracted_text
    )

    first = asyncio.run(get_subtitles(movie, str(video), tmp_path, db_path))
    assert first.source is SubtitleSource.EMBEDDED

    video.write_text("a re-remuxed file with different bytes")

    reextracted = False

    async def _extracted_text_v2(*args, **kwargs):
        nonlocal reextracted
        reextracted = True
        return "1\n00:00:01,000 --> 00:00:02,000\nRe-extracted line\n"

    monkeypatch.setattr(
        "app.worker.subtitles.extract_embedded_subtitle", _extracted_text_v2
    )

    second = asyncio.run(get_subtitles(movie, str(video), tmp_path, db_path))
    assert reextracted is True
    assert [e.text for e in second.entries] == ["Re-extracted line"]


def test_get_subtitles_no_subtitles_result_is_cached_and_stays_fresh(
    tmp_path, monkeypatch
):
    video = tmp_path / "Film.mkv"
    video.write_text("video bytes")
    db_path = tmp_path / "search_index.db"
    movie = _movie()

    monkeypatch.setattr(
        "app.worker.subtitles.probe_subtitle_streams", _no_embedded_streams
    )

    first = asyncio.run(get_subtitles(movie, str(video), tmp_path, db_path))
    assert first.source is SubtitleSource.NONE
    assert search_index.get_entries(db_path, movie.guid) == []

    # A NONE result has no source file to fingerprint, so it's always
    # served from cache once recorded -- prove it by making a repeat probe
    # blow up if it's ever attempted again.
    async def _boom(*args, **kwargs):
        raise AssertionError("should not re-probe once cached as NONE")

    monkeypatch.setattr("app.worker.subtitles.probe_subtitle_streams", _boom)

    second = asyncio.run(get_subtitles(movie, str(video), tmp_path, db_path))
    assert second == first


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


def test_delete_cached_subtitles_removes_existing_file(tmp_path):
    result = SubtitleResult(
        guid="plex://movie/abc",
        source=SubtitleSource.SIDECAR,
        entries=[SubtitleEntry(index=1, start=1.0, end=2.0, text="Hi")],
    )
    write_cached_subtitles(tmp_path, result)

    deleted = delete_cached_subtitles(tmp_path, "plex://movie/abc")

    assert deleted is True
    assert read_cached_subtitles(tmp_path, "plex://movie/abc") is None


def test_delete_cached_subtitles_returns_false_when_nothing_to_delete(tmp_path):
    assert delete_cached_subtitles(tmp_path, "plex://movie/never-cached") is False
