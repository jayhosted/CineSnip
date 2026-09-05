import pytest
from fastapi.testclient import TestClient

from app.settings import Settings
from app.worker import api as api_module
from app.worker.api import create_app


@pytest.fixture
def settings(tmp_path):
    return Settings(
        discord_token="x", plex_url="http://x", plex_token="x",
        cache_dir=tmp_path / "cache", scratch_dir=tmp_path / "scratch",
    )


@pytest.fixture
def client(settings, monkeypatch):
    # create_app() eagerly constructs a media client, which for a real
    # PlexClient means connecting over the network — irrelevant to these
    # style-preset routes, so stub it out like the other api tests do.
    monkeypatch.setattr(api_module, "create_media_client", lambda s: object())
    return TestClient(create_app(settings))


def test_get_style_presets_lists_builtins(client):
    response = client.get("/style-presets")
    assert response.status_code == 200
    items = response.json()
    names = {item["name"] for item in items}
    # "none"/"No Subtitles" is a real, selectable style choice for the bot's
    # and web app's dropdowns (CLAUDE.md Section 2), not an absence of one.
    assert names == {"classic", "boxed", "cinematic", "meme", "none"}
    labels = {item["name"]: item["label"] for item in items}
    assert labels["none"] == "No Subtitles"


def test_preview_style_returns_a_png(client):
    response = client.post(
        "/style-presets/preview",
        json={
            "font": "Liberation Sans", "font_size": 26,
            "primary_color": "&H00FFFFFF", "outline_color": "&H00000000",
            "back_color": "&H00000000", "border_style": 1,
            "outline": 2.0, "shadow": 0.0, "bold": False,
            "uppercase": False, "margin_v": 24,
        },
    )
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content[:8] == b"\x89PNG\r\n\x1a\n"
