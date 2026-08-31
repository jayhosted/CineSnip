import pytest
from pydantic import ValidationError

from app.settings import (
    LibraryConfig,
    LibrarySyncDefaults,
    PathMapping,
    QuoteMatchDefaults,
    RenderDefaults,
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
        PathMapping(path_prefix="D:\\Movies", container_path="/media/movies")
    ]
    tv_mappings = [PathMapping(path_prefix="D:\\TV", container_path="/media/tv")]
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


def test_render_defaults_audio_language_defaults_to_english():
    assert RenderDefaults().audio_language == "eng"


def test_audio_language_round_trips_through_config_yaml(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("DISCORD_TOKEN=x\nPLEX_URL=http://x\nPLEX_TOKEN=x\n")
    config_path = tmp_path / "config.yaml"
    config_path.write_text("libraries: []\n")

    settings = load_settings(env_path=env_path, config_path=config_path)
    settings.render_defaults.audio_language = "fre"
    write_config_yaml(settings, config_path=config_path)

    reloaded = load_settings(env_path=env_path, config_path=config_path)
    assert reloaded.render_defaults.audio_language == "fre"


def test_render_defaults_soundboard_replace_scope_defaults_to_cinesnip_only():
    assert RenderDefaults().soundboard_replace_scope == "cinesnip_only"


def test_soundboard_replace_scope_round_trips_through_config_yaml(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("DISCORD_TOKEN=x\nPLEX_URL=http://x\nPLEX_TOKEN=x\n")
    config_path = tmp_path / "config.yaml"
    config_path.write_text("libraries: []\n")

    settings = load_settings(env_path=env_path, config_path=config_path)
    settings.render_defaults.soundboard_replace_scope = "any"
    write_config_yaml(settings, config_path=config_path)

    reloaded = load_settings(env_path=env_path, config_path=config_path)
    assert reloaded.render_defaults.soundboard_replace_scope == "any"


def test_soundboard_replace_scope_rejects_invalid_value():
    with pytest.raises(ValidationError):
        RenderDefaults(soundboard_replace_scope="everything")


def test_path_mapping_field_is_path_prefix_not_plex_prefix():
    mapping = PathMapping(path_prefix="D:\\Movies", container_path="/media/movies")
    assert mapping.path_prefix == "D:\\Movies"
    assert not hasattr(mapping, "plex_prefix")


def test_media_server_defaults_to_plex(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("DISCORD_TOKEN=t\nPLEX_URL=http://x\nPLEX_TOKEN=y\n")
    config = tmp_path / "config.yaml"
    config.write_text("libraries: []\n")
    settings = load_settings(env_path=env, config_path=config)
    assert settings.media_server == "plex"


def test_media_server_jellyfin_requires_jellyfin_url_and_key(tmp_path):
    env = tmp_path / ".env"
    env.write_text("DISCORD_TOKEN=t\n")
    config = tmp_path / "config.yaml"
    config.write_text("media_server: jellyfin\nlibraries: []\n")
    with pytest.raises(SettingsError, match="JELLYFIN_URL"):
        load_settings(env_path=env, config_path=config)


def test_media_server_jellyfin_succeeds_with_jellyfin_env(tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        "DISCORD_TOKEN=t\nJELLYFIN_URL=http://jf:8096\nJELLYFIN_API_KEY=key\n"
    )
    config = tmp_path / "config.yaml"
    config.write_text("media_server: jellyfin\nlibraries: []\n")
    settings = load_settings(env_path=env, config_path=config)
    assert settings.media_server == "jellyfin"
    assert settings.jellyfin_url == "http://jf:8096"
    assert settings.jellyfin_api_key == "key"


def test_jellyfin_plus_library_sync_enabled_is_rejected_at_load(tmp_path):
    # library_sync is Plex-only (issue #25): JellyfinClient raises
    # NotImplementedError for it, and the sync loop's broad
    # "server unreachable" handler would swallow that into a silent no-op —
    # so the combination has to fail here instead, at config-load time.
    env = tmp_path / ".env"
    env.write_text(
        "DISCORD_TOKEN=t\nJELLYFIN_URL=http://jf:8096\nJELLYFIN_API_KEY=key\n"
    )
    config = tmp_path / "config.yaml"
    config.write_text(
        "media_server: jellyfin\nlibraries: []\nlibrary_sync:\n  enabled: true\n"
    )

    with pytest.raises(SettingsError, match="library_sync"):
        load_settings(env_path=env, config_path=config)


def test_jellyfin_with_library_sync_disabled_still_loads(tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        "DISCORD_TOKEN=t\nJELLYFIN_URL=http://jf:8096\nJELLYFIN_API_KEY=key\n"
    )
    config = tmp_path / "config.yaml"
    config.write_text(
        "media_server: jellyfin\nlibraries: []\nlibrary_sync:\n  enabled: false\n"
    )

    assert load_settings(env_path=env, config_path=config).library_sync.enabled is False


def test_plex_with_library_sync_enabled_still_loads(tmp_path):
    env = tmp_path / ".env"
    env.write_text("DISCORD_TOKEN=t\nPLEX_URL=http://x\nPLEX_TOKEN=y\n")
    config = tmp_path / "config.yaml"
    config.write_text("libraries: []\nlibrary_sync:\n  enabled: true\n")

    assert load_settings(env_path=env, config_path=config).library_sync.enabled is True


def test_media_server_round_trips_through_write_config_yaml(tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        "DISCORD_TOKEN=t\nJELLYFIN_URL=http://jf:8096\nJELLYFIN_API_KEY=key\n"
    )
    config = tmp_path / "config.yaml"
    config.write_text("media_server: jellyfin\nlibraries: []\n")

    settings = load_settings(env_path=env, config_path=config)
    write_config_yaml(settings, config_path=config)

    assert load_settings(env_path=env, config_path=config).media_server == "jellyfin"
