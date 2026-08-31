import asyncio
import os

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from app.settings import LibraryConfig, LibrarySyncDefaults, QuoteMatchDefaults, RenderDefaults, Settings, SubtitleDefaults, WorkerConfig, load_settings
from app.web.state import LibraryChoice, MappingRow, WizardState
from app.runtime import SettingsHolder
from app.web.app import (
    _JellyfinAuthError,
    _connect_and_discover_sync_jellyfin,
    _verify_discord_token,
    _write_config_files,
    create_web_app,
)


# ---- Regression: post-wizard settings reload must see the wizard's own
# writes, not the stale placeholder values Docker's env_file already
# loaded into os.environ before the wizard ever ran. ----------------------


@pytest.fixture
def isolated_env(monkeypatch):
    # Simulate exactly what docker-compose's `env_file: .env` does at
    # container start: populate os.environ with .env.example's empty
    # placeholders *before* load_settings() ever touches the file on disk.
    for key in ("DISCORD_TOKEN", "PLEX_URL", "PLEX_TOKEN", "DEV_GUILD_ID"):
        monkeypatch.setenv(key, "")
    yield


def _minimal_config(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({"libraries": []}))
    return config_path


def test_load_settings_default_does_not_pick_up_fresh_env_file(tmp_path, isolated_env):
    # This is the trap itself, made explicit: with override_env's default
    # (False, matching python-dotenv's own default), a key already present
    # in os.environ — even as an empty string — wins over what's actually
    # in the .env file on disk.
    env_path = tmp_path / ".env"
    env_path.write_text("DISCORD_TOKEN=real-token\nPLEX_URL=http://plex:32400\nPLEX_TOKEN=real-plex-token\n")
    config_path = _minimal_config(tmp_path)

    with pytest.raises(Exception):
        load_settings(env_path=env_path, config_path=config_path)


def test_load_settings_override_env_picks_up_fresh_env_file(tmp_path, isolated_env):
    # This is the fix: the post-wizard reload (app/main.py) must pass
    # override_env=True so the freshly-written .env actually wins.
    env_path = tmp_path / ".env"
    env_path.write_text("DISCORD_TOKEN=real-token\nPLEX_URL=http://plex:32400\nPLEX_TOKEN=real-plex-token\n")
    config_path = _minimal_config(tmp_path)

    settings = load_settings(env_path=env_path, config_path=config_path, override_env=True)

    assert settings.discord_token == "real-token"
    assert settings.plex_url == "http://plex:32400"
    assert settings.plex_token == "real-plex-token"


def test_load_settings_no_env_conflict_works_regardless_of_override(tmp_path, monkeypatch):
    # Sanity check: when os.environ has nothing to conflict with (the
    # normal non-wizard startup path), override_env's value shouldn't
    # matter — this guards against the fix accidentally changing behavior
    # for the common case.
    for key in ("DISCORD_TOKEN", "PLEX_URL", "PLEX_TOKEN", "DEV_GUILD_ID"):
        monkeypatch.delenv(key, raising=False)
    env_path = tmp_path / ".env"
    env_path.write_text("DISCORD_TOKEN=t\nPLEX_URL=http://plex\nPLEX_TOKEN=p\n")
    config_path = _minimal_config(tmp_path)

    settings = load_settings(env_path=env_path, config_path=config_path)

    assert settings.discord_token == "t"


# ---- _write_config_files -------------------------------------------------


def _state_with_one_library(*, three_d_format="none"):
    state = WizardState()
    state.discord_token = "disc-token"
    state.discord_username = "cinesnip-bot"
    state.media_server = "plex"
    state.plex_url = "http://plex:32400"
    state.plex_account_token = "plex-token"
    choice = LibraryChoice(
        name="Movies",
        section_type="movie",
        selected=True,
        mapping_rows=[MappingRow(path_prefix="D:\\Movies", container_path="/media/movies")],
        three_d_format=three_d_format,
    )
    state.library_choices = [choice]
    return state


def test_write_config_files_round_trips_through_load_settings(tmp_path):
    state = _state_with_one_library()
    env_path = tmp_path / ".env"
    config_path = tmp_path / "config.yaml"

    _write_config_files(state, env_path=env_path, config_path=config_path)
    settings = load_settings(env_path=env_path, config_path=config_path, override_env=True)

    assert settings.discord_token == "disc-token"
    assert settings.plex_url == "http://plex:32400"
    assert settings.plex_token == "plex-token"
    assert len(settings.libraries) == 1
    assert settings.libraries[0].name == "Movies"


def test_write_config_files_preserves_unrelated_env_lines(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("DISCORD_TOKEN=old\nPLEX_URL=old\nPLEX_TOKEN=old\nDEV_GUILD_ID=123456\n")
    config_path = tmp_path / "config.yaml"
    state = _state_with_one_library()

    _write_config_files(state, env_path=env_path, config_path=config_path)

    lines = env_path.read_text().splitlines()
    assert "DEV_GUILD_ID=123456" in lines
    assert "DISCORD_TOKEN=disc-token" in lines
    assert "PLEX_TOKEN=plex-token" in lines
    # The old values must actually be gone, not just superseded by a later
    # duplicate line (which load_dotenv would silently prefer the first of).
    assert "DISCORD_TOKEN=old" not in lines


def test_write_config_files_emits_three_d_format(tmp_path):
    state = _state_with_one_library(three_d_format="over_under")
    env_path = tmp_path / ".env"
    config_path = tmp_path / "config.yaml"

    _write_config_files(state, env_path=env_path, config_path=config_path)
    settings = load_settings(env_path=env_path, config_path=config_path, override_env=True)

    assert settings.libraries[0].three_d_format == "over_under"


def test_write_config_files_defaults_track_settings_models_not_a_hardcoded_copy(tmp_path):
    # Regression for the "hand-copied defaults drift from app/settings.py"
    # finding: written defaults must come from the Pydantic models
    # themselves, not a separate literal dict — verified here by comparing
    # against the models directly rather than hardcoding expected values a
    # second time in this test.
    state = _state_with_one_library()
    env_path = tmp_path / ".env"
    config_path = tmp_path / "config.yaml"

    _write_config_files(state, env_path=env_path, config_path=config_path)
    raw = yaml.safe_load(config_path.read_text())

    assert raw["render_defaults"] == RenderDefaults().model_dump()
    assert raw["subtitle_defaults"] == SubtitleDefaults().model_dump()
    assert raw["quote_match"] == QuoteMatchDefaults().model_dump()
    assert raw["worker"] == WorkerConfig().model_dump()
    assert raw["library_sync"] == LibrarySyncDefaults().model_dump()


def test_write_config_files_uses_state_media_server_when_set(tmp_path):
    # The normal case: /wizard/connect collected media_server onto state
    # itself (Jellyfin here), which must win over whatever the previously
    # running config said — e.g. switching an existing Plex install to
    # Jellyfin via a full wizard re-run.
    state = _state_with_one_library()
    state.media_server = "jellyfin"
    state.jellyfin_url = "http://jf:8096"
    state.jellyfin_api_key = "key"
    current_settings = Settings(
        discord_token="old",
        plex_url="http://plex:32400",
        plex_token="old",
        media_server="plex",
        libraries=[LibraryConfig(name="Movies")],
    )
    env_path = tmp_path / ".env"
    config_path = tmp_path / "config.yaml"

    _write_config_files(state, current_settings=current_settings, env_path=env_path, config_path=config_path)

    assert yaml.safe_load(config_path.read_text())["media_server"] == "jellyfin"
    lines = env_path.read_text().splitlines()
    assert "JELLYFIN_URL=http://jf:8096" in lines
    assert "JELLYFIN_API_KEY=key" in lines
    assert not any(line.startswith("PLEX_URL=") for line in lines)


def test_write_config_files_falls_back_to_current_settings_media_server_if_state_unset(tmp_path):
    # Defensive fallback for a WizardState that never went through
    # /wizard/connect (shouldn't happen in the real app — finish() always
    # re-runs validation, which requires media_server to be set) — must not
    # silently drop an existing Jellyfin install back to the "plex" default.
    state = _state_with_one_library()
    state.media_server = None
    state.library_sync_enabled = False
    current_settings = Settings(
        discord_token="old",
        jellyfin_url="http://jf:8096",
        jellyfin_api_key="key",
        media_server="jellyfin",
        libraries=[LibraryConfig(name="Movies")],
    )
    env_path = tmp_path / ".env"
    config_path = tmp_path / "config.yaml"

    _write_config_files(state, current_settings=current_settings, env_path=env_path, config_path=config_path)

    assert yaml.safe_load(config_path.read_text())["media_server"] == "jellyfin"


def test_write_config_files_defaults_media_server_to_plex_on_first_run(tmp_path):
    state = _state_with_one_library()
    env_path = tmp_path / ".env"
    config_path = tmp_path / "config.yaml"

    _write_config_files(state, env_path=env_path, config_path=config_path)

    assert yaml.safe_load(config_path.read_text())["media_server"] == "plex"


# ---- Backend choice (issue #26) --------------------------------------------


def test_current_step_shows_backend_picker_before_any_choice():
    state = WizardState()
    state.discord_username = "cinesnip-bot"
    assert state.media_server is None
    assert state.current_step == 2


def test_current_step_waits_for_jellyfin_url_once_jellyfin_chosen():
    state = WizardState()
    state.discord_username = "cinesnip-bot"
    state.media_server = "jellyfin"
    assert state.current_step == 2  # jellyfin_url not set yet

    state.jellyfin_url = "http://jf:8096"
    state.library_choices = [LibraryChoice(name="Movies", section_type="movie", selected=True)]
    assert state.current_step == 4  # a library is already selected, so straight to Sync


def test_current_step_waits_for_plex_url_once_plex_chosen():
    state = WizardState()
    state.discord_username = "cinesnip-bot"
    state.media_server = "plex"
    assert state.current_step == 2  # plex_url not set yet


def test_connect_reset_shows_picker_on_an_already_configured_install():
    # Regression: on any real (already-set-up) install, /wizard/connect
    # dispatches through _enter_wizard_step -> _seed_wizard_state_from_settings,
    # which fills in state.media_server from the live config whenever it's
    # None. A reset handler that cleared media_server and then re-entered
    # via connect_step would get it immediately re-seeded back to the
    # configured backend before the picker ever had a chance to render —
    # making "Use a different media server?"/"Switch to Jellyfin instead?"
    # a silent no-op for every real reconfiguration. It must render the
    # picker directly instead.
    settings = Settings(
        discord_token="",  # empty so _seed_wizard_state_from_settings skips the real Discord API call
        plex_url="http://plex.example",
        plex_token="",  # empty so it skips the real Plex connection attempt too
        libraries=[LibraryConfig(name="Movies")],
    )
    settings_holder = SettingsHolder(settings=settings)

    async def on_setup_complete():
        return None

    app = create_web_app(settings_holder, on_setup_complete)
    client = TestClient(app)

    # First entry seeds state.media_server = "plex" from the live config.
    client.get("/wizard/connect")
    # The reset link must actually show the picker, not bounce back to Plex.
    response = client.get("/wizard/connect/reset")

    assert "Which media server do you use?" in response.text
    assert "Connect your Plex account" not in response.text


def _patch_httpx_sync_client(monkeypatch, handler):
    real_client = httpx.Client

    def fake_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", fake_client)


def test_connect_and_discover_sync_jellyfin_raises_on_bad_key(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={})

    _patch_httpx_sync_client(monkeypatch, handler)

    with pytest.raises(_JellyfinAuthError):
        _connect_and_discover_sync_jellyfin("http://jf.test", "bad-key")


def test_connect_and_discover_sync_jellyfin_returns_server_and_libraries(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/System/Info":
            return httpx.Response(200, json={"ServerName": "MyJellyfin"})
        if request.url.path == "/Users":
            return httpx.Response(200, json=[{"Id": "user-1"}])
        if request.url.path == "/Library/VirtualFolders":
            return httpx.Response(
                200,
                json=[
                    {"Name": "Movies", "ItemId": "f1", "CollectionType": "movies", "Locations": ["/media/movies"]},
                    {"Name": "Collections", "ItemId": "f2", "CollectionType": "boxsets", "Locations": []},
                ],
            )
        if request.url.path == "/Users/user-1/Items":
            return httpx.Response(200, json={"Items": []})
        raise AssertionError(f"unexpected path {request.url.path}")

    _patch_httpx_sync_client(monkeypatch, handler)

    server_name, choices = _connect_and_discover_sync_jellyfin("http://jf.test", "good-key")

    assert server_name == "MyJellyfin"
    assert [c.name for c in choices] == ["Movies"]  # boxsets folder excluded
    assert choices[0].section_type == "movie"


# ---- Sync step (issue #8) --------------------------------------------------


def test_current_step_advances_through_sync_before_validate():
    state = _state_with_one_library()
    assert state.library_sync_enabled is None
    assert state.current_step == 4  # Sync — not yet decided

    state.library_sync_enabled = True
    assert state.current_step == 5  # Validate


def test_write_config_files_uses_wizard_sync_choice_on_first_run(tmp_path):
    state = _state_with_one_library()
    state.library_sync_enabled = True
    env_path = tmp_path / ".env"
    config_path = tmp_path / "config.yaml"

    _write_config_files(state, env_path=env_path, config_path=config_path)
    raw = yaml.safe_load(config_path.read_text())

    assert raw["library_sync"]["enabled"] is True
    assert raw["library_sync"]["interval_hours"] == LibrarySyncDefaults().interval_hours


def test_write_config_files_preserves_interval_hours_on_reconfiguration(tmp_path):
    # The Sync step only ever collects the on/off choice — interval_hours
    # stays whatever a Settings "Edit ___" user already set, matching the
    # existing preserve-the-rest behavior for render_defaults etc.
    state = _state_with_one_library()
    state.library_sync_enabled = False
    current_settings = Settings(
        discord_token="old",
        plex_url="http://plex:32400",
        plex_token="old",
        libraries=[LibraryConfig(name="Movies")],
        library_sync=LibrarySyncDefaults(enabled=True, interval_hours=6.0),
    )
    env_path = tmp_path / ".env"
    config_path = tmp_path / "config.yaml"

    _write_config_files(state, current_settings=current_settings, env_path=env_path, config_path=config_path)
    raw = yaml.safe_load(config_path.read_text())

    assert raw["library_sync"]["enabled"] is False  # the wizard's own choice wins
    assert raw["library_sync"]["interval_hours"] == 6.0  # untouched, preserved


# ---- _verify_discord_token ------------------------------------------------


def _run(coro):
    return asyncio.run(coro)


def test_verify_discord_token_rejected(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "401: Unauthorized"})

    _patch_httpx_client(monkeypatch, handler)

    ok, detail, payload = _run(_verify_discord_token("bad-token"))

    assert ok is False
    assert "rejected" in detail.lower()
    assert payload is None


def test_verify_discord_token_success_via_module(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "999", "username": "my-bot"})

    _patch_httpx_client(monkeypatch, handler)

    ok, detail, payload = _run(_verify_discord_token("good-token"))

    assert ok is True
    assert "my-bot" in detail
    assert payload["id"] == "999"


def test_verify_discord_token_network_error(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    _patch_httpx_client(monkeypatch, handler)

    ok, detail, payload = _run(_verify_discord_token("some-token"))

    assert ok is False
    assert "reach discord" in detail.lower()
    assert payload is None


def _patch_httpx_client(monkeypatch, handler):
    # _verify_discord_token constructs its own httpx.AsyncClient(timeout=10)
    # internally with no transport hook exposed, so route all outbound
    # traffic through a mock transport by patching httpx.AsyncClient itself
    # for the duration of the test — the simplest way to exercise the
    # function exactly as written without changing its signature just for
    # testability.
    real_async_client = httpx.AsyncClient

    def fake_async_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", fake_async_client)
