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

    def __init__(self, settings: Settings, movie_items: list[MovieResult] | None = None) -> None:
        self.movie_library_names = frozenset({"Movies"})
        self.show_library_names = frozenset({"TV Shows"})
        self._episodes_by_show: dict[int, list[MovieResult]] = {}
        self._movie_items = movie_items or []
        self._movies_by_rating_key: dict[int, MovieResult] = {}

    def list_episodes(self, show_rating_key: int) -> list[MovieResult]:
        return self._episodes_by_show.get(show_rating_key, [])

    def get_movie(self, rating_key: int) -> MovieResult:
        from app.worker.plex_client import MovieNotFoundError

        movie = self._movies_by_rating_key.get(rating_key)
        if movie is None:
            raise MovieNotFoundError(f"No movie with rating_key {rating_key}")
        return movie

    def get_episode(self, show_rating_key: int, season: int, episode: int) -> MovieResult:
        # MovieResult carries no season/episode fields of its own (Episode
        # formatting bakes "S01E01" into .title instead — CLAUDE.md Section
        # 4) — tests register exactly the one episode under test per show,
        # so returning it unconditionally is enough to exercise this path.
        from app.worker.plex_client import EpisodeNotFoundError

        episodes = self._episodes_by_show.get(show_rating_key, [])
        if not episodes:
            raise EpisodeNotFoundError(
                f"No S{season:02d}E{episode:02d} for show {show_rating_key}"
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


import json

from app.settings import LibraryConfig, PathMapping, QuoteMatchDefaults
from app.worker import library_sync as library_sync_module
from app.worker.quote_index import upsert_no_subtitle_title
from app.worker.subtitles import SubtitleResult, SubtitleSource
# SubtitleEntry is already imported at the top of this file (see the
# existing `from app.worker.subtitles import SubtitleEntry` import used by
# `_write_title`) — reuse it, don't re-import under a different name.


def _movie_item(guid, rating_key, title="Film", library_name="Movies", plex_path="D:\\Movies\\film.mkv"):
    return MovieResult(
        rating_key=rating_key,
        title=title,
        year=2000,
        duration_ms=1000,
        thumb_url=None,
        plex_path=plex_path,
        guid=guid,
        library_name=library_name,
    )


def _settings_with_sync(tmp_path, enabled: bool, cap: int | None = None, mount_root=None) -> Settings:
    kwargs = {}
    if cap is not None:
        kwargs["quote_match"] = QuoteMatchDefaults(library_extend_cap=cap)
    libraries = []
    if mount_root is not None:
        libraries = [
            LibraryConfig(
                name="Movies",
                path_mappings=[PathMapping(plex_prefix="D:\\Movies", container_path=str(mount_root))],
            )
        ]
    return Settings(
        discord_token="x",
        plex_url="http://localhost",
        plex_token="x",
        cache_dir=tmp_path / "cache",
        libraries=libraries,
        library_sync={"enabled": enabled},
        **kwargs,
    )


def _ndjson_lines(resp) -> list[dict]:
    return [json.loads(line) for line in resp.text.strip().split("\n") if line]


def test_search_quote_extend_stays_cached_only_when_sync_disabled(tmp_path, monkeypatch):
    settings = _settings_with_sync(tmp_path, enabled=False)
    _write_title(
        settings.quote_index_db_path,
        "guid-1", 101, "Monty Python", "Movies",
        ["Nobody expects the Spanish Inquisition!"],
    )
    fake_plex = _FakePlexClient(settings, movie_items=[_movie_item("guid-2", 102, "Uncached Film")])
    client = _client(settings, monkeypatch, fake_plex)

    resp = client.get("/search-quote-extend", params={"quote": "nobody expects"})

    assert resp.status_code == 200
    events = _ndjson_lines(resp)
    assert [e["type"] for e in events] == ["cached", "final"]
    assert events[0]["matches"][0]["title"] == "Monty Python"
    assert events[1]["remaining_uncached"] is None
    assert events[1]["matches"] == events[0]["matches"]


def test_search_quote_extend_short_circuits_when_nothing_uncached(tmp_path, monkeypatch):
    settings = _settings_with_sync(tmp_path, enabled=True)
    _write_title(
        settings.quote_index_db_path,
        "guid-1", 101, "Monty Python", "Movies",
        ["Nobody expects the Spanish Inquisition!"],
    )
    fake_plex = _FakePlexClient(settings, movie_items=[_movie_item("guid-1", 101, "Monty Python")])
    client = _client(settings, monkeypatch, fake_plex)

    resp = client.get("/search-quote-extend", params={"quote": "nobody expects"})

    events = _ndjson_lines(resp)
    assert [e["type"] for e in events] == ["cached", "scanning", "final"]
    assert events[2]["remaining_uncached"] == 0


def test_search_quote_extend_skips_titles_already_marked_no_subtitle(tmp_path, monkeypatch):
    settings = _settings_with_sync(tmp_path, enabled=True)
    upsert_no_subtitle_title(settings.quote_index_db_path, "guid-2", 102, "Silent Film", "Movies")
    fake_plex = _FakePlexClient(settings, movie_items=[_movie_item("guid-2", 102, "Silent Film")])
    client = _client(settings, monkeypatch, fake_plex)

    resp = client.get("/search-quote-extend", params={"quote": "anything"})

    events = _ndjson_lines(resp)
    assert [e["type"] for e in events] == ["cached", "scanning", "final"]
    assert events[2]["remaining_uncached"] == 0


def test_search_quote_extend_extracts_uncached_titles_up_to_cap(tmp_path, monkeypatch):
    mount_root = tmp_path / "media"
    mount_root.mkdir()
    (mount_root / "film.mkv").write_bytes(b"x")
    settings = _settings_with_sync(tmp_path, enabled=True, cap=1, mount_root=mount_root)

    found_entries = [
        SubtitleEntry(index=1, start=0.0, end=2.0, text="Nobody expects the Spanish Inquisition!")
    ]

    async def _fake_get_subtitles(movie, container_video_path, cache_dir, db_path, ffprobe_timeout=180.0, ffmpeg_timeout=180.0):
        search_index.upsert_title(
            db_path, movie.guid, movie.rating_key, movie.title, movie.library_name,
            "sidecar", None, None, found_entries, None,
        )
        return SubtitleResult(guid=movie.guid, source=SubtitleSource.SIDECAR, entries=found_entries)

    monkeypatch.setattr(library_sync_module, "get_subtitles", _fake_get_subtitles)

    fake_plex = _FakePlexClient(
        settings,
        movie_items=[
            _movie_item("guid-1", 101, "Monty Python", plex_path="D:\\Movies\\film.mkv"),
            _movie_item("guid-2", 102, "Uncached Film Two", plex_path="D:\\Movies\\missing.mkv"),
        ],
    )
    client = _client(settings, monkeypatch, fake_plex)

    resp = client.get("/search-quote-extend", params={"quote": "nobody expects"})

    events = _ndjson_lines(resp)
    types = [e["type"] for e in events]
    assert types == ["cached", "scanning", "progress", "final"]
    assert events[0]["matches"] == []
    assert events[2]["title"] == "Monty Python"
    assert events[2]["index"] == 1
    assert events[2]["total"] == 1
    final = events[3]
    assert final["remaining_uncached"] == 1
    assert final["matches"][0]["title"] == "Monty Python"
    assert search_index.has_title(settings.quote_index_db_path, "guid-1")


def test_search_quote_extend_does_not_permanently_mark_a_failed_extraction(tmp_path, monkeypatch):
    mount_root = tmp_path / "media"
    mount_root.mkdir()
    (mount_root / "film.mkv").write_bytes(b"x")
    settings = _settings_with_sync(tmp_path, enabled=True, mount_root=mount_root)

    async def _boom(movie, container_video_path, cache_dir, db_path, ffprobe_timeout=180.0, ffmpeg_timeout=180.0):
        raise RuntimeError("ffmpeg exploded")

    monkeypatch.setattr(library_sync_module, "get_subtitles", _boom)

    fake_plex = _FakePlexClient(
        settings, movie_items=[_movie_item("guid-1", 101, "Broken Film", plex_path="D:\\Movies\\film.mkv")]
    )
    client = _client(settings, monkeypatch, fake_plex)

    resp = client.get("/search-quote-extend", params={"quote": "anything"})

    events = _ndjson_lines(resp)
    assert [e["type"] for e in events] == ["cached", "scanning", "progress", "final"]
    # A transient extraction failure must not become a permanent negative
    # cache entry — it needs to be retried on a future extend call.
    assert not search_index.has_title(settings.quote_index_db_path, "guid-1")
    from app.worker.quote_index import is_no_subtitle_title
    assert not is_no_subtitle_title(settings.quote_index_db_path, "guid-1")


def test_search_quote_extend_treats_plex_enumeration_failure_as_empty_for_that_library(tmp_path, monkeypatch):
    settings = _settings_with_sync(tmp_path, enabled=True)
    _write_title(
        settings.quote_index_db_path,
        "guid-1", 101, "Monty Python", "Movies",
        ["Nobody expects the Spanish Inquisition!"],
    )

    class _RaisingPlex(_FakePlexClient):
        def enumerate_section(self, section):
            raise RuntimeError("Plex unreachable")

    fake_plex = _RaisingPlex(settings)
    client = _client(settings, monkeypatch, fake_plex)

    resp = client.get("/search-quote-extend", params={"quote": "nobody expects"})

    events = _ndjson_lines(resp)
    assert [e["type"] for e in events] == ["cached", "scanning", "final"]
    assert events[2]["remaining_uncached"] == 0


def test_search_quote_extend_skip_does_not_consume_cap_budget(tmp_path, monkeypatch):
    """A SKIP outcome (no path mapping, file missing) is just a cheap path
    check, not an extraction — it must not eat into `cap`, or a library
    with many perpetually-SKIP titles (e.g. no path mapping configured for
    one section, CLAUDE.md Section 3's documented fallback shape) would
    make "Search N more" loop forever on the same head of the list without
    ever reaching a title that could actually be processed.
    """
    mount_root = tmp_path / "media"
    mount_root.mkdir()
    (mount_root / "film.mkv").write_bytes(b"x")
    settings = _settings_with_sync(tmp_path, enabled=True, cap=1, mount_root=mount_root)

    found_entries = [
        SubtitleEntry(index=1, start=0.0, end=2.0, text="Nobody expects the Spanish Inquisition!")
    ]

    async def _fake_get_subtitles(movie, container_video_path, cache_dir, db_path, ffprobe_timeout=180.0, ffmpeg_timeout=180.0):
        search_index.upsert_title(
            db_path, movie.guid, movie.rating_key, movie.title, movie.library_name,
            "sidecar", None, None, found_entries, None,
        )
        return SubtitleResult(guid=movie.guid, source=SubtitleSource.SIDECAR, entries=found_entries)

    monkeypatch.setattr(library_sync_module, "get_subtitles", _fake_get_subtitles)

    # First item in enumeration order has no path mapping covering its
    # plex_path (mount_root's mapping only covers "D:\Movies") — a SKIP.
    # Second item does have a matching mapping and an on-disk file — a real,
    # extractable title. With cap=1, the SKIP must not use up the one slot:
    # the extractable title should still be processed in this same call.
    fake_plex = _FakePlexClient(
        settings,
        movie_items=[
            _movie_item("guid-skip", 100, "Unmapped Film", plex_path="E:\\Other\\weird.mkv"),
            _movie_item("guid-1", 101, "Monty Python", plex_path="D:\\Movies\\film.mkv"),
        ],
    )
    client = _client(settings, monkeypatch, fake_plex)

    resp = client.get("/search-quote-extend", params={"quote": "nobody expects"})

    events = _ndjson_lines(resp)
    types = [e["type"] for e in events]
    assert types == ["cached", "scanning", "progress", "final"]
    # Only one progress event — for the productive title, not the SKIP'd one.
    assert events[2]["title"] == "Monty Python"
    assert events[2]["index"] == 1
    assert events[2]["total"] == 1
    final = events[3]
    # Both items were scanned this call (one SKIP'd, one processed), so
    # nothing was left un-looked-at.
    assert final["remaining_uncached"] == 0
    assert final["matches"][0]["title"] == "Monty Python"
    assert search_index.has_title(settings.quote_index_db_path, "guid-1")
    assert not search_index.has_title(settings.quote_index_db_path, "guid-skip")


def test_search_quote_extend_rejects_non_positive_cap(tmp_path, monkeypatch):
    settings = _settings_with_sync(tmp_path, enabled=True)
    client = _client(settings, monkeypatch)

    resp = client.get("/search-quote-extend", params={"quote": "anything", "cap": 0})

    assert resp.status_code == 422


def test_search_quote_extend_skips_enumeration_when_library_unchanged(tmp_path, monkeypatch):
    """Mirrors library_sync's own section.updatedAt change-detection: if a
    movie library's live updatedAt still matches what library_sync's last
    full pass stored, nothing in it could be uncached beyond what's already
    known, so the (often several-second, real-Plex-network) enumeration is
    skipped entirely — verified here by making enumerate_section raise if
    it's ever called.
    """
    from app.worker.quote_index import set_section_updated_at

    settings = _settings_with_sync(tmp_path, enabled=True)
    _write_title(
        settings.quote_index_db_path,
        "guid-1", 101, "Monty Python", "Movies",
        ["Nobody expects the Spanish Inquisition!"],
    )
    set_section_updated_at(settings.quote_index_db_path, "Movies", 12345)

    class _UnchangedPlex(_FakePlexClient):
        def current_section_updated_ats(self):
            return {"Movies": 12345}

        def enumerate_section(self, section):
            raise AssertionError("enumerate_section must not be called when updatedAt is unchanged")

    fake_plex = _UnchangedPlex(settings)
    client = _client(settings, monkeypatch, fake_plex)

    resp = client.get("/search-quote-extend", params={"quote": "nobody expects"})

    events = _ndjson_lines(resp)
    assert [e["type"] for e in events] == ["cached", "scanning", "final"]
    assert events[2]["remaining_uncached"] == 0


def test_search_quote_extend_still_enumerates_when_library_changed(tmp_path, monkeypatch):
    """The companion case to the skip test above: a live updatedAt that
    differs from the stored value must still trigger a full enumeration —
    confirms the skip is conditional, not accidentally unconditional. Uses
    an item with no path mapping (a cheap SKIP, not a real extraction) so
    this test only exercises the enumeration-was-called path, not the
    extraction machinery other tests already cover.
    """
    from app.worker.quote_index import set_section_updated_at

    mount_root = tmp_path / "media"
    mount_root.mkdir()
    settings = _settings_with_sync(tmp_path, enabled=True, mount_root=mount_root)
    _write_title(
        settings.quote_index_db_path,
        "guid-1", 101, "Monty Python", "Movies",
        ["Nobody expects the Spanish Inquisition!"],
    )
    set_section_updated_at(settings.quote_index_db_path, "Movies", 111)

    class _ChangedPlex(_FakePlexClient):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.enumerate_called = False

        def current_section_updated_ats(self):
            return {"Movies": 222}  # differs from the stored 111

        def enumerate_section(self, section):
            self.enumerate_called = True
            return super().enumerate_section(section)

    # File doesn't exist under mount_root -> a cheap "SKIP (file not found)",
    # not a real extraction — this test only cares that enumeration ran.
    fake_plex = _ChangedPlex(
        settings,
        movie_items=[_movie_item("guid-2", 102, "Another Film", plex_path="D:\\Movies\\missing.mkv")],
    )
    client = _client(settings, monkeypatch, fake_plex)

    resp = client.get("/search-quote-extend", params={"quote": "nobody expects"})

    events = _ndjson_lines(resp)
    assert fake_plex.enumerate_called
    assert [e["type"] for e in events] == ["cached", "scanning", "final"]
    assert events[2]["remaining_uncached"] == 0


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
    assert body["rating_key"] == 101
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
    assert resp.json()["rating_key"] == 202


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
    assert resp.json()["rating_key"] == 101


def test_random_quote_endpoint_returns_404_when_nothing_cached(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    client = _client(settings, monkeypatch)

    resp = client.get("/random-quote", params={"media": "all"})

    assert resp.status_code == 404


def test_search_quote_endpoint_returns_more_than_eight_when_available(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    for i in range(12):
        _write_title(
            settings.quote_index_db_path,
            f"guid-{i}", 100 + i, f"Title {i}", "Movies",
            ["Nobody expects the Spanish Inquisition!"],
        )

    client = _client(settings, monkeypatch)
    resp = client.get("/search-quote", params={"quote": "nobody expects the spanish inquisition"})

    assert resp.status_code == 200
    body = resp.json()
    # Old behavior (candidate_limit=8) would have truncated this to 8 —
    # asserting >8 proves qm.fetch_limit (default 50), not the old cap,
    # is what reaches search_cached_library's result_limit.
    assert len(body["matches"]) == 12


def test_search_quote_endpoint_reports_truncated_when_more_than_fetch_limit(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    settings.quote_match = QuoteMatchDefaults(fetch_limit=5)
    for i in range(8):
        _write_title(
            settings.quote_index_db_path,
            f"guid-{i}", 100 + i, f"Title {i}", "Movies",
            ["Nobody expects the Spanish Inquisition!"],
        )

    client = _client(settings, monkeypatch)
    resp = client.get("/search-quote", params={"quote": "nobody expects the spanish inquisition"})

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["matches"]) == 5
    assert body["truncated"] is True


def test_search_quote_endpoint_reports_not_truncated_when_within_fetch_limit(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    settings.quote_match = QuoteMatchDefaults(fetch_limit=5)
    for i in range(3):
        _write_title(
            settings.quote_index_db_path,
            f"guid-{i}", 100 + i, f"Title {i}", "Movies",
            ["Nobody expects the Spanish Inquisition!"],
        )

    client = _client(settings, monkeypatch)
    resp = client.get("/search-quote", params={"quote": "nobody expects the spanish inquisition"})

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["matches"]) == 3
    assert body["truncated"] is False


def test_search_episodes_quote_endpoint_reports_truncated_when_more_than_fetch_limit(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    settings.quote_match = QuoteMatchDefaults(fetch_limit=2)
    episodes = []
    for i in range(4):
        episode = MovieResult(
            rating_key=500 + i, title=f"The Office — S01E0{i} — Ep{i}", year=None,
            duration_ms=1000, thumb_url=None, plex_path=f"D:\\TV\\office{i}.mkv",
            guid=f"ep-guid-{i}", library_name="TV Shows",
        )
        episodes.append(episode)
        _write_title(
            settings.quote_index_db_path,
            f"ep-guid-{i}", 500 + i, f"The Office — S01E0{i} — Ep{i}", "TV Shows",
            ["That's what she said."],
        )

    fake_plex = _FakePlexClient(settings)
    fake_plex._episodes_by_show[900] = episodes
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
    fake_plex._movies_by_rating_key[101] = _movie_item("guid-1", 101, "Monty Python")
    client = _client(settings, monkeypatch, fake_plex)

    resp = client.get("/random-line/101")

    assert resp.status_code == 200
    body = resp.json()
    assert body["rating_key"] == 101
    # "Okay." is under the default random_min_words (3) and must never be
    # picked — the only eligible line is the long one.
    assert body["text"] == "Nobody expects the Spanish Inquisition!"
    assert body["pool_size"] == 1
    assert isinstance(body["entry_id"], int)


def test_random_line_endpoint_404_when_rating_key_unknown(tmp_path, monkeypatch):
    settings = _settings_with_sync(tmp_path, enabled=False)
    fake_plex = _FakePlexClient(settings)
    client = _client(settings, monkeypatch, fake_plex)

    resp = client.get("/random-line/999")

    assert resp.status_code == 404


def test_random_line_show_endpoint_whole_show_picks_from_any_episode(tmp_path, monkeypatch):
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

    resp = client.get("/random-line-show/900")

    assert resp.status_code == 200
    body = resp.json()
    assert body["rating_key"] == 501
    assert body["text"] == "That's what she said."


def test_random_line_show_endpoint_single_episode_scope(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    ep1 = MovieResult(
        rating_key=501, title="The Office — S01E01 — Pilot", year=None,
        duration_ms=1000, thumb_url=None, plex_path="D:\\TV\\office1.mkv",
        guid="ep-guid-1", library_name="TV Shows",
    )
    _write_title(
        settings.quote_index_db_path, "ep-guid-1", 501, ep1.title, "TV Shows", ["Line from episode one."],
    )
    fake_plex = _FakePlexClient(settings)
    fake_plex._episodes_by_show[900] = [ep1]
    client = _client(settings, monkeypatch, fake_plex)

    resp = client.get("/random-line-show/900", params={"season": 1, "episode": 1})

    assert resp.status_code == 200
    assert resp.json()["rating_key"] == 501


def test_random_line_show_endpoint_requires_both_season_and_episode(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    client = _client(settings, monkeypatch)

    resp = client.get("/random-line-show/900", params={"season": 1})

    assert resp.status_code == 422


def test_random_line_show_endpoint_404_when_show_unknown(tmp_path, monkeypatch):
    settings = _settings(tmp_path)

    class _NoShowPlex(_FakePlexClient):
        def list_episodes(self, show_rating_key: int) -> list[MovieResult]:
            from app.worker.plex_client import ShowNotFoundError

            raise ShowNotFoundError(f"No show {show_rating_key}")

    client = _client(settings, monkeypatch, _NoShowPlex(settings))

    resp = client.get("/random-line-show/900")

    assert resp.status_code == 404
