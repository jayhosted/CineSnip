from app.settings import LibraryConfig, Settings
from app.web.dashboard import CoverageStats, _coverage_stats
from app.runtime import SettingsHolder
from app.worker import quote_index


def _settings(tmp_path, library_names: list[str]) -> Settings:
    return Settings(
        discord_token="x", plex_url="http://localhost", plex_token="x",
        cache_dir=tmp_path / "cache",
        libraries=[LibraryConfig(name=name) for name in library_names],
    )


class _FakePlexByName:
    """Keyed by section identity isn't practical for a fake — instead this
    fake's enumerate_section looks up counts via a small wrapper object
    library_sections() returns, so _coverage_stats' `sections.get(name)`
    -> `plex.enumerate_section(section)` round-trip works without needing
    real plexapi Section objects."""

    class _Section:
        def __init__(self, count: int):
            self.count = count

    def __init__(self, section_counts: dict[str, int]):
        self._counts = section_counts

    def library_sections(self):
        return [(name, self._Section(count)) for name, count in self._counts.items()]

    def enumerate_section(self, section):
        return [object()] * section.count


def test_coverage_stats_aggregates_across_libraries(tmp_path):
    settings = _settings(tmp_path, ["Movies", "3D"])
    quote_index.upsert_cached_title(settings.quote_index_db_path, "g1", 1, "F1", "Movies", "sidecar")
    quote_index.upsert_cached_title(settings.quote_index_db_path, "g2", 2, "F2", "Movies", "embedded")
    quote_index.upsert_no_subtitle_title(settings.quote_index_db_path, "g3", 3, "F3", "Movies")
    quote_index.upsert_cached_title(settings.quote_index_db_path, "g4", 4, "F4", "3D", "sidecar")

    holder = SettingsHolder(settings=settings, plex_client=_FakePlexByName({"Movies": 5, "3D": 2}))

    stats = _coverage_stats(holder)

    assert stats == CoverageStats(
        cached_total=3, library_total=7, sidecar_count=2, embedded_count=1,
        no_subtitle_count=1, library_count=2,
    )


def test_coverage_stats_handles_no_plex_client(tmp_path):
    settings = _settings(tmp_path, ["Movies"])
    holder = SettingsHolder(settings=settings, plex_client=None)

    stats = _coverage_stats(holder)

    assert stats.library_total == 0
    assert stats.library_count == 1


def test_coverage_stats_handles_no_libraries_configured(tmp_path):
    settings = _settings(tmp_path, [])
    holder = SettingsHolder(settings=settings, plex_client=_FakePlexByName({}))

    stats = _coverage_stats(holder)

    assert stats == CoverageStats(0, 0, 0, 0, 0, 0)
