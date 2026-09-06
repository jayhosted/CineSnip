import pytest
from fastapi.testclient import TestClient

from app.settings import Settings
from app.worker import api as api_module
from app.worker.api import create_app


class _FakeMedia:
    # A bare object() (used elsewhere in this file) lacks movie_library_names,
    # which the preview-background route needs — a minimal stand-in with an
    # empty library set by default (candidate-free, matching object()'s
    # "no real media backend" spirit for tests that don't care about it).
    movie_library_names: frozenset[str] = frozenset()
    show_library_names: frozenset[str] = frozenset()


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
    monkeypatch.setattr(api_module, "create_media_client", lambda s: _FakeMedia())
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


def test_preview_background_returns_404_with_no_cached_movies(client):
    # _FakeMedia's movie_library_names is empty and the test settings'
    # quote_index.db doesn't exist yet — pick_random_movie_frame_source
    # has nothing to work with, which must surface as a clean 404, not a
    # 500 from some downstream None-handling gap.
    response = client.get("/style-presets/preview-background")
    assert response.status_code == 404


def test_preview_background_returns_a_movie_and_background_id(client, monkeypatch):
    from app.worker.media_client import MovieResult

    movie = MovieResult(
        media_id="1", title="A Popular Movie", year=2020, duration_ms=6_000_000,
        thumb_url=None, source_path="/media/movies/movie.mp4", guid="guid-1",
        library_name="Movies",
    )

    async def fake_pick(media, settings, db_path):
        return movie, "/resolved/movie.mp4"

    async def fake_extract(container_path, scratch_dir, timeout_seconds=30.0):
        from pathlib import Path
        path = Path(scratch_dir) / "style-preview-bg-deadbeefdeadbeefdeadbeefdeadbeef.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\x89PNG\r\n\x1a\nfakepngbytes")
        return path

    monkeypatch.setattr(api_module, "pick_random_movie_frame_source", fake_pick)
    monkeypatch.setattr(api_module, "extract_background_frame", fake_extract)

    response = client.get("/style-presets/preview-background")
    assert response.status_code == 200
    body = response.json()
    assert body["title"] == "A Popular Movie"
    assert body["background_id"] == "deadbeefdeadbeefdeadbeefdeadbeef"


def test_preview_style_ignores_a_malformed_background_id(client, settings, monkeypatch):
    captured = {}

    async def fake_render(style, fonts_dir, scratch_dir, timeout_seconds=30.0, background_path=None):
        captured["background_path"] = background_path
        return b"\x89PNG\r\n\x1a\nfakepngbytes"

    monkeypatch.setattr(api_module, "render_style_preview", fake_render)

    response = client.post(
        "/style-presets/preview",
        json={
            "font": "Liberation Sans", "font_size": 26,
            "primary_color": "&H00FFFFFF", "outline_color": "&H00000000",
            "back_color": "&H00000000", "border_style": 1,
            "outline": 2.0, "shadow": 0.0, "bold": False,
            "uppercase": False, "margin_v": 24,
            "background_id": "../../etc/passwd",
        },
    )
    assert response.status_code == 200
    assert captured["background_path"] is None


def test_preview_style_uses_a_valid_existing_background_id(client, settings, monkeypatch):
    from app.worker.style_preview import background_frame_path

    background_id = "deadbeefdeadbeefdeadbeefdeadbeef"
    frame_path = background_frame_path(settings.scratch_dir, background_id)
    frame_path.parent.mkdir(parents=True, exist_ok=True)
    frame_path.write_bytes(b"\x89PNG\r\n\x1a\nrealframe")

    captured = {}

    async def fake_render(style, fonts_dir, scratch_dir, timeout_seconds=30.0, background_path=None):
        captured["background_path"] = background_path
        return b"\x89PNG\r\n\x1a\nfakepngbytes"

    monkeypatch.setattr(api_module, "render_style_preview", fake_render)

    response = client.post(
        "/style-presets/preview",
        json={
            "font": "Liberation Sans", "font_size": 26,
            "primary_color": "&H00FFFFFF", "outline_color": "&H00000000",
            "back_color": "&H00000000", "border_style": 1,
            "outline": 2.0, "shadow": 0.0, "bold": False,
            "uppercase": False, "margin_v": 24,
            "background_id": background_id,
        },
    )
    assert response.status_code == 200
    assert captured["background_path"] == frame_path


def test_preview_style_ignores_a_valid_id_pattern_with_no_matching_file(client, settings, monkeypatch):
    captured = {}

    async def fake_render(style, fonts_dir, scratch_dir, timeout_seconds=30.0, background_path=None):
        captured["background_path"] = background_path
        return b"\x89PNG\r\n\x1a\nfakepngbytes"

    monkeypatch.setattr(api_module, "render_style_preview", fake_render)

    response = client.post(
        "/style-presets/preview",
        json={
            "font": "Liberation Sans", "font_size": 26,
            "primary_color": "&H00FFFFFF", "outline_color": "&H00000000",
            "back_color": "&H00000000", "border_style": 1,
            "outline": 2.0, "shadow": 0.0, "bold": False,
            "uppercase": False, "margin_v": 24,
            "background_id": "a" * 32,
        },
    )
    assert response.status_code == 200
    assert captured["background_path"] is None


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
