import httpx
import pytest
from fastapi import FastAPI
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient

from app.bot.worker_client import RenderResult, WorkerClient
from app.runtime import SettingsHolder
from app.settings import Settings
from app.web.generate import register_generate_routes

_TEMPLATES_DIR = "app/web/templates"


@pytest.fixture
def settings_holder():
    settings = Settings(discord_token="x", plex_url="http://x", plex_token="x")
    return SettingsHolder(settings=settings)


@pytest.fixture
def client(settings_holder):
    app = FastAPI()
    templates = Jinja2Templates(directory=_TEMPLATES_DIR)
    register_generate_routes(app, templates, settings_holder)
    return TestClient(app)


def test_select_endpoint_accepts_string_media_id(client):
    # media_id is opaque (str) post-rename — a Jellyfin-style non-numeric id
    # must round-trip through the hidden form field unmodified, not get
    # coerced/rejected the way an int(...) cast would have.
    response = client.get(
        "/generate/select?media_id=abc-123&kind=film&title=Film&year=&library_name=Movies"
    )
    assert response.status_code == 200
    assert 'name="media_id"' in response.text
    assert 'value="abc-123"' in response.text


def test_render_endpoint_passes_string_media_id_through_to_worker(client, monkeypatch):
    # /generate/render's form["media_id"] read is the other half of the
    # opaque-media_id contract /generate/select covers above — this proves
    # a non-numeric id round-trips through the POST body into
    # WorkerClient.render() unmodified, not coerced/rejected the way an
    # int(...) cast would have.
    captured = {}

    async def fake_render(self, media_id, timecode, **kwargs):
        captured["media_id"] = media_id
        return RenderResult(content=b"gif-bytes", format="gif", style="classic", start=0.0, duration=4.0)

    monkeypatch.setattr(WorkerClient, "render", fake_render)

    response = client.post(
        "/generate/render",
        data={"media_id": "abc-123", "timecode": "0:00:01"},
    )

    assert response.status_code == 200
    assert captured["media_id"] == "abc-123"


def test_generate_select_uses_worker_style_options(client, monkeypatch):
    # The style-pill grid only renders once a title is selected
    # (panel_generate_left.html's {% if selected %} form) — /generate/select
    # is the real route that reaches that render path, so exercise it
    # directly rather than the bare /generate page (which never shows the
    # style options with nothing selected yet).
    async def fake_style_options(self):
        return [("classic", "Classic"), ("simpsons", "Simpsons")]

    monkeypatch.setattr(WorkerClient, "style_options", fake_style_options)

    response = client.get(
        "/generate/select?media_id=abc-123&kind=film&title=Film&year=&library_name=Movies"
    )

    assert response.status_code == 200
    assert "Simpsons" in response.text


def test_generate_reset_calls_worker_style_options_without_error(client, monkeypatch):
    # panel_generate_left.html only shows the style grid once a title is
    # selected, so /generate/reset (which clears selection) can't assert on
    # rendered options — this just guards that the call site doesn't raise.
    async def fake_style_options(self):
        return [("classic", "Classic"), ("simpsons", "Simpsons")]

    monkeypatch.setattr(WorkerClient, "style_options", fake_style_options)

    response = client.get("/generate/reset?kind=film")

    assert response.status_code == 200


def test_generate_select_falls_back_when_worker_style_options_fails(client, monkeypatch):
    async def fake_style_options(self):
        raise httpx.HTTPError("boom")

    monkeypatch.setattr(WorkerClient, "style_options", fake_style_options)

    response = client.get(
        "/generate/select?media_id=abc-123&kind=film&title=Film&year=&library_name=Movies"
    )

    assert response.status_code == 200
    # panel_generate_left.html only renders each option's label up to its
    # first " (" (style-pill text), so assert on the raw radio value
    # instead of the full fallback label string.
    assert 'value="boxed"' in response.text
    assert "Simpsons" not in response.text
