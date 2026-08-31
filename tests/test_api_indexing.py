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


def _movie(media_id="101", title="Film", library_name="Movies"):
    from app.worker.media_client import MovieResult
    return MovieResult(
        media_id=media_id, title=title, year=2000, duration_ms=1000,
        thumb_url=None, source_path="D:\\Movies\\film.mkv", guid="guid-1", library_name=library_name,
    )


def test_index_if_searchable_does_not_rewrite_search_index_for_a_searchable_result(tmp_path):
    """get_subtitles() (app/worker/subtitles.py) already unconditionally
    writes a searchable result into search_index before every real caller
    invokes _index_if_searchable — so this must be a genuine no-op for the
    SIDECAR/EMBEDDED case, not a redundant second write (which would cost a
    full delete+reinsert of every subtitle line/FTS row on every /render,
    /resolve-quote, and episode-cache request for data that didn't change).
    Simulates that prior write directly via search_index.upsert_title, the
    same way get_subtitles() would have, then asserts _index_if_searchable
    leaves it byte-for-byte untouched."""
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

    # Untouched: same fingerprint, same entries, same source metadata —
    # not just "still present" (a rewrite with correct data would also
    # pass that), but identical to what was there before the call.
    assert search_index.get_fingerprint(settings.quote_index_db_path, "guid-1") == (123.0, 456)
    assert search_index.get_entries(settings.quote_index_db_path, "guid-1") == entries
    source, sidecar_path, stream_index = search_index.get_source_info(
        settings.quote_index_db_path, "guid-1"
    )
    assert (source, sidecar_path, stream_index) == ("sidecar", "/media/film.en.srt", None)

    # Never wrote the legacy quote_index table either — that write was
    # removed for the searchable branch entirely, not redirected.
    assert has_cached_title(settings.quote_index_db_path, "guid-1") is False


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
