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
        discord_token="x", plex_url="http://x", plex_token="x",
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


def test_styles_list_shows_all_builtins(client):
    response = client.get("/settings/styles")
    assert response.status_code == 200
    for name in ("classic", "boxed", "cinematic", "meme"):
        assert name in response.text.lower()


def test_styles_save_creates_a_new_custom_preset(client, settings_holder):
    response = client.post(
        "/settings/styles/save",
        data={
            "original_name": "", "name": "simpsons", "font": "Simpsonfont",
            "font_size": "28", "primary_color": "&H0000FFFF",
            "outline_color": "&H00000000", "back_color": "&H00000000",
            "border_style": "1", "outline": "2.0", "shadow": "0.0",
            "margin_v": "24", "alignment": "2",
        },
    )
    assert response.status_code == 200
    assert "simpsons" in {cfg.name for cfg in settings_holder.settings.subtitle_styles}


def test_styles_save_edits_a_builtin_field_without_renaming(client, settings_holder):
    response = client.post(
        "/settings/styles/save",
        data={
            "original_name": "classic", "name": "classic", "font": "Liberation Sans",
            "font_size": "32", "primary_color": "&H00FFFFFF",
            "outline_color": "&H00000000", "back_color": "&H00000000",
            "border_style": "1", "outline": "2.0", "shadow": "0.0",
            "margin_v": "24", "alignment": "2",
        },
    )
    assert response.status_code == 200
    assert settings_holder.settings.style_presets()["classic"].font_size == 32


def test_styles_save_rejects_renaming_a_builtin(client, settings_holder):
    response = client.post(
        "/settings/styles/save",
        data={
            "original_name": "classic", "name": "classic2", "font": "Liberation Sans",
            "font_size": "26", "primary_color": "&H00FFFFFF",
            "outline_color": "&H00000000", "back_color": "&H00000000",
            "border_style": "1", "outline": "2.0", "shadow": "0.0",
            "margin_v": "24", "alignment": "2",
        },
    )
    assert response.status_code == 200
    assert "error" in response.text.lower() or "can't be renamed" in response.text.lower()
    assert "classic" in {cfg.name for cfg in settings_holder.settings.subtitle_styles}


def test_styles_delete_rejects_a_builtin(client, settings_holder):
    response = client.post("/settings/styles/classic/delete")
    assert response.status_code == 200
    assert "classic" in {cfg.name for cfg in settings_holder.settings.subtitle_styles}


def test_styles_delete_removes_a_custom_preset(client, settings_holder):
    client.post(
        "/settings/styles/save",
        data={
            "original_name": "", "name": "simpsons", "font": "Simpsonfont",
            "font_size": "28", "primary_color": "&H0000FFFF",
            "outline_color": "&H00000000", "back_color": "&H00000000",
            "border_style": "1", "outline": "2.0", "shadow": "0.0",
            "margin_v": "24", "alignment": "2",
        },
    )
    response = client.post("/settings/styles/simpsons/delete")
    assert response.status_code == 200
    assert "simpsons" not in {cfg.name for cfg in settings_holder.settings.subtitle_styles}


def test_styles_font_upload_rejects_a_non_font_file(client):
    response = client.post(
        "/settings/styles/classic/font",
        files={"font_file": ("not-a-font.ttf", b"definitely not a font", "font/ttf")},
    )
    assert response.status_code == 200
    assert "error" in response.text.lower() or "couldn't" in response.text.lower()


def test_styles_preview_proxies_the_worker_and_embeds_base64(client, monkeypatch):
    async def fake_preview_style(self, style):
        return b"\x89PNG\r\n\x1a\nfakepngbytes"

    monkeypatch.setattr(
        "app.bot.worker_client.WorkerClient.preview_style", fake_preview_style
    )
    response = client.post(
        "/settings/styles/preview",
        data={
            "font": "Liberation Sans", "font_size": "26",
            "primary_color": "&H00FFFFFF", "outline_color": "&H00000000",
            "back_color": "&H00000000", "border_style": "1",
            "outline": "2.0", "shadow": "0.0", "margin_v": "24", "alignment": "2",
        },
    )
    assert response.status_code == 200
    assert "data:image/png;base64," in response.text
