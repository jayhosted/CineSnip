import pytest
from fastapi import FastAPI
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient

from app.runtime import SettingsHolder
from app.settings import Settings
from app.web.settings import register_settings_routes

_TEMPLATES_DIR = "app/web/templates"


@pytest.fixture
def settings_holder(tmp_path):
    settings = Settings(
        discord_token="x",
        media_server="jellyfin",
        jellyfin_url="http://jf.test",
        jellyfin_api_key="key",
        cache_dir=tmp_path / "cache",
    )
    return SettingsHolder(settings=settings)


@pytest.fixture
def client(settings_holder, monkeypatch):
    app = FastAPI()
    templates = Jinja2Templates(directory=_TEMPLATES_DIR)

    def fake_write_config_yaml(new_settings, config_path=None):
        settings_holder.settings = new_settings

    monkeypatch.setattr("app.web.settings.write_config_yaml", fake_write_config_yaml)

    async def on_setup_complete():
        return None

    register_settings_routes(app, templates, settings_holder, on_setup_complete)
    return TestClient(app)


def test_cache_save_rejects_library_sync_with_jellyfin_before_writing(client, settings_holder):
    response = client.post(
        "/settings/cache",
        data={"enabled": "on", "interval_hours": "24.0"},
    )

    assert response.status_code == 200
    assert "jellyfin" in response.text.lower()
    # The write must never have happened — settings_holder.settings is only
    # ever swapped by fake_write_config_yaml, so it staying untouched proves
    # apply()/write_config_yaml() was never called.
    assert settings_holder.settings.library_sync.enabled is False


def test_cache_save_allows_library_sync_disabled_with_jellyfin(client, settings_holder):
    response = client.post(
        "/settings/cache",
        data={"interval_hours": "12.0"},
    )

    assert response.status_code == 200
    assert settings_holder.settings.library_sync.enabled is False
    assert settings_holder.settings.library_sync.interval_hours == 12.0


def test_cache_save_allows_library_sync_with_plex(tmp_path, monkeypatch):
    app = FastAPI()
    templates = Jinja2Templates(directory=_TEMPLATES_DIR)
    settings = Settings(
        discord_token="x", plex_url="http://x", plex_token="x", cache_dir=tmp_path / "cache"
    )
    settings_holder = SettingsHolder(settings=settings)

    def fake_write_config_yaml(new_settings, config_path=None):
        settings_holder.settings = new_settings

    monkeypatch.setattr("app.web.settings.write_config_yaml", fake_write_config_yaml)

    async def on_setup_complete():
        return None

    register_settings_routes(app, templates, settings_holder, on_setup_complete)
    client = TestClient(app)

    response = client.post(
        "/settings/cache",
        data={"enabled": "on", "interval_hours": "24.0"},
    )

    assert response.status_code == 200
    assert settings_holder.settings.library_sync.enabled is True
