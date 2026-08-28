from app.settings import Settings
from app.worker.api import _index_if_searchable
from app.worker.quote_index import has_cached_title, is_no_subtitle_title, library_coverage
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
    )

    _index_if_searchable(settings, _movie(), result)

    assert has_cached_title(settings.quote_index_db_path, "guid-1") is True
    assert library_coverage(settings.quote_index_db_path, "Movies").sidecar_count == 1


def test_index_if_searchable_records_no_subtitle_titles(tmp_path):
    settings = _settings(tmp_path)
    result = SubtitleResult(guid="guid-1", source=SubtitleSource.NONE, entries=[])

    _index_if_searchable(settings, _movie(), result)

    assert is_no_subtitle_title(settings.quote_index_db_path, "guid-1") is True
    assert has_cached_title(settings.quote_index_db_path, "guid-1") is False
