"""Covers /resolve-quote's `truncated` field — added alongside issue #7's
bot-side pagination so the bot can tell "this is every match" apart from
"the worker's fetch_limit was hit, there may be more" without guessing off
a raw count that could coincidentally equal fetch_limit on its own.
"""

from fastapi.testclient import TestClient

from app.settings import LibraryConfig, PathMapping, QuoteMatchDefaults, Settings
from app.worker import api as api_module
from app.worker.plex_client import MovieResult


class _FakePlexClient:
    def __init__(self, settings: Settings, movie: MovieResult) -> None:
        self._movie = movie

    def get_movie(self, rating_key: int) -> MovieResult:
        return self._movie


def _settings(tmp_path, fetch_limit: int) -> Settings:
    return Settings(
        discord_token="x", plex_url="http://localhost", plex_token="x",
        cache_dir=tmp_path / "cache",
        quote_match=QuoteMatchDefaults(fetch_limit=fetch_limit),
        libraries=[
            LibraryConfig(
                name="Movies",
                path_mappings=[
                    PathMapping(plex_prefix="/media", container_path=str(tmp_path))
                ],
            )
        ],
    )


def _client(settings: Settings, monkeypatch) -> TestClient:
    movie = MovieResult(
        rating_key=1,
        title="The Matrix",
        year=1999,
        duration_ms=8_160_000,
        thumb_url=None,
        plex_path="/media/movie.mkv",
        guid="guid-1",
        library_name="Movies",
    )
    monkeypatch.setattr(api_module, "PlexClient", lambda s: _FakePlexClient(s, movie))
    return TestClient(api_module.create_app(settings))


def _write_sidecar_srt(tmp_path, count: int, text: str) -> None:
    (tmp_path / "movie.mkv").write_bytes(b"fake")
    blocks = []
    for i in range(count):
        start = i * 5
        end = start + 2
        blocks.append(
            f"{i + 1}\n"
            f"00:00:{start:02d},000 --> 00:00:{end:02d},000\n"
            f"{text}\n"
        )
    (tmp_path / "movie.srt").write_text("\n".join(blocks))


def test_resolve_quote_endpoint_reports_truncated_when_more_than_fetch_limit(tmp_path, monkeypatch):
    settings = _settings(tmp_path, fetch_limit=3)
    _write_sidecar_srt(tmp_path, count=6, text="Nobody expects the Spanish Inquisition!")
    client = _client(settings, monkeypatch)

    resp = client.get("/resolve-quote/1", params={"quote": "nobody expects the spanish inquisition"})

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["matches"]) == 3
    assert body["truncated"] is True


def test_resolve_quote_endpoint_reports_not_truncated_when_within_fetch_limit(tmp_path, monkeypatch):
    settings = _settings(tmp_path, fetch_limit=8)
    _write_sidecar_srt(tmp_path, count=2, text="Nobody expects the Spanish Inquisition!")
    client = _client(settings, monkeypatch)

    resp = client.get("/resolve-quote/1", params={"quote": "nobody expects the spanish inquisition"})

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["matches"]) == 2
    assert body["truncated"] is False
