from app.settings import Settings
from app.worker import search_index
from app.worker.api import _index_if_searchable
from app.worker.quote_index import has_cached_title, is_no_subtitle_title
from app.worker.subtitles import SubtitleEntry, SubtitleResult, SubtitleSource


def _settings(tmp_path) -> Settings:
    return Settings(
        discord_token="x", plex_url="http://localhost", plex_token="x",
        cache_dir=tmp_path / "cache",
    )


def _movie(rating_key=101, title="Film", library_name="Movies"):
    from app.worker.plex_client import MovieResult
    return MovieResult(
        rating_key=rating_key, title=title, year=2000, duration_ms=1000,
        thumb_url=None, plex_path="D:\\Movies\\film.mkv", guid="guid-1", library_name=library_name,
    )


def test_index_if_searchable_records_source_for_a_real_match(tmp_path):
    settings = _settings(tmp_path)
    result = SubtitleResult(
        guid="guid-1", source=SubtitleSource.SIDECAR,
        entries=[SubtitleEntry(index=1, start=0.0, end=1.0, text="Hi")],
        sidecar_path="/media/film.en.srt",
    )

    _index_if_searchable(settings, _movie(), result)

    entries = search_index.get_entries(settings.quote_index_db_path, "guid-1")
    assert entries is not None
    assert len(entries) == 1
    assert entries[0].text == "Hi"

    source, sidecar_path, stream_index = search_index.get_source_info(
        settings.quote_index_db_path, "guid-1"
    )
    assert source == "sidecar"
    assert sidecar_path == "/media/film.en.srt"

    # _index_if_searchable no longer writes the legacy quote_index tables —
    # that write now happens exclusively via search_index (subtitles.py's
    # get_subtitles() writes there directly too).
    assert has_cached_title(settings.quote_index_db_path, "guid-1") is False


def test_index_if_searchable_preserves_an_existing_fingerprint(tmp_path):
    """A redundant second write (get_subtitles() already wrote this title
    into search_index moments earlier in the real request flow) must not
    clobber the stored fingerprint with None — that would force a cold
    re-extraction on every subsequent request for this title."""
    settings = _settings(tmp_path)
    entries = [SubtitleEntry(index=1, start=0.0, end=1.0, text="Hi")]
    search_index.upsert_title(
        settings.quote_index_db_path,
        "guid-1", 101, "Film", "Movies", "sidecar",
        "/media/film.en.srt", None, entries, (123.0, 456),
    )

    result = SubtitleResult(
        guid="guid-1", source=SubtitleSource.SIDECAR,
        entries=entries, sidecar_path="/media/film.en.srt",
    )
    _index_if_searchable(settings, _movie(), result)

    assert search_index.get_fingerprint(settings.quote_index_db_path, "guid-1") == (123.0, 456)


def test_index_if_searchable_records_no_subtitle_titles(tmp_path):
    settings = _settings(tmp_path)
    result = SubtitleResult(guid="guid-1", source=SubtitleSource.NONE, entries=[])

    _index_if_searchable(settings, _movie(), result)

    assert is_no_subtitle_title(settings.quote_index_db_path, "guid-1") is True
    assert has_cached_title(settings.quote_index_db_path, "guid-1") is False
    # The NONE branch still only writes the legacy no_subtitle_titles
    # bookkeeping table, not search_index — get_subtitles() is what indexes
    # a NONE outcome into search_index (with empty entries), not this call.
    assert search_index.get_entries(settings.quote_index_db_path, "guid-1") is None
