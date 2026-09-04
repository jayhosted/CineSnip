import sqlite3
import threading
import time

from app.worker import search_index as search_index_module
from app.worker.quote_index import CachedTitle
from app.worker.search_index import (
    _connect,
    count_entries,
    coverage_counts,
    fetch_entry_windows,
    get_entries,
    get_fingerprint,
    get_title_ids_by_guid,
    has_title,
    iter_all_entries,
    list_entry_rows_for_titles,
    list_titles,
    list_titles_for_library,
    pick_random_entry_id,
    remove_title,
    search_entry_ids,
    upsert_title,
)
from app.worker.subtitles import SubtitleEntry


def _entries(*texts):
    return [
        SubtitleEntry(index=i, start=float(i), end=float(i) + 1.0, text=text)
        for i, text in enumerate(texts)
    ]


def _search_title_ids(db_path, tokens, **kwargs):
    """Test helper: distinct title_ids among search_entry_ids' surviving
    entry rows — most of this file's existing assertions only care about
    which TITLES matched, not the underlying entry-level rows."""
    return sorted({title_id for _, title_id, _idx in search_entry_ids(db_path, tokens, **kwargs)})


def test_schema_creation_is_idempotent(tmp_path):
    db_path = tmp_path / "quote_index.db"
    with _connect(db_path):
        pass
    with _connect(db_path):
        pass  # second call must not raise


def test_schema_ddl_only_runs_once_per_db_path(tmp_path, monkeypatch):
    # Perf regression coverage: schema creation/migration must not re-run
    # on every connection (real measurement: several seconds of pure
    # redundant DDL overhead across a 279-connection whole-show TV search).
    monkeypatch.setattr(search_index_module, "_initialized_dbs", set())
    db_path = tmp_path / "quote_index.db"

    assert db_path not in search_index_module._initialized_dbs
    with _connect(db_path):
        pass
    # The schema-creation branch must have actually run and recorded this
    # db_path as initialized...
    assert db_path in search_index_module._initialized_dbs

    # ...and a real table created by that first connect must still be
    # queryable — the guard must only skip *re-running* the DDL, not the
    # actual schema it creates.
    with _connect(db_path) as conn:
        conn.execute("SELECT * FROM titles")
        conn.execute("SELECT * FROM entries")
        conn.execute("SELECT * FROM entries_fts")


def test_connect_migrates_pre_issue_24_rating_key_column(tmp_path):
    """issue #24 renamed titles.rating_key -> titles.media_id, but
    CREATE TABLE IF NOT EXISTS is a no-op against a titles table that
    already existed under the old name. An install upgrading in place
    must have its column renamed on first connect, not fail every
    media_id-based query with "no such column: media_id"."""
    db_path = tmp_path / "quote_index.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE titles(title_id INTEGER PRIMARY KEY, guid TEXT UNIQUE NOT NULL, "
        "rating_key INTEGER NOT NULL, title TEXT NOT NULL, library_name TEXT NOT NULL, "
        "source TEXT, sidecar_path TEXT, stream_index INTEGER, cached_at TEXT NOT NULL, "
        "fingerprint_mtime REAL, fingerprint_size INTEGER)"
    )
    conn.execute(
        "INSERT INTO titles (guid, rating_key, title, library_name, cached_at) VALUES "
        "('guid-1', 101, 'Film One', 'Movies', '2026-01-01T00:00:00Z')"
    )
    conn.commit()
    conn.close()

    titles = list_titles(db_path)

    assert len(titles) == 1
    assert titles[0].guid == "guid-1"
    assert titles[0].media_id == "101"


def test_upsert_title_round_trip(tmp_path):
    db_path = tmp_path / "quote_index.db"
    entries = _entries("Hello, world!", "Goodbye, world.")
    upsert_title(
        db_path,
        guid="guid-1",
        media_id="101",
        title="Film One",
        library_name="Movies",
        source="sidecar",
        sidecar_path="/media/film-one.srt",
        stream_index=None,
        entries=entries,
        fingerprint=(123.456, 789),
    )

    result = get_entries(db_path, "guid-1")

    assert result == entries
    # normalized_text must not leak onto the returned SubtitleEntry objects
    for entry in result:
        assert not hasattr(entry, "normalized_text")


def test_get_entries_returns_none_for_unknown_guid(tmp_path):
    db_path = tmp_path / "quote_index.db"
    assert get_entries(db_path, "does-not-exist") is None


def test_get_entries_returns_empty_list_for_cached_title_with_no_entries(tmp_path):
    db_path = tmp_path / "quote_index.db"
    upsert_title(
        db_path,
        guid="guid-empty",
        media_id="1",
        title="Empty",
        library_name="Movies",
        source="sidecar",
        sidecar_path=None,
        stream_index=None,
        entries=[],
        fingerprint=None,
    )
    assert get_entries(db_path, "guid-empty") == []


def test_upsert_title_twice_replaces_entries_not_duplicates(tmp_path):
    db_path = tmp_path / "quote_index.db"
    upsert_title(
        db_path,
        guid="guid-1",
        media_id="101",
        title="Film One",
        library_name="Movies",
        source="sidecar",
        sidecar_path=None,
        stream_index=None,
        entries=_entries("first version line one", "first version line two"),
        fingerprint=None,
    )
    new_entries = _entries("second version only line")
    upsert_title(
        db_path,
        guid="guid-1",
        media_id="101",
        title="Film One",
        library_name="Movies",
        source="sidecar",
        sidecar_path=None,
        stream_index=None,
        entries=new_entries,
        fingerprint=None,
    )

    result = get_entries(db_path, "guid-1")
    assert result == new_entries

    # the old word must no longer be searchable at all
    assert _search_title_ids(db_path, ["first"]) == []


def test_get_fingerprint_round_trip(tmp_path):
    db_path = tmp_path / "quote_index.db"
    upsert_title(
        db_path,
        guid="guid-1",
        media_id="101",
        title="Film One",
        library_name="Movies",
        source="sidecar",
        sidecar_path=None,
        stream_index=None,
        entries=[],
        fingerprint=(111.5, 2048),
    )
    assert get_fingerprint(db_path, "guid-1") == (111.5, 2048)


def test_get_fingerprint_none_when_not_cached(tmp_path):
    db_path = tmp_path / "quote_index.db"
    assert get_fingerprint(db_path, "nope") is None


def test_get_fingerprint_none_when_not_set(tmp_path):
    db_path = tmp_path / "quote_index.db"
    upsert_title(
        db_path,
        guid="guid-1",
        media_id="101",
        title="Film One",
        library_name="Movies",
        source="sidecar",
        sidecar_path=None,
        stream_index=None,
        entries=[],
        fingerprint=None,
    )
    assert get_fingerprint(db_path, "guid-1") is None


def _populate_small_corpus(db_path):
    upsert_title(
        db_path,
        guid="guid-1",
        media_id="101",
        title="Film One",
        library_name="Movies",
        source="sidecar",
        sidecar_path=None,
        stream_index=None,
        entries=_entries("the quick brown fox", "jumps over the lazy dog"),
        fingerprint=None,
    )
    upsert_title(
        db_path,
        guid="guid-2",
        media_id="102",
        title="Film Two",
        library_name="3D",
        source="embedded",
        sidecar_path=None,
        stream_index=2,
        entries=_entries("a completely different sentence", "with unique zephyr word"),
        fingerprint=None,
    )


def test_list_titles_and_for_library_and_has_and_remove(tmp_path):
    db_path = tmp_path / "quote_index.db"
    _populate_small_corpus(db_path)

    titles = list_titles(db_path)
    assert set(titles) == {
        CachedTitle(guid="guid-1", media_id="101", title="Film One", library_name="Movies", source="sidecar"),
        CachedTitle(guid="guid-2", media_id="102", title="Film Two", library_name="3D", source="embedded"),
    }

    movies_only = list_titles_for_library(db_path, "Movies")
    assert [t.guid for t in movies_only] == ["guid-1"]

    assert has_title(db_path, "guid-1") is True
    assert has_title(db_path, "unknown-guid") is False

    # "zephyr" is unique to guid-2 — confirm it's searchable before removal
    ids_before = _search_title_ids(db_path, ["zephyr"])
    assert len(ids_before) == 1

    remove_title(db_path, "guid-2")

    assert has_title(db_path, "guid-2") is False
    assert list_titles_for_library(db_path, "3D") == []
    assert get_entries(db_path, "guid-2") is None
    # no orphaned entries/entries_fts rows left behind
    assert _search_title_ids(db_path, ["zephyr"]) == []


def test_remove_title_on_unknown_guid_is_a_noop(tmp_path):
    db_path = tmp_path / "quote_index.db"
    _populate_small_corpus(db_path)
    remove_title(db_path, "does-not-exist")  # must not raise
    assert len(list_titles(db_path)) == 2


def test_list_titles_on_missing_db_returns_empty(tmp_path):
    assert list_titles(tmp_path / "does-not-exist.db") == []
    assert list_titles_for_library(tmp_path / "does-not-exist.db", "Movies") == []


def test_search_title_ids_exact_match_and_no_match(tmp_path):
    db_path = tmp_path / "quote_index.db"
    _populate_small_corpus(db_path)

    title_id_map = {t.guid: t for t in list_titles(db_path)}
    assert set(title_id_map) == {"guid-1", "guid-2"}

    fox_ids = _search_title_ids(db_path, ["fox"])
    assert len(fox_ids) == 1

    zephyr_ids = _search_title_ids(db_path, ["zephyr"])
    assert len(zephyr_ids) == 1
    assert zephyr_ids != fox_ids

    assert _search_title_ids(db_path, ["nonexistentword"]) == []
    assert _search_title_ids(db_path, []) == []


def test_search_title_ids_handles_single_character_and_digit_tokens(tmp_path):
    db_path = tmp_path / "quote_index.db"
    upsert_title(
        db_path,
        guid="guid-1",
        media_id="101",
        title="Film One",
        library_name="Movies",
        source="sidecar",
        sidecar_path=None,
        stream_index=None,
        entries=_entries("agent 007 says a single word"),
        fingerprint=None,
    )
    # single-char token and lone-digit token must not raise FTS5 syntax
    # errors, and must actually match (normalize_for_match's alnum-only
    # output needs no extra quoting for either case)
    assert _search_title_ids(db_path, ["a"]) != []
    assert _search_title_ids(db_path, ["007"]) != []


def test_iter_all_entries(tmp_path):
    db_path = tmp_path / "quote_index.db"
    _populate_small_corpus(db_path)

    result = dict(iter_all_entries(db_path))

    assert set(result.keys()) == {"guid-1", "guid-2"}
    assert [e.text for e in result["guid-1"]] == ["the quick brown fox", "jumps over the lazy dog"]
    assert [e.text for e in result["guid-2"]] == ["a completely different sentence", "with unique zephyr word"]


def test_iter_all_entries_on_missing_db_yields_nothing(tmp_path):
    assert list(iter_all_entries(tmp_path / "does-not-exist.db")) == []


def test_iter_all_entries_scoped_to_title_ids_excludes_others(tmp_path):
    db_path = tmp_path / "quote_index.db"
    _populate_small_corpus(db_path)
    guid_to_id = {t.guid: t.media_id for t in list_titles(db_path)}
    with _connect(db_path) as conn:
        rows = conn.execute("SELECT guid, title_id FROM titles").fetchall()
    guid_to_title_id = dict(rows)

    result = dict(iter_all_entries(db_path, title_ids=[guid_to_title_id["guid-1"]]))

    assert set(result.keys()) == {"guid-1"}


def test_iter_all_entries_empty_scope_yields_nothing(tmp_path):
    db_path = tmp_path / "quote_index.db"
    _populate_small_corpus(db_path)
    assert list(iter_all_entries(db_path, title_ids=[])) == []


def test_get_title_ids_by_guid(tmp_path):
    db_path = tmp_path / "quote_index.db"
    _populate_small_corpus(db_path)
    with _connect(db_path) as conn:
        rows = conn.execute("SELECT guid, title_id FROM titles").fetchall()
    expected = dict(rows)

    result = get_title_ids_by_guid(db_path, ["guid-1", "guid-2", "unknown-guid"])

    assert result == expected


def test_get_title_ids_by_guid_empty_input(tmp_path):
    db_path = tmp_path / "quote_index.db"
    _populate_small_corpus(db_path)
    assert get_title_ids_by_guid(db_path, []) == {}


def test_get_title_ids_by_guid_missing_db(tmp_path):
    assert get_title_ids_by_guid(tmp_path / "does-not-exist.db", ["guid-1"]) == {}


def test_fetch_entry_windows(tmp_path):
    db_path = tmp_path / "quote_index.db"
    _populate_small_corpus(db_path)
    with _connect(db_path) as conn:
        rows = conn.execute("SELECT guid, title_id FROM titles").fetchall()
    guid_to_title_id = dict(rows)
    id1 = guid_to_title_id["guid-1"]

    # guid-1's entries have idx 0 and 1 (see _entries()) — a window of
    # (0, 0) must fetch only the first, not the whole title.
    result = fetch_entry_windows(db_path, [(id1, 0, 0)])

    assert set(result.keys()) == {id1}
    ordered = result[id1]
    assert [entry.text for _, entry in ordered] == ["the quick brown fox"]
    entry_id, entry = ordered[0]
    assert isinstance(entry_id, int)


def test_fetch_entry_windows_covers_full_range_when_window_is_wide(tmp_path):
    db_path = tmp_path / "quote_index.db"
    _populate_small_corpus(db_path)
    with _connect(db_path) as conn:
        rows = conn.execute("SELECT guid, title_id FROM titles").fetchall()
    guid_to_title_id = dict(rows)
    id1 = guid_to_title_id["guid-1"]

    result = fetch_entry_windows(db_path, [(id1, 0, 999)])

    assert [entry.text for _, entry in result[id1]] == [
        "the quick brown fox",
        "jumps over the lazy dog",
    ]


def test_fetch_entry_windows_excludes_entries_outside_every_window(tmp_path):
    db_path = tmp_path / "quote_index.db"
    texts = ["filler"] * 50
    texts[10] = "first hit line"
    texts[40] = "second hit line"
    upsert_title(
        db_path, guid="guid-1", media_id="1", title="Film", library_name="Movies",
        source="sidecar", sidecar_path=None, stream_index=None,
        entries=_entries(*texts), fingerprint=None,
    )
    with _connect(db_path) as conn:
        title_id = conn.execute("SELECT title_id FROM titles WHERE guid = 'guid-1'").fetchone()[0]

    result = fetch_entry_windows(db_path, [(title_id, 8, 12), (title_id, 38, 42)])

    ordered = result[title_id]
    assert len(ordered) == 10  # 5 entries per window * 2 windows
    fetched_idxs = sorted(entry.index for _, entry in ordered)
    assert fetched_idxs == list(range(8, 13)) + list(range(38, 43))


def test_fetch_entry_windows_empty_input(tmp_path):
    db_path = tmp_path / "quote_index.db"
    _populate_small_corpus(db_path)
    assert fetch_entry_windows(db_path, []) == {}


def test_fetch_entry_windows_batches_many_windows_beyond_chunk_size(tmp_path):
    # Regression coverage for fetch_entry_windows' internal chunking
    # (windows are batched to stay under SQLite's bound-parameter limit) —
    # more windows than one chunk must still return every window's rows.
    db_path = tmp_path / "quote_index.db"
    title_ids = []
    for i in range(250):
        upsert_title(
            db_path, guid=f"guid-{i}", media_id=str(i), title=f"Film {i}", library_name="Movies",
            source="sidecar", sidecar_path=None, stream_index=None,
            entries=_entries("only line"), fingerprint=None,
        )
    with _connect(db_path) as conn:
        rows = conn.execute("SELECT title_id FROM titles").fetchall()
    title_ids = [r[0] for r in rows]

    windows = [(tid, 0, 0) for tid in title_ids]
    result = fetch_entry_windows(db_path, windows)

    assert set(result.keys()) == set(title_ids)
    assert all(len(v) == 1 for v in result.values())


def test_search_entry_ids_is_entry_level_not_title_level(tmp_path):
    # A single title with many entries where only ONE entry contains the
    # search term must yield exactly one (entry_id, title_id, idx) row, not
    # a blanket "every entry of this title" result — this is the crux of
    # the entry-vs-title LIMIT bug: capping at the title level meant every
    # entry of a survivor title got fuzzy-scored downstream, not just the
    # entries that actually matched.
    db_path = tmp_path / "quote_index.db"
    texts = ["irrelevant filler line"] * 50
    texts[25] = "the treasure is buried under the old oak tree"
    upsert_title(
        db_path, guid="guid-1", media_id="1", title="Film", library_name="Movies",
        source="sidecar", sidecar_path=None, stream_index=None,
        entries=_entries(*texts), fingerprint=None,
    )

    hits = search_entry_ids(db_path, ["treasure"])

    assert len(hits) == 1
    entry_id, title_id, idx = hits[0]
    assert idx == 25
    with _connect(db_path) as conn:
        db_idx = conn.execute("SELECT idx FROM entries WHERE id = ?", (entry_id,)).fetchone()[0]
    assert db_idx == 25


def test_search_entry_ids_respects_limit_across_titles(tmp_path):
    db_path = tmp_path / "quote_index.db"
    for i in range(10):
        upsert_title(
            db_path, guid=f"guid-{i}", media_id=str(i), title=f"Film {i}", library_name="Movies",
            source="sidecar", sidecar_path=None, stream_index=None,
            entries=_entries("a shared unique keyword here"), fingerprint=None,
        )

    hits = search_entry_ids(db_path, ["keyword"], limit=3)

    assert len(hits) == 3


def test_search_entry_ids_scoped_to_title_ids(tmp_path):
    db_path = tmp_path / "quote_index.db"
    _populate_small_corpus(db_path)
    with _connect(db_path) as conn:
        rows = conn.execute("SELECT guid, title_id FROM titles").fetchall()
    guid_to_title_id = dict(rows)

    # "the" appears in both titles' entries; scoping to guid-1's title_id
    # only must exclude guid-2's matching entries even though they'd
    # otherwise satisfy the FTS5 match.
    scoped_hits = search_entry_ids(db_path, ["the"], title_ids=[guid_to_title_id["guid-1"]])
    assert scoped_hits
    assert all(title_id == guid_to_title_id["guid-1"] for _, title_id, _idx in scoped_hits)


def test_search_entry_ids_empty_scope_yields_nothing(tmp_path):
    db_path = tmp_path / "quote_index.db"
    _populate_small_corpus(db_path)
    assert search_entry_ids(db_path, ["the"], title_ids=[]) == []


def test_coverage_counts_by_source_excludes_none_and_other_libraries(tmp_path):
    db_path = tmp_path / "quote_index.db"
    upsert_title(
        db_path, guid="g1", media_id="1", title="Sidecar Film", library_name="Movies",
        source="sidecar", sidecar_path=None, stream_index=None, entries=[], fingerprint=None,
    )
    upsert_title(
        db_path, guid="g2", media_id="2", title="Sidecar Film Two", library_name="Movies",
        source="sidecar", sidecar_path=None, stream_index=None, entries=[], fingerprint=None,
    )
    upsert_title(
        db_path, guid="g3", media_id="3", title="Embedded Film", library_name="Movies",
        source="embedded", sidecar_path=None, stream_index=2, entries=[], fingerprint=None,
    )
    upsert_title(
        db_path, guid="g4", media_id="4", title="No Subtitle Film", library_name="Movies",
        source="none", sidecar_path=None, stream_index=None, entries=[], fingerprint=None,
    )
    # a different library must not bleed into Movies' counts
    upsert_title(
        db_path, guid="g5", media_id="5", title="3D Film", library_name="3D",
        source="sidecar", sidecar_path=None, stream_index=None, entries=[], fingerprint=None,
    )

    assert coverage_counts(db_path, "Movies") == {"sidecar": 2, "embedded": 1}
    assert coverage_counts(db_path, "3D") == {"sidecar": 1, "embedded": 0}


def test_coverage_counts_on_missing_db_returns_zeros(tmp_path):
    assert coverage_counts(tmp_path / "does-not-exist.db", "Movies") == {"sidecar": 0, "embedded": 0}


def test_coverage_counts_on_unknown_library_returns_zeros(tmp_path):
    db_path = tmp_path / "quote_index.db"
    upsert_title(
        db_path, guid="g1", media_id="1", title="Film", library_name="Movies",
        source="sidecar", sidecar_path=None, stream_index=None, entries=[], fingerprint=None,
    )
    assert coverage_counts(db_path, "TV Shows") == {"sidecar": 0, "embedded": 0}


def test_wal_mode_allows_concurrent_read_during_write(tmp_path):
    db_path = tmp_path / "quote_index.db"
    _populate_small_corpus(db_path)

    # Prime WAL mode by ensuring the schema/pragmas have been applied once.
    with _connect(db_path):
        pass

    write_started = threading.Event()
    write_should_commit = threading.Event()
    read_result = {}
    read_error = {}

    def writer():
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE titles SET title = 'In Progress Write' WHERE guid = 'guid-1'"
        )
        write_started.set()
        # Hold the write transaction open until the reader has had a chance
        # to run, to prove WAL lets a reader proceed concurrently.
        write_should_commit.wait(timeout=5)
        conn.commit()
        conn.close()

    def reader():
        write_started.wait(timeout=5)
        try:
            with _connect(db_path) as conn:
                rows = conn.execute("SELECT guid FROM titles").fetchall()
            read_result["rows"] = rows
        except Exception as exc:  # pragma: no cover - failure path
            read_error["error"] = exc
        finally:
            write_should_commit.set()

    writer_thread = threading.Thread(target=writer)
    reader_thread = threading.Thread(target=reader)
    writer_thread.start()
    time.sleep(0.05)
    reader_thread.start()
    writer_thread.join(timeout=5)
    reader_thread.join(timeout=5)

    assert "error" not in read_error, read_error.get("error")
    assert "rows" in read_result
    assert len(read_result["rows"]) == 2


def test_pick_random_entry_id_returns_none_when_db_does_not_exist(tmp_path):
    db_path = tmp_path / "quote_index.db"
    assert pick_random_entry_id(db_path, title_ids=[1]) is None


def test_pick_random_entry_id_returns_none_for_empty_title_ids(tmp_path):
    db_path = tmp_path / "quote_index.db"
    upsert_title(
        db_path,
        guid="guid-1",
        media_id="101",
        title="Film One",
        library_name="Movies",
        source="sidecar",
        sidecar_path=None,
        stream_index=None,
        entries=_entries("first line", "second line"),
        fingerprint=None,
    )
    assert pick_random_entry_id(db_path, title_ids=[]) is None


def test_pick_random_entry_id_only_returns_entries_from_scoped_titles(tmp_path):
    db_path = tmp_path / "quote_index.db"
    upsert_title(
        db_path,
        guid="guid-1",
        media_id="101",
        title="Film One",
        library_name="Movies",
        source="sidecar",
        sidecar_path=None,
        stream_index=None,
        entries=_entries("only line in film one"),
        fingerprint=None,
    )
    upsert_title(
        db_path,
        guid="guid-2",
        media_id="102",
        title="Film Two",
        library_name="Movies",
        source="sidecar",
        sidecar_path=None,
        stream_index=None,
        entries=_entries("only line in film two"),
        fingerprint=None,
    )
    title_ids = get_title_ids_by_guid(db_path, ["guid-1"])
    scoped_title_id = title_ids["guid-1"]

    for _ in range(10):
        result = pick_random_entry_id(db_path, title_ids=[scoped_title_id])
        assert result is not None
        entry_id, title_id, idx = result
        assert title_id == scoped_title_id


def test_pick_random_entry_id_excludes_given_ids(tmp_path):
    db_path = tmp_path / "quote_index.db"
    upsert_title(
        db_path, guid="guid-1", media_id="101", title="Film One", library_name="Movies",
        source="sidecar", sidecar_path=None, stream_index=None,
        entries=_entries("only", "other"), fingerprint=None,
    )
    title_id = get_title_ids_by_guid(db_path, ["guid-1"])["guid-1"]
    first = pick_random_entry_id(db_path, title_ids=[title_id])
    assert first is not None
    first_entry_id = first[0]

    # Excluding the only remaining entry must leave nothing to pick.
    all_entry_ids = {
        row[0]
        for row in list_entry_rows_for_titles(db_path, [title_id])
    }
    other_entry_id = next(iter(all_entry_ids - {first_entry_id}))

    result = pick_random_entry_id(
        db_path, title_ids=[title_id], exclude_entry_ids=frozenset({first_entry_id, other_entry_id})
    )
    assert result is None

    # Excluding just one must always yield the other one.
    for _ in range(5):
        result = pick_random_entry_id(
            db_path, title_ids=[title_id], exclude_entry_ids=frozenset({first_entry_id})
        )
        assert result is not None
        assert result[0] == other_entry_id


def test_count_entries_scoped_to_title_ids(tmp_path):
    db_path = tmp_path / "quote_index.db"
    _populate_small_corpus(db_path)
    title_id_1 = get_title_ids_by_guid(db_path, ["guid-1"])["guid-1"]
    title_id_2 = get_title_ids_by_guid(db_path, ["guid-2"])["guid-2"]

    assert count_entries(db_path, [title_id_1]) == 2
    assert count_entries(db_path, [title_id_1, title_id_2]) == 4


def test_count_entries_empty_scope_or_missing_db(tmp_path):
    db_path = tmp_path / "quote_index.db"
    _populate_small_corpus(db_path)
    assert count_entries(db_path, []) == 0
    assert count_entries(tmp_path / "does-not-exist.db", [1]) == 0


def test_list_entry_rows_for_titles(tmp_path):
    db_path = tmp_path / "quote_index.db"
    _populate_small_corpus(db_path)
    title_id_1 = get_title_ids_by_guid(db_path, ["guid-1"])["guid-1"]

    rows = list_entry_rows_for_titles(db_path, [title_id_1])

    assert len(rows) == 2
    texts = sorted(text for _entry_id, _title_id, _idx, _start, _end, text in rows)
    assert texts == ["jumps over the lazy dog", "the quick brown fox"]
    for entry_id, title_id, idx, start, end, _text in rows:
        assert isinstance(entry_id, int)
        assert title_id == title_id_1
        assert isinstance(start, float)
        assert isinstance(end, float)


def test_list_entry_rows_for_titles_empty_scope(tmp_path):
    db_path = tmp_path / "quote_index.db"
    _populate_small_corpus(db_path)
    assert list_entry_rows_for_titles(db_path, []) == []
