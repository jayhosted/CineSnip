"""Covers /render's response headers — the bot can't know
render_defaults.duration_seconds or its min/max clamp (worker-only config),
so the actual start/duration used must be echoed back the same way
X-Clip-Format/X-Clip-Style already are.
"""

from fastapi.testclient import TestClient

from app.settings import LibraryConfig, PathMapping, Settings
from app.worker import api as api_module
from app.worker.plex_client import MovieResult


class _FakePlexClient:
    def __init__(self, settings: Settings, movie: MovieResult) -> None:
        self._movie = movie

    def get_movie(self, rating_key: int) -> MovieResult:
        return self._movie


class _FakeRenderer:
    async def render_clip(self, *args, **kwargs) -> bytes:
        return b"clip-bytes"


def _settings(tmp_path) -> Settings:
    return Settings(
        discord_token="x", plex_url="http://localhost", plex_token="x",
        cache_dir=tmp_path / "cache",
        libraries=[
            LibraryConfig(
                name="Movies",
                path_mappings=[
                    PathMapping(plex_prefix="/media", container_path=str(tmp_path))
                ],
            )
        ],
    )


def _client(settings: Settings, monkeypatch, movie_path) -> TestClient:
    movie = MovieResult(
        rating_key=1,
        title="The Matrix",
        year=1999,
        duration_ms=8_160_000,
        thumb_url=None,
        plex_path=str(movie_path),
        guid="guid-1",
        library_name="Movies",
    )
    monkeypatch.setattr(api_module, "PlexClient", lambda s: _FakePlexClient(s, movie))
    app = api_module.create_app(settings)
    app.state.renderer = _FakeRenderer()
    return TestClient(app)


def test_render_echoes_actual_start_and_duration_for_bare_timecode(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    movie_path = tmp_path / "movie.mkv"
    movie_path.write_bytes(b"fake")
    client = _client(settings, monkeypatch, "/media/movie.mkv")

    response = client.post("/render", json={"rating_key": 1, "timecode": "62"})

    assert response.status_code == 200
    assert response.headers["X-Clip-Start"] == "62.0"
    assert float(response.headers["X-Clip-Duration"]) == settings.render_defaults.duration_seconds


def test_render_echoes_explicit_end_timecode_span(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    movie_path = tmp_path / "movie.mkv"
    movie_path.write_bytes(b"fake")
    client = _client(settings, monkeypatch, "/media/movie.mkv")

    response = client.post(
        "/render",
        json={"rating_key": 1, "timecode": "60", "end_timecode": "65"},
    )

    assert response.status_code == 200
    assert response.headers["X-Clip-Start"] == "60.0"
    assert response.headers["X-Clip-Duration"] == "5.0"


def test_render_accepts_explicit_start_and_end(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    movie_path = tmp_path / "movie.mkv"
    movie_path.write_bytes(b"fake")
    client = _client(settings, monkeypatch, "/media/movie.mkv")

    response = client.post("/render", json={"rating_key": 1, "start": 10.0, "end": 15.0})

    assert response.status_code == 200
    assert response.headers["X-Clip-Start"] == "10.0"
    assert response.headers["X-Clip-Duration"] == "5.0"


def test_render_rejects_explicit_span_outside_duration_bounds(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    movie_path = tmp_path / "movie.mkv"
    movie_path.write_bytes(b"fake")
    client = _client(settings, monkeypatch, "/media/movie.mkv")

    response = client.post(
        "/render",
        json={
            "rating_key": 1,
            "start": 10.0,
            "end": 10.0 + settings.render_defaults.max_duration_seconds + 1,
        },
    )

    assert response.status_code == 422


def test_render_rejects_end_before_start(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    movie_path = tmp_path / "movie.mkv"
    movie_path.write_bytes(b"fake")
    client = _client(settings, monkeypatch, "/media/movie.mkv")

    response = client.post("/render", json={"rating_key": 1, "start": 15.0, "end": 10.0})

    assert response.status_code == 422


def test_render_requires_start_and_end_together(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    movie_path = tmp_path / "movie.mkv"
    movie_path.write_bytes(b"fake")
    client = _client(settings, monkeypatch, "/media/movie.mkv")

    response = client.post("/render", json={"rating_key": 1, "start": 10.0})

    assert response.status_code == 422


def test_render_requires_timecode_or_start_end(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    movie_path = tmp_path / "movie.mkv"
    movie_path.write_bytes(b"fake")
    client = _client(settings, monkeypatch, "/media/movie.mkv")

    response = client.post("/render", json={"rating_key": 1})

    assert response.status_code == 422


def test_render_passes_subtitle_overrides_to_the_renderer(tmp_path, monkeypatch):
    import app.worker.api as api_module

    settings = _settings(tmp_path)
    movie_path = tmp_path / "movie.mkv"
    movie_path.write_bytes(b"fake")

    captured = {}

    class _CapturingRenderer:
        async def render_clip(self, *args, **kwargs):
            captured.update(kwargs)
            return b"clip-bytes"

    async def _fake_get_subtitles(*args, **kwargs):
        from app.worker.subtitles import SubtitleResult, SubtitleSource
        return SubtitleResult(
            guid="guid-1", source=SubtitleSource.SIDECAR, sidecar_path="x.srt",
            stream_index=None, entries=[],
        )

    monkeypatch.setattr(api_module, "get_subtitles", _fake_get_subtitles)
    client = _client(settings, monkeypatch, "/media/movie.mkv")
    client.app.state.renderer = _CapturingRenderer()

    response = client.post(
        "/render",
        json={
            "rating_key": 1,
            "start": 10.0,
            "end": 15.0,
            "style": "classic",
            "subtitle_overrides": {"3": None, "4": "edited"},
        },
    )

    assert response.status_code == 200
    assert captured["subtitle_overrides"] == {3: None, 4: "edited"}
