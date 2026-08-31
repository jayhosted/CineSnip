import pytest
from fastapi import FastAPI
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient

from app.runtime import SettingsHolder
from app.settings import Settings
from app.web.settings import register_settings_routes

_TEMPLATES_DIR = "app/web/templates"


@pytest.fixture
def settings_holder():
    settings = Settings(discord_token="x", plex_url="http://x", plex_token="x")
    return SettingsHolder(settings=settings)


@pytest.fixture
def client(settings_holder, monkeypatch):
    app = FastAPI()
    templates = Jinja2Templates(directory=_TEMPLATES_DIR)

    # register_settings_routes' apply() always calls write_config_yaml with
    # its hardcoded default path ("config.yaml" in cwd) — stub it out so a
    # test run never touches the real repo's config.yaml, and have it swap
    # settings_holder.settings in place the way the real on_setup_complete
    # (app/web/app.py) does after re-reading from disk.
    def fake_write_config_yaml(new_settings, config_path=None):
        settings_holder.settings = new_settings

    monkeypatch.setattr("app.web.settings.write_config_yaml", fake_write_config_yaml)

    async def on_setup_complete():
        return None

    register_settings_routes(app, templates, settings_holder, on_setup_complete)
    return TestClient(app)


def test_settings_audio_page_shows_soundboard_replace_scope_options(client):
    response = client.get("/settings/audio")
    assert response.status_code == 200
    assert "soundboard_replace_scope" in response.text
    assert "cinesnip_only" in response.text


def test_settings_audio_save_updates_soundboard_replace_scope(client, settings_holder):
    response = client.post(
        "/settings/audio",
        data={"audio_language": "eng", "soundboard_replace_scope": "any"},
    )
    assert response.status_code == 200
    assert settings_holder.settings.render_defaults.soundboard_replace_scope == "any"


def test_settings_audio_save_rejects_invalid_soundboard_replace_scope(client, settings_holder):
    before = settings_holder.settings.render_defaults.soundboard_replace_scope
    response = client.post(
        "/settings/audio",
        data={"audio_language": "eng", "soundboard_replace_scope": "bogus"},
    )
    assert response.status_code == 200
    assert "couldn" in response.text.lower() or "error" in response.text.lower()
    assert settings_holder.settings.render_defaults.soundboard_replace_scope == before
