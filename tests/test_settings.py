import pytest

from app.settings import (
    LibraryConfig,
    LibrarySyncDefaults,
    PathMapping,
    QuoteMatchDefaults,
    Settings,
    SettingsError,
    load_settings,
    write_config_yaml,
)


def _settings(libraries: list[LibraryConfig]) -> Settings:
    return Settings(
        discord_token="x",
        plex_url="http://localhost",
        plex_token="x",
        libraries=libraries,
    )


def test_path_mappings_for_returns_the_matching_librarys_mappings():
    movies_mappings = [
        PathMapping(plex_prefix="D:\\Movies", container_path="/media/movies")
    ]
    tv_mappings = [PathMapping(plex_prefix="D:\\TV", container_path="/media/tv")]
    settings = _settings(
        [
            LibraryConfig(name="Movies", path_mappings=movies_mappings),
            LibraryConfig(name="TV Shows", path_mappings=tv_mappings),
        ]
    )

    assert settings.path_mappings_for("Movies") == movies_mappings
    assert settings.path_mappings_for("TV Shows") == tv_mappings


def test_path_mappings_for_raises_for_unconfigured_library():
    settings = _settings([LibraryConfig(name="Movies", path_mappings=[])])

    with pytest.raises(SettingsError):
        settings.path_mappings_for("4K Movies")


def test_three_d_format_for_defaults_to_none():
    settings = _settings([LibraryConfig(name="Movies", path_mappings=[])])

    assert settings.three_d_format_for("Movies") == "none"


def test_three_d_format_for_returns_configured_value():
    settings = _settings(
        [LibraryConfig(name="3D", path_mappings=[], three_d_format="over_under")]
    )

    assert settings.three_d_format_for("3D") == "over_under"


def test_three_d_format_for_raises_for_unconfigured_library():
    settings = _settings([LibraryConfig(name="Movies", path_mappings=[])])

    with pytest.raises(SettingsError):
        settings.three_d_format_for("4K Movies")


def test_library_sync_defaults_are_off_by_default():
    settings = _settings([])

    assert settings.library_sync.enabled is False
    assert settings.library_sync.interval_hours == 24.0


def test_library_sync_config_overrides_apply():
    settings = _settings([])
    settings = settings.model_copy(
        update={"library_sync": LibrarySyncDefaults(enabled=True, interval_hours=6.0)}
    )

    assert settings.library_sync.enabled is True
    assert settings.library_sync.interval_hours == 6.0


def test_quote_match_fetch_limit_defaults_to_50():
    quote_match = QuoteMatchDefaults()
    assert quote_match.fetch_limit == 50


def test_quote_match_fetch_limit_round_trips_through_config_yaml(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("DISCORD_TOKEN=x\nPLEX_URL=http://x\nPLEX_TOKEN=x\n")
    config_path = tmp_path / "config.yaml"
    config_path.write_text("libraries: []\n")

    settings = load_settings(env_path=env_path, config_path=config_path)
    settings.quote_match.fetch_limit = 75
    write_config_yaml(settings, config_path=config_path)

    reloaded = load_settings(env_path=env_path, config_path=config_path)
    assert reloaded.quote_match.fetch_limit == 75
