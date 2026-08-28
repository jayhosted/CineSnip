"""End-to-end FastAPI route coverage for /search-quote and
/search-episodes-quote against a real search_index SQLite fixture.

Neither route had any test exercising the actual endpoint before this file
(tests/test_library_search.py only ever calls search_cached_library()
directly) — which is exactly how api.py's two call sites kept passing
search_cached_library() the old cache_dir-first signature unnoticed through
a full review cycle (see the Task 6 brief). These tests use the real
FastAPI TestClient against create_app() and a real search_index.db fixture,
with search_cached_library() itself never mocked, so a regression back to
the wrong first argument (or the wrong type) fails here.
"""

from fastapi.testclient import TestClient

from app.settings import Settings
from app.worker import api as api_module
from app.worker import search_index
from app.worker.plex_client import MovieResult
from app.worker.subtitles import SubtitleEntry


class _FakePlexClient:
    """Stands in for the real PlexClient (which connects to a live Plex
    server in __init__) — both endpoints under test only ever reach the
    handful of attributes/methods defined here."""

    def __init__(self, settings: Settings) -> None:
        self.movie_library_names = frozenset({"Movies"})
        self._episodes_by_show: dict[int, list[MovieResult]] = {}

    def list_episodes(self, show_rating_key: int) -> list[MovieResult]:
        return self._episodes_by_show.get(show_rating_key, [])


def _settings(tmp_path) -> Settings:
    return Settings(
        discord_token="x", plex_url="http://localhost", plex_token="x",
        cache_dir=tmp_path / "cache",
    )


def _write_title(db_path, guid, rating_key, title, library_name, texts):
    entries = [
        SubtitleEntry(index=i + 1, start=float(i * 5), end=float(i * 5 + 2), text=text)
        for i, text in enumerate(texts)
    ]
    search_index.upsert_title(
        db_path,
        guid=guid,
        rating_key=rating_key,
        title=title,
        library_name=library_name,
        source="sidecar",
        sidecar_path=None,
        stream_index=None,
        entries=entries,
        fingerprint=None,
    )


def _client(settings: Settings, monkeypatch, fake_plex: _FakePlexClient | None = None) -> TestClient:
    monkeypatch.setattr(api_module, "PlexClient", lambda s: fake_plex or _FakePlexClient(s))
    app = api_module.create_app(settings)
    return TestClient(app)


def test_search_quote_endpoint_finds_real_cached_match(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    _write_title(
        settings.quote_index_db_path,
        "guid-1", 101, "Monty Python", "Movies",
        ["Nobody expects the Spanish Inquisition!"],
    )

    client = _client(settings, monkeypatch)
    resp = client.get("/search-quote", params={"quote": "nobody expects the spanish inquisition"})

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["matches"]) == 1
    assert body["matches"][0]["rating_key"] == 101
    assert body["matches"][0]["title"] == "Monty Python"


def test_search_quote_endpoint_filters_to_movie_libraries(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    # Same line cached under a non-movie library name — /search-quote is
    # documented movie-only (CLAUDE.md Section 5's TV-leak fix), so this
    # must not appear even though it matches on text.
    _write_title(
        settings.quote_index_db_path,
        "guid-2", 202, "Some Show", "TV Shows",
        ["Nobody expects the Spanish Inquisition!"],
    )

    client = _client(settings, monkeypatch)
    resp = client.get("/search-quote", params={"quote": "nobody expects the spanish inquisition"})

    assert resp.status_code == 200
    assert resp.json()["matches"] == []


def test_search_quote_endpoint_no_matches_is_empty_not_error(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    client = _client(settings, monkeypatch)

    resp = client.get("/search-quote", params={"quote": "a phrase that appears nowhere"})

    assert resp.status_code == 200
    assert resp.json()["matches"] == []


def test_search_episodes_quote_endpoint_finds_real_cached_match(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    episode = MovieResult(
        rating_key=501, title="The Office — S01E01 — Pilot", year=None,
        duration_ms=1000, thumb_url=None, plex_path="D:\\TV\\office.mkv",
        guid="ep-guid-1", library_name="TV Shows",
    )
    _write_title(
        settings.quote_index_db_path,
        "ep-guid-1", 501, "The Office — S01E01 — Pilot", "TV Shows",
        ["That's what she said."],
    )

    fake_plex = _FakePlexClient(settings)
    fake_plex._episodes_by_show[900] = [episode]
    client = _client(settings, monkeypatch, fake_plex)

    resp = client.get("/search-episodes-quote/900", params={"quote": "that's what she said"})

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["matches"]) == 1
    assert body["matches"][0]["rating_key"] == 501


def test_search_episodes_quote_endpoint_no_matches_is_empty_not_error(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    episode = MovieResult(
        rating_key=501, title="The Office — S01E01 — Pilot", year=None,
        duration_ms=1000, thumb_url=None, plex_path="D:\\TV\\office.mkv",
        guid="ep-guid-1", library_name="TV Shows",
    )
    _write_title(
        settings.quote_index_db_path,
        "ep-guid-1", 501, "The Office — S01E01 — Pilot", "TV Shows",
        ["That's what she said."],
    )

    fake_plex = _FakePlexClient(settings)
    fake_plex._episodes_by_show[900] = [episode]
    client = _client(settings, monkeypatch, fake_plex)

    resp = client.get("/search-episodes-quote/900", params={"quote": "a phrase that appears nowhere"})

    assert resp.status_code == 200
    assert resp.json()["matches"] == []
