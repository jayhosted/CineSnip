from app.settings import LibraryConfig, Settings
from app.web.dashboard import CoverageStats, _coverage_stats
from app.runtime import SettingsHolder
from app.worker import quote_index, search_index


def _settings(tmp_path, library_names: list[str]) -> Settings:
    return Settings(
        discord_token="x", plex_url="http://localhost", plex_token="x",
        cache_dir=tmp_path / "cache",
        libraries=[LibraryConfig(name=name) for name in library_names],
    )


def _upsert(db_path, guid, media_id, title, library_name, source):
    # Mirrors the real write path (subtitles.get_subtitles() /
    # library_sync.py) post-migration: sidecar/embedded titles land in
    # search_index.titles, not the legacy quote_index.cached_titles table.
    search_index.upsert_title(
        db_path, guid=guid, media_id=media_id, title=title,
        library_name=library_name, source=source, sidecar_path=None,
        stream_index=None, entries=[], fingerprint=None,
    )


def test_coverage_stats_aggregates_across_libraries(tmp_path):
    settings = _settings(tmp_path, ["Movies", "3D"])
    _upsert(settings.quote_index_db_path, "g1", "1", "F1", "Movies", "sidecar")
    _upsert(settings.quote_index_db_path, "g2", "2", "F2", "Movies", "embedded")
    quote_index.upsert_no_subtitle_title(settings.quote_index_db_path, "g3", "3", "F3", "Movies")
    _upsert(settings.quote_index_db_path, "g4", "4", "F4", "3D", "sidecar")
    quote_index.set_library_item_count(settings.quote_index_db_path, "Movies", 5)
    quote_index.set_library_item_count(settings.quote_index_db_path, "3D", 2)

    holder = SettingsHolder(settings=settings, media_client=None)

    stats = _coverage_stats(holder)

    assert stats == CoverageStats(
        cached_total=3, library_total=7, sidecar_count=2, embedded_count=1,
        no_subtitle_count=1, library_count=2,
    )


def test_coverage_stats_excludes_no_subtitle_source_rows_from_buckets(tmp_path):
    # get_subtitles() upserts a search_index.titles row even for the
    # no-subtitle outcome (source="none") — it must not inflate either the
    # sidecar or embedded bucket.
    settings = _settings(tmp_path, ["Movies"])
    _upsert(settings.quote_index_db_path, "g1", "1", "F1", "Movies", "sidecar")
    _upsert(settings.quote_index_db_path, "g2", "2", "F2", "Movies", "none")
    quote_index.upsert_no_subtitle_title(settings.quote_index_db_path, "g2", "2", "F2", "Movies")

    holder = SettingsHolder(settings=settings, media_client=None)

    stats = _coverage_stats(holder)

    assert stats.sidecar_count == 1
    assert stats.embedded_count == 0
    assert stats.no_subtitle_count == 1


def test_coverage_stats_handles_no_persisted_count_yet(tmp_path):
    settings = _settings(tmp_path, ["Movies"])
    holder = SettingsHolder(settings=settings, media_client=None)

    stats = _coverage_stats(holder)

    assert stats.library_total == 0
    assert stats.library_count == 1


def test_coverage_stats_handles_no_libraries_configured(tmp_path):
    settings = _settings(tmp_path, [])
    holder = SettingsHolder(settings=settings, media_client=None)

    stats = _coverage_stats(holder)

    assert stats == CoverageStats(0, 0, 0, 0, 0, 0)
