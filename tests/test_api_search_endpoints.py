"""End-to-end FastAPI route coverage for /search-episodes-quote and
/search-quote-extend against a real search_index SQLite fixture.

These routes had no test exercising the actual endpoint before this file
(tests/test_library_search.py only ever calls search_cached_library()
directly) — which is exactly how api.py's call sites kept passing
search_cached_library() the old cache_dir-first signature unnoticed through
a full review cycle (see the Task 6 brief). These tests use the real
FastAPI TestClient against create_app() and a real search_index.db fixture,
with search_cached_library() itself never mocked, so a regression back to
the wrong first argument (or the wrong type) fails here.
"""

import asyncio

from fastapi.testclient import TestClient

from app.settings import Settings
from app.worker import api as api_module
from app.worker import search_index
from app.worker.media_client import MovieResult
from app.worker.subtitles import SubtitleEntry, SubtitleResult, SubtitleSource


class _FakePlexClient:
    """Stands in for the real PlexClient (which connects to a live Plex
    server in __init__) — both endpoints under test only ever reach the
    handful of attributes/methods defined here."""

    def __init__(self, settings: Settings, movie_items: list[MovieResult] | None = None) -> None:
        self.movie_library_names = frozenset({"Movies"})
        self.show_library_names = frozenset({"TV Shows"})
        self._episodes_by_show: dict[str, list[MovieResult]] = {}
        self._movie_items = movie_items or []
        self._movies_by_media_id: dict[str, MovieResult] = {}

    def list_episodes(self, show_media_id: str) -> list[MovieResult]:
        return self._episodes_by_show.get(show_media_id, [])

    def get_movie(self, media_id: str) -> MovieResult:
        from app.worker.media_client import MovieNotFoundError

        movie = self._movies_by_media_id.get(media_id)
        if movie is None:
            raise MovieNotFoundError(f"No movie with media_id {media_id}")
        return movie

    def get_episode(self, show_media_id: str, season: int, episode: int) -> MovieResult:
        # MovieResult carries no season/episode fields of its own (Episode
        # formatting bakes "S01E01" into .title instead — CLAUDE.md Section
        # 4) — tests register exactly the one episode under test per show,
        # so returning it unconditionally is enough to exercise this path.
        from app.worker.media_client import EpisodeNotFoundError

        episodes = self._episodes_by_show.get(show_media_id, [])
        if not episodes:
            raise EpisodeNotFoundError(
                f"No S{season:02d}E{episode:02d} for show {show_media_id}"
            )
        return episodes[0]

    def library_sections(self) -> list[tuple[str, object]]:
        return [("Movies", "movies-section")]

    def enumerate_section(self, section: object) -> list[MovieResult]:
        return self._movie_items


def _settings(tmp_path) -> Settings:
    return Settings(
        discord_token="x", plex_url="http://localhost", plex_token="x",
        cache_dir=tmp_path / "cache",
    )


def _write_title(db_path, guid, media_id, title, library_name, texts):
    entries = [
        SubtitleEntry(index=i + 1, start=float(i * 5), end=float(i * 5 + 2), text=text)
        for i, text in enumerate(texts)
    ]
    search_index.upsert_title(
        db_path,
        guid=guid,
        media_id=media_id,
        title=title,
        library_name=library_name,
        source="sidecar",
        sidecar_path=None,
        stream_index=None,
        entries=entries,
        fingerprint=None,
    )


def _client(settings: Settings, monkeypatch, fake_plex: _FakePlexClient | None = None) -> TestClient:
    monkeypatch.setattr(api_module, "create_media_client", lambda s: fake_plex or _FakePlexClient(s))
    app = api_module.create_app(settings)
    return TestClient(app)


def test_search_episodes_quote_endpoint_finds_real_cached_match(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    episode = MovieResult(
        media_id="501", title="The Office — S01E01 — Pilot", year=None,
        duration_ms=1000, thumb_url=None, source_path="D:\\TV\\office.mkv",
        guid="ep-guid-1", library_name="TV Shows",
    )
    _write_title(
        settings.quote_index_db_path,
        "ep-guid-1", 501, "The Office — S01E01 — Pilot", "TV Shows",
        ["That's what she said."],
    )

    fake_plex = _FakePlexClient(settings)
    fake_plex._episodes_by_show["900"] = [episode]
    client = _client(settings, monkeypatch, fake_plex)

    resp = client.get("/search-episodes-quote/900", params={"quote": "that's what she said"})

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["matches"]) == 1
    assert body["matches"][0]["media_id"] == "501"


def test_search_episodes_quote_endpoint_no_matches_is_empty_not_error(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    episode = MovieResult(
        media_id="501", title="The Office — S01E01 — Pilot", year=None,
        duration_ms=1000, thumb_url=None, source_path="D:\\TV\\office.mkv",
        guid="ep-guid-1", library_name="TV Shows",
    )
    _write_title(
        settings.quote_index_db_path,
        "ep-guid-1", 501, "The Office — S01E01 — Pilot", "TV Shows",
        ["That's what she said."],
    )

    fake_plex = _FakePlexClient(settings)
    fake_plex._episodes_by_show["900"] = [episode]
    client = _client(settings, monkeypatch, fake_plex)

    resp = client.get("/search-episodes-quote/900", params={"quote": "a phrase that appears nowhere"})

    assert resp.status_code == 200
    assert resp.json()["matches"] == []


def test_search_episodes_quote_endpoint_skips_get_subtitles_for_fresh_cached_episode(
    tmp_path, monkeypatch
):
    """_ensure_all_episodes_cached bulk-preloads (source, sidecar_path,
    fingerprint) for the whole show in one query, then _ensure_episode_cached
    must skip get_subtitles() entirely for an episode that pre-check finds
    already fresh — that's the whole point of the preload (get_subtitles()
    would otherwise open 3 more short-lived SQLite connections per episode,
    and its own get_entries() call is pure waste here since search reads
    straight from search_index, never from this function's return value).
    A NONE-sourced episode (no subtitles found previously) is always fresh
    with no file to check, making it the simplest case to prove this with.
    """
    settings = _settings(tmp_path)
    episode = MovieResult(
        media_id="501", title="The Office — S01E01 — Pilot", year=None,
        duration_ms=1000, thumb_url=None, source_path="D:\\TV\\office.mkv",
        guid="ep-guid-1", library_name="TV Shows",
    )
    search_index.upsert_title(
        settings.quote_index_db_path,
        guid="ep-guid-1", media_id=501, title=episode.title, library_name="TV Shows",
        source="none", sidecar_path=None, stream_index=None, entries=[], fingerprint=None,
    )

    fake_plex = _FakePlexClient(settings)
    fake_plex._episodes_by_show["900"] = [episode]
    client = _client(settings, monkeypatch, fake_plex)

    monkeypatch.setattr(api_module, "_resolve_container_path", lambda movie, settings: "fake-path")
    monkeypatch.setattr(api_module.os.path, "exists", lambda path: True)
    monkeypatch.setattr(api_module, "find_sidecar_subtitle", lambda path: None)

    def fail_get_subtitles(*args, **kwargs):
        raise AssertionError("get_subtitles must not be called for an already-fresh episode")

    monkeypatch.setattr(api_module, "get_subtitles", fail_get_subtitles)

    resp = client.get("/search-episodes-quote/900", params={"quote": "anything"})

    assert resp.status_code == 200


def test_search_episodes_quote_endpoint_still_calls_get_subtitles_for_uncached_episode(
    tmp_path, monkeypatch
):
    """The bulk pre-check must only skip get_subtitles() for episodes the
    bulk query actually found a fresh row for — an episode with no title
    row at all (never touched by any flow) is absent from
    get_cache_metadata_bulk's result, so it must still fall through to the
    full get_subtitles() extraction path exactly as before."""
    settings = _settings(tmp_path)
    episode = MovieResult(
        media_id="501", title="The Office — S01E01 — Pilot", year=None,
        duration_ms=1000, thumb_url=None, source_path="D:\\TV\\office.mkv",
        guid="ep-guid-1", library_name="TV Shows",
    )
    fake_plex = _FakePlexClient(settings)
    fake_plex._episodes_by_show["900"] = [episode]
    client = _client(settings, monkeypatch, fake_plex)

    monkeypatch.setattr(api_module, "_resolve_container_path", lambda movie, settings: "fake-path")
    monkeypatch.setattr(api_module.os.path, "exists", lambda path: True)

    calls = []

    async def fake_get_subtitles(movie, container_path, cache_dir, db_path, **kwargs):
        calls.append(movie.guid)
        return SubtitleResult(guid=movie.guid, source=SubtitleSource.NONE, entries=[])

    monkeypatch.setattr(api_module, "get_subtitles", fake_get_subtitles)

    resp = client.get("/search-episodes-quote/900", params={"quote": "anything"})

    assert resp.status_code == 200
    assert calls == ["ep-guid-1"]


def test_search_episodes_quote_endpoint_checks_episodes_with_bounded_concurrency(
    tmp_path, monkeypatch
):
    # Real-world regression: a 279-episode show measured ~34s here purely
    # from checking each episode's cache status one at a time, identical
    # whether the show was cold or fully warm — the per-episode check
    # itself is cheap (a stat + a SQLite read), serializing it wasn't. This
    # proves the fix without needing real ffmpeg/filesystem/Plex: episodes
    # must run with real concurrency (max_in_flight > 1), but still capped
    # at _EPISODE_CACHE_CHECK_CONCURRENCY so a never-touched show can't
    # hammer ffmpeg with unbounded simultaneous extractions.
    settings = _settings(tmp_path)
    episode_count = api_module._EPISODE_CACHE_CHECK_CONCURRENCY * 2
    episodes = [
        MovieResult(
            media_id=str(700 + i), title=f"Show — S01E{i:02d} — Ep", year=None,
            duration_ms=1000, thumb_url=None, source_path=f"D:\\TV\\show\\ep{i}.mkv",
            guid=f"ep-guid-{i}", library_name="TV Shows",
        )
        for i in range(episode_count)
    ]
    fake_plex = _FakePlexClient(settings)
    fake_plex._episodes_by_show["900"] = episodes
    client = _client(settings, monkeypatch, fake_plex)

    # Bypass real path-mapping/filesystem checks — this test is only about
    # _ensure_all_episodes_cached's concurrency, not path resolution.
    monkeypatch.setattr(api_module, "_resolve_container_path", lambda movie, settings: "fake-path")
    monkeypatch.setattr(api_module.os.path, "exists", lambda path: True)

    in_flight = 0
    max_in_flight = 0
    call_count = 0

    async def fake_get_subtitles(movie, container_path, cache_dir, db_path, **kwargs):
        nonlocal in_flight, max_in_flight, call_count
        call_count += 1
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        await asyncio.sleep(0.01)  # real yield, so other episodes can actually interleave
        in_flight -= 1
        return SubtitleResult(guid=movie.guid, source=SubtitleSource.NONE, entries=[])

    monkeypatch.setattr(api_module, "get_subtitles", fake_get_subtitles)

    resp = client.get("/search-episodes-quote/900", params={"quote": "anything"})

    assert resp.status_code == 200
    assert call_count == episode_count
    assert max_in_flight > 1  # real concurrency, not the old strictly-sequential loop
    assert max_in_flight <= api_module._EPISODE_CACHE_CHECK_CONCURRENCY  # still bounded


import json

from app.settings import LibraryConfig, PathMapping, QuoteMatchDefaults
# SubtitleEntry is already imported at the top of this file (see the
# existing `from app.worker.subtitles import SubtitleEntry` import used by
# `_write_title`) — reuse it, don't re-import under a different name.


def _movie_item(guid, media_id, title="Film", library_name="Movies", source_path="D:\\Movies\\film.mkv"):
    return MovieResult(
        media_id=str(media_id),
        title=title,
        year=2000,
        duration_ms=1000,
        thumb_url=None,
        source_path=source_path,
        guid=guid,
        library_name=library_name,
    )


def _settings_with_sync(tmp_path, enabled: bool, mount_root=None) -> Settings:
    libraries = []
    if mount_root is not None:
        libraries = [
            LibraryConfig(
                name="Movies",
                path_mappings=[PathMapping(path_prefix="D:\\Movies", container_path=str(mount_root))],
            )
        ]
    return Settings(
        discord_token="x",
        plex_url="http://localhost",
        plex_token="x",
        cache_dir=tmp_path / "cache",
        libraries=libraries,
        library_sync={"enabled": enabled},
    )


def _ndjson_lines(resp) -> list[dict]:
    return [json.loads(line) for line in resp.text.strip().split("\n") if line]


def test_search_quote_extend_is_cache_only(tmp_path, monkeypatch):
    """/search-quote-extend never touches Plex — library_sync (the 24h
    scheduled pass or a manual "Sync now") is solely responsible for
    keeping quote_index.db current. A title enumerate_section() would find
    live on Plex but that isn't cached yet must NOT appear, and Plex must
    never be called at all — verified by making enumerate_section raise if
    it's ever reached.
    """
    settings = _settings_with_sync(tmp_path, enabled=True)
    _write_title(
        settings.quote_index_db_path,
        "guid-1", 101, "Monty Python", "Movies",
        ["Nobody expects the Spanish Inquisition!"],
    )

    class _NoPlexCallsAllowed(_FakePlexClient):
        def enumerate_section(self, section):
            raise AssertionError("search-quote-extend must never enumerate Plex")

        def current_section_updated_ats(self, names=None):
            raise AssertionError("search-quote-extend must never check Plex for changes")

    fake_plex = _NoPlexCallsAllowed(settings, movie_items=[_movie_item("guid-2", 102, "Uncached Film")])
    client = _client(settings, monkeypatch, fake_plex)

    resp = client.get("/search-quote-extend", params={"quote": "nobody expects"})

    assert resp.status_code == 200
    events = _ndjson_lines(resp)
    assert [e["type"] for e in events] == ["cached", "final"]
    assert events[0]["matches"][0]["title"] == "Monty Python"
    assert events[1]["remaining_uncached"] is None
    assert events[1]["matches"] == events[0]["matches"]


def test_search_quote_extend_media_defaults_to_all(tmp_path, monkeypatch):
    settings = _settings_with_sync(tmp_path, enabled=True)
    _write_title(
        settings.quote_index_db_path,
        "guid-1", 101, "Some Show", "TV Shows",
        ["Nobody expects the Spanish Inquisition!"],
    )
    client = _client(settings, monkeypatch)

    resp = client.get("/search-quote-extend", params={"quote": "nobody expects"})

    events = _ndjson_lines(resp)
    assert events[-1]["matches"][0]["title"] == "Some Show"


def test_search_quote_extend_media_movie_excludes_tv(tmp_path, monkeypatch):
    settings = _settings_with_sync(tmp_path, enabled=True)
    _write_title(
        settings.quote_index_db_path,
        "guid-1", 101, "Some Show", "TV Shows",
        ["Nobody expects the Spanish Inquisition!"],
    )
    client = _client(settings, monkeypatch)

    resp = client.get(
        "/search-quote-extend", params={"quote": "nobody expects", "media": "movie"}
    )

    events = _ndjson_lines(resp)
    assert events[-1]["matches"] == []


def test_search_quote_extend_media_tv_excludes_movies(tmp_path, monkeypatch):
    settings = _settings_with_sync(tmp_path, enabled=True)
    _write_title(
        settings.quote_index_db_path,
        "guid-1", 101, "Monty Python", "Movies",
        ["Nobody expects the Spanish Inquisition!"],
    )
    client = _client(settings, monkeypatch)

    resp = client.get(
        "/search-quote-extend", params={"quote": "nobody expects", "media": "tv"}
    )

    events = _ndjson_lines(resp)
    assert events[-1]["matches"] == []


def test_random_quote_endpoint_no_quote_returns_a_cached_line(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    _write_title(
        settings.quote_index_db_path,
        "guid-1", 101, "Monty Python", "Movies",
        ["Nobody expects the Spanish Inquisition!"],
    )
    client = _client(settings, monkeypatch)

    resp = client.get("/random-quote", params={"media": "movie"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["media_id"] == "101"
    assert body["text"] == "Nobody expects the Spanish Inquisition!"


def test_random_quote_endpoint_media_movie_excludes_tv(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    _write_title(
        settings.quote_index_db_path,
        "guid-2", 202, "Some Show", "TV Shows",
        ["Only line in the whole cache."],
    )
    client = _client(settings, monkeypatch)

    resp = client.get("/random-quote", params={"media": "movie"})

    assert resp.status_code == 404


def test_random_quote_endpoint_media_all_includes_tv(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    _write_title(
        settings.quote_index_db_path,
        "guid-2", 202, "Some Show", "TV Shows",
        ["Only line in the whole cache."],
    )
    client = _client(settings, monkeypatch)

    resp = client.get("/random-quote", params={"media": "all"})

    assert resp.status_code == 200
    assert resp.json()["media_id"] == "202"


def test_random_quote_endpoint_with_quote_only_returns_whole_word_match(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    _write_title(
        settings.quote_index_db_path,
        "guid-1", 101, "Cat Film", "Movies",
        ["The cat sat on the mat."],
    )
    _write_title(
        settings.quote_index_db_path,
        "guid-2", 102, "Unrelated Film", "Movies",
        ["The file was concatenated."],
    )
    client = _client(settings, monkeypatch)

    resp = client.get("/random-quote", params={"quote": "cat", "media": "movie"})

    assert resp.status_code == 200
    assert resp.json()["media_id"] == "101"


def test_random_quote_endpoint_returns_404_when_nothing_cached(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    client = _client(settings, monkeypatch)

    resp = client.get("/random-quote", params={"media": "all"})

    assert resp.status_code == 404


def test_search_episodes_quote_endpoint_reports_truncated_when_more_than_fetch_limit(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    settings.quote_match = QuoteMatchDefaults(fetch_limit=2)
    episodes = []
    for i in range(4):
        episode = MovieResult(
            media_id=str(500 + i), title=f"The Office — S01E0{i} — Ep{i}", year=None,
            duration_ms=1000, thumb_url=None, source_path=f"D:\\TV\\office{i}.mkv",
            guid=f"ep-guid-{i}", library_name="TV Shows",
        )
        episodes.append(episode)
        _write_title(
            settings.quote_index_db_path,
            f"ep-guid-{i}", 500 + i, f"The Office — S01E0{i} — Ep{i}", "TV Shows",
            ["That's what she said."],
        )

    fake_plex = _FakePlexClient(settings)
    fake_plex._episodes_by_show["900"] = episodes
    client = _client(settings, monkeypatch, fake_plex)

    resp = client.get("/search-episodes-quote/900", params={"quote": "that's what she said"})

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["matches"]) == 2
    assert body["truncated"] is True


def test_random_line_endpoint_filters_short_lines_and_returns_pool_info(tmp_path, monkeypatch):
    mount_root = tmp_path / "media"
    mount_root.mkdir()
    (mount_root / "film.mkv").write_bytes(b"x")
    settings = _settings_with_sync(tmp_path, enabled=False, mount_root=mount_root)
    _write_title(
        settings.quote_index_db_path,
        "guid-1", 101, "Monty Python", "Movies",
        ["Okay.", "Nobody expects the Spanish Inquisition!"],
    )
    fake_plex = _FakePlexClient(settings)
    fake_plex._movies_by_media_id["101"] = _movie_item("guid-1", 101, "Monty Python")
    client = _client(settings, monkeypatch, fake_plex)

    resp = client.get("/random-line/101")

    assert resp.status_code == 200
    body = resp.json()
    assert body["media_id"] == "101"
    # "Okay." is under the default random_min_words (3) and must never be
    # picked — the only eligible line is the long one.
    assert body["text"] == "Nobody expects the Spanish Inquisition!"
    assert body["pool_size"] == 1
    assert isinstance(body["entry_id"], int)


def test_random_line_endpoint_404_when_media_id_unknown(tmp_path, monkeypatch):
    settings = _settings_with_sync(tmp_path, enabled=False)
    fake_plex = _FakePlexClient(settings)
    client = _client(settings, monkeypatch, fake_plex)

    resp = client.get("/random-line/999")

    assert resp.status_code == 404


def test_random_line_show_endpoint_whole_show_picks_from_any_episode(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    episode = MovieResult(
        media_id="501", title="The Office — S01E01 — Pilot", year=None,
        duration_ms=1000, thumb_url=None, source_path="D:\\TV\\office.mkv",
        guid="ep-guid-1", library_name="TV Shows",
    )
    _write_title(
        settings.quote_index_db_path,
        "ep-guid-1", 501, "The Office — S01E01 — Pilot", "TV Shows",
        ["That's what she said."],
    )
    fake_plex = _FakePlexClient(settings)
    fake_plex._episodes_by_show["900"] = [episode]
    client = _client(settings, monkeypatch, fake_plex)

    resp = client.get("/random-line-show/900")

    assert resp.status_code == 200
    body = resp.json()
    assert body["media_id"] == "501"
    assert body["text"] == "That's what she said."


def test_random_line_show_endpoint_single_episode_scope(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    ep1 = MovieResult(
        media_id="501", title="The Office — S01E01 — Pilot", year=None,
        duration_ms=1000, thumb_url=None, source_path="D:\\TV\\office1.mkv",
        guid="ep-guid-1", library_name="TV Shows",
    )
    _write_title(
        settings.quote_index_db_path, "ep-guid-1", 501, ep1.title, "TV Shows", ["Line from episode one."],
    )
    fake_plex = _FakePlexClient(settings)
    fake_plex._episodes_by_show["900"] = [ep1]
    client = _client(settings, monkeypatch, fake_plex)

    resp = client.get("/random-line-show/900", params={"season": 1, "episode": 1})

    assert resp.status_code == 200
    assert resp.json()["media_id"] == "501"


def test_random_line_show_endpoint_requires_both_season_and_episode(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    client = _client(settings, monkeypatch)

    resp = client.get("/random-line-show/900", params={"season": 1})

    assert resp.status_code == 422


def test_random_line_show_endpoint_404_when_show_unknown(tmp_path, monkeypatch):
    settings = _settings(tmp_path)

    class _NoShowPlex(_FakePlexClient):
        def list_episodes(self, show_media_id: str) -> list[MovieResult]:
            from app.worker.media_client import ShowNotFoundError

            raise ShowNotFoundError(f"No show {show_media_id}")

    client = _client(settings, monkeypatch, _NoShowPlex(settings))

    resp = client.get("/random-line-show/900")

    assert resp.status_code == 404


def test_random_line_show_endpoint_404_when_show_unknown_single_episode_scope(tmp_path, monkeypatch):
    # get_episode() (unlike list_episodes()) can also raise ShowNotFoundError
    # — on Jellyfin, a 404 on /Shows/{id}/Episodes means the show itself
    # doesn't exist, distinct from EpisodeNotFoundError (the show exists but
    # has no such season/episode). This call site only caught the latter
    # (ultrareview finding, issue #24) — a stale show media_id in the
    # season+episode branch leaked as an unhandled 500.
    settings = _settings(tmp_path)

    class _NoShowPlex(_FakePlexClient):
        def get_episode(self, show_media_id: str, season: int, episode: int) -> MovieResult:
            from app.worker.media_client import ShowNotFoundError

            raise ShowNotFoundError(f"No show {show_media_id}")

    client = _client(settings, monkeypatch, _NoShowPlex(settings))

    resp = client.get("/random-line-show/900", params={"season": 1, "episode": 1})

    assert resp.status_code == 404


def test_resolve_episode_endpoint_404_when_show_unknown(tmp_path, monkeypatch):
    # Same gap as random-line-show above, in /resolve-episode's own
    # get_episode() call site (ultrareview finding, issue #24).
    settings = _settings(tmp_path)

    class _NoShowPlex(_FakePlexClient):
        def get_episode(self, show_media_id: str, season: int, episode: int) -> MovieResult:
            from app.worker.media_client import ShowNotFoundError

            raise ShowNotFoundError(f"No show {show_media_id}")

    client = _client(settings, monkeypatch, _NoShowPlex(settings))

    resp = client.get("/resolve-episode/900", params={"season": 1, "episode": 1})

    assert resp.status_code == 404
