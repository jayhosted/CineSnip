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
    StylePresetConfig,
    StylePresetError,
    TitleStyleOverride,
    TitleStyleOverrideError,
    delete_style_preset,
    delete_title_override,
    load_settings,
    resolve_style_for_title,
    upsert_style_preset,
    upsert_title_override,
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


def test_jellyfin_plus_library_sync_enabled_loads(tmp_path):
    # library_sync now works with either backend (issue #25) — Jellyfin's
    # JellyfinClient.current_section_updated_ats() supplies its own change
    # signal, so this combination is no longer rejected at config-load time.
    env = tmp_path / ".env"
    env.write_text(
        "DISCORD_TOKEN=t\nJELLYFIN_URL=http://jf:8096\nJELLYFIN_API_KEY=key\n"
    )
    config = tmp_path / "config.yaml"
    config.write_text(
        "media_server: jellyfin\nlibraries: []\nlibrary_sync:\n  enabled: true\n"
    )

    assert load_settings(env_path=env, config_path=config).library_sync.enabled is True


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


def test_settings_seeds_four_builtin_style_presets_by_default():
    settings = Settings(discord_token="x", plex_url="http://x", plex_token="x")
    names = {cfg.name for cfg in settings.subtitle_styles}
    assert names == {"classic", "boxed", "cinematic", "meme"}
    assert all(cfg.builtin for cfg in settings.subtitle_styles)


def test_style_presets_converts_configs_to_style_preset_objects():
    settings = Settings(discord_token="x", plex_url="http://x", plex_token="x")
    presets = settings.style_presets()
    assert presets["classic"].font == "Liberation Sans"
    assert presets["classic"].font_size == 26


def test_upsert_style_preset_adds_a_new_custom_preset():
    settings = Settings(discord_token="x", plex_url="http://x", plex_token="x")
    new_cfg = StylePresetConfig(
        name="simpsons", font="Simpsonfont", font_size=28,
        primary_color="&H0000FFFF", outline_color="&H00000000",
        back_color="&H00000000", border_style=1, outline=2.0, shadow=0.0,
        bold=False, uppercase=False, margin_v=24,
    )
    updated = upsert_style_preset(settings, original_name=None, config=new_cfg)
    assert "simpsons" in {cfg.name for cfg in updated.subtitle_styles}
    assert len(updated.subtitle_styles) == 5


def test_upsert_style_preset_edits_a_builtin_without_renaming():
    settings = Settings(discord_token="x", plex_url="http://x", plex_token="x")
    classic = next(cfg for cfg in settings.subtitle_styles if cfg.name == "classic")
    edited = classic.model_copy(update={"font_size": 30})
    updated = upsert_style_preset(settings, original_name="classic", config=edited)
    assert updated.style_presets()["classic"].font_size == 30
    assert next(c for c in updated.subtitle_styles if c.name == "classic").builtin is True


def test_upsert_style_preset_forces_builtin_true_even_if_input_says_false():
    settings = Settings(discord_token="x", plex_url="http://x", plex_token="x")
    classic = next(cfg for cfg in settings.subtitle_styles if cfg.name == "classic")
    snuck_in = classic.model_copy(update={"font_size": 30, "builtin": False})
    updated = upsert_style_preset(settings, original_name="classic", config=snuck_in)
    assert next(c for c in updated.subtitle_styles if c.name == "classic").builtin is True


def test_upsert_style_preset_renames_a_custom_preset():
    settings = Settings(discord_token="x", plex_url="http://x", plex_token="x")
    new_cfg = StylePresetConfig(
        name="simpsons", font="Simpsonfont", font_size=28,
        primary_color="&H0000FFFF", outline_color="&H00000000",
        back_color="&H00000000", border_style=1, outline=2.0, shadow=0.0,
        bold=False, uppercase=False, margin_v=24,
    )
    with_custom = upsert_style_preset(settings, original_name=None, config=new_cfg)
    renamed_cfg = new_cfg.model_copy(update={"name": "flanders"})
    updated = upsert_style_preset(
        with_custom, original_name="simpsons", config=renamed_cfg
    )
    names = {cfg.name for cfg in updated.subtitle_styles}
    assert "simpsons" not in names
    assert "flanders" in names
    flanders = next(c for c in updated.subtitle_styles if c.name == "flanders")
    assert flanders.font == "Simpsonfont"
    assert flanders.font_size == 28


def test_upsert_style_preset_rejects_unknown_original_name():
    settings = Settings(discord_token="x", plex_url="http://x", plex_token="x")
    cfg = StylePresetConfig(
        name="ghost", font="X", font_size=10, primary_color="&H00FFFFFF",
        outline_color="&H00000000", back_color="&H00000000", border_style=1,
        outline=1.0, shadow=0.0, bold=False, uppercase=False, margin_v=10,
    )
    with pytest.raises(StylePresetError):
        upsert_style_preset(settings, original_name="does-not-exist", config=cfg)


def test_upsert_style_preset_rejects_renaming_a_builtin():
    settings = Settings(discord_token="x", plex_url="http://x", plex_token="x")
    classic = next(cfg for cfg in settings.subtitle_styles if cfg.name == "classic")
    renamed = classic.model_copy(update={"name": "classic2"})
    with pytest.raises(StylePresetError):
        upsert_style_preset(settings, original_name="classic", config=renamed)


def test_upsert_style_preset_rejects_duplicate_custom_name():
    settings = Settings(discord_token="x", plex_url="http://x", plex_token="x")
    dupe = StylePresetConfig(
        name="boxed", font="X", font_size=10, primary_color="&H00FFFFFF",
        outline_color="&H00000000", back_color="&H00000000", border_style=1,
        outline=1.0, shadow=0.0, bold=False, uppercase=False, margin_v=10,
    )
    with pytest.raises(StylePresetError):
        upsert_style_preset(settings, original_name=None, config=dupe)


def test_delete_style_preset_rejects_a_builtin():
    settings = Settings(discord_token="x", plex_url="http://x", plex_token="x")
    with pytest.raises(StylePresetError):
        delete_style_preset(settings, "classic")


def test_delete_style_preset_removes_a_custom_preset():
    settings = Settings(discord_token="x", plex_url="http://x", plex_token="x")
    new_cfg = StylePresetConfig(
        name="simpsons", font="Simpsonfont", font_size=28,
        primary_color="&H0000FFFF", outline_color="&H00000000",
        back_color="&H00000000", border_style=1, outline=2.0, shadow=0.0,
        bold=False, uppercase=False, margin_v=24,
    )
    with_custom = upsert_style_preset(settings, original_name=None, config=new_cfg)
    removed = delete_style_preset(with_custom, "simpsons")
    assert "simpsons" not in {cfg.name for cfg in removed.subtitle_styles}


def test_write_config_yaml_round_trips_subtitle_styles(tmp_path):
    settings = Settings(discord_token="x", plex_url="http://x", plex_token="x")
    config_path = tmp_path / "config.yaml"
    env_path = tmp_path / ".env"
    env_path.write_text("DISCORD_TOKEN=x\nPLEX_URL=http://x\nPLEX_TOKEN=x\n")
    config_path.write_text("media_server: plex\n")
    write_config_yaml(settings, config_path)

    reloaded = load_settings(env_path=env_path, config_path=config_path)
    assert {cfg.name for cfg in reloaded.subtitle_styles} == {
        "classic", "boxed", "cinematic", "meme",
    }


def _style_kwargs(**overrides) -> dict:
    kwargs = dict(
        name="simpsons", font="Simpsonfont", font_size=28,
        primary_color="&H0000FFFF", outline_color="&H00000000",
        back_color="&H00000000", border_style=1, outline=2.0, shadow=0.0,
        bold=False, uppercase=False, margin_v=24,
    )
    kwargs.update(overrides)
    return kwargs


@pytest.mark.parametrize("bad_name", ["", "   ", "none", "None", "__preview__", "a/b", "a\\b", "x" * 65])
def test_style_preset_config_rejects_invalid_names(bad_name):
    with pytest.raises(ValidationError):
        StylePresetConfig(**_style_kwargs(name=bad_name))


@pytest.mark.parametrize("good_name", ["simpsons", "My Cool Style", "a" * 64])
def test_style_preset_config_accepts_valid_names(good_name):
    cfg = StylePresetConfig(**_style_kwargs(name=good_name))
    assert cfg.name == good_name


@pytest.mark.parametrize("field", ["font", "primary_color", "outline_color", "back_color"])
def test_style_preset_config_rejects_newlines_in_style_fields(field):
    with pytest.raises(ValidationError):
        StylePresetConfig(**_style_kwargs(**{field: "line1\nline2"}))


def _settings_with_overrides(*overrides: TitleStyleOverride) -> Settings:
    return Settings(
        discord_token="x", plex_url="http://x", plex_token="x",
        title_style_overrides=list(overrides),
    )


def test_resolve_style_for_title_matches_case_insensitive_substring():
    settings = _settings_with_overrides(TitleStyleOverride(match="South Park", style="classic"))
    assert resolve_style_for_title(settings, "south park — s01e01 — cartman gets an anal probe", "none") == "classic"


def test_resolve_style_for_title_falls_back_when_no_match():
    settings = _settings_with_overrides(TitleStyleOverride(match="South Park", style="classic"))
    assert resolve_style_for_title(settings, "The Matrix", "none") == "none"


def test_resolve_style_for_title_first_match_in_list_order_wins():
    settings = _settings_with_overrides(
        TitleStyleOverride(match="South Park", style="classic"),
        TitleStyleOverride(match="Park", style="boxed"),
    )
    assert resolve_style_for_title(settings, "South Park", "none") == "classic"


def test_upsert_title_override_rejects_unknown_style():
    settings = _settings_with_overrides()
    with pytest.raises(TitleStyleOverrideError):
        upsert_title_override(settings, None, TitleStyleOverride(match="South Park", style="nope"))


def test_upsert_title_override_adds_a_new_entry():
    settings = _settings_with_overrides()
    updated = upsert_title_override(settings, None, TitleStyleOverride(match="South Park", style="classic"))
    assert [o.match for o in updated.title_style_overrides] == ["South Park"]


def test_upsert_title_override_rejects_duplicate_match():
    settings = _settings_with_overrides(TitleStyleOverride(match="South Park", style="classic"))
    with pytest.raises(TitleStyleOverrideError):
        upsert_title_override(settings, None, TitleStyleOverride(match="South Park", style="boxed"))


def test_delete_title_override_removes_the_entry():
    settings = _settings_with_overrides(TitleStyleOverride(match="South Park", style="classic"))
    updated = delete_title_override(settings, "South Park")
    assert updated.title_style_overrides == []


def test_delete_title_override_rejects_unknown_match():
    settings = _settings_with_overrides()
    with pytest.raises(TitleStyleOverrideError):
        delete_title_override(settings, "South Park")


def test_title_style_overrides_round_trip_through_config_yaml(tmp_path):
    settings = _settings_with_overrides(TitleStyleOverride(match="South Park", style="classic"))
    config_path = tmp_path / "config.yaml"
    env_path = tmp_path / ".env"
    env_path.write_text("DISCORD_TOKEN=x\nPLEX_URL=http://x\nPLEX_TOKEN=x\n")
    write_config_yaml(settings, config_path)

    reloaded = load_settings(env_path=env_path, config_path=config_path)
    assert [o.match for o in reloaded.title_style_overrides] == ["South Park"]
