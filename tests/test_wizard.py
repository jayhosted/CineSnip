import asyncio
import os

import httpx
import pytest
import yaml

from app.settings import LibrarySyncDefaults, QuoteMatchDefaults, RenderDefaults, SubtitleDefaults, WorkerConfig, load_settings
from app.web.state import LibraryChoice, MappingRow, WizardState
from app.web.app import _verify_discord_token, _write_config_files


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
    state.plex_url = "http://plex:32400"
    state.plex_account_token = "plex-token"
    choice = LibraryChoice(
        name="Movies",
        section_type="movie",
        selected=True,
        mapping_rows=[MappingRow(plex_prefix="D:\\Movies", container_path="/media/movies")],
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
