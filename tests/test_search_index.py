import sqlite3
import threading
import time

from app.worker.quote_index import CachedTitle
from app.worker.search_index import (
    _connect,
    coverage_counts,
    fetch_entries_for_titles,
    fetch_entry_windows,
    get_entries,
    get_fingerprint,
    get_title_ids_by_guid,
    has_title,
    iter_all_entries,
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


def test_upsert_title_round_trip(tmp_path):
    db_path = tmp_path / "quote_index.db"
    entries = _entries("Hello, world!", "Goodbye, world.")
    upsert_title(
        db_path,
        guid="guid-1",
        rating_key=101,
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
        rating_key=1,
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
        rating_key=101,
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
        rating_key=101,
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
        rating_key=101,
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
        rating_key=101,
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
        rating_key=101,
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
        rating_key=102,
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
        CachedTitle(guid="guid-1", rating_key=101, title="Film One", library_name="Movies", source="sidecar"),
        CachedTitle(guid="guid-2", rating_key=102, title="Film Two", library_name="3D", source="embedded"),
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
        rating_key=101,
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


def test_fetch_entries_for_titles(tmp_path):
    db_path = tmp_path / "quote_index.db"
    _populate_small_corpus(db_path)
    titles = {t.guid: t for t in list_titles(db_path)}

    with _connect(db_path) as conn:
        rows = conn.execute("SELECT guid, title_id FROM titles").fetchall()
    guid_to_id = dict(rows)

    result = fetch_entries_for_titles(db_path, list(guid_to_id.values()))

    assert set(result.keys()) == set(guid_to_id.values())
    id1 = guid_to_id["guid-1"]
    assert [e.text for e in result[id1]] == ["the quick brown fox", "jumps over the lazy dog"]


def test_fetch_entries_for_titles_empty_input(tmp_path):
    db_path = tmp_path / "quote_index.db"
    _populate_small_corpus(db_path)
    assert fetch_entries_for_titles(db_path, []) == {}


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
    guid_to_id = {t.guid: t.rating_key for t in list_titles(db_path)}
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
        db_path, guid="guid-1", rating_key=1, title="Film", library_name="Movies",
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
            db_path, guid=f"guid-{i}", rating_key=i, title=f"Film {i}", library_name="Movies",
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
        db_path, guid="guid-1", rating_key=1, title="Film", library_name="Movies",
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
            db_path, guid=f"guid-{i}", rating_key=i, title=f"Film {i}", library_name="Movies",
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
        db_path, guid="g1", rating_key=1, title="Sidecar Film", library_name="Movies",
        source="sidecar", sidecar_path=None, stream_index=None, entries=[], fingerprint=None,
    )
    upsert_title(
        db_path, guid="g2", rating_key=2, title="Sidecar Film Two", library_name="Movies",
        source="sidecar", sidecar_path=None, stream_index=None, entries=[], fingerprint=None,
    )
    upsert_title(
        db_path, guid="g3", rating_key=3, title="Embedded Film", library_name="Movies",
        source="embedded", sidecar_path=None, stream_index=2, entries=[], fingerprint=None,
    )
    upsert_title(
        db_path, guid="g4", rating_key=4, title="No Subtitle Film", library_name="Movies",
        source="none", sidecar_path=None, stream_index=None, entries=[], fingerprint=None,
    )
    # a different library must not bleed into Movies' counts
    upsert_title(
        db_path, guid="g5", rating_key=5, title="3D Film", library_name="3D",
        source="sidecar", sidecar_path=None, stream_index=None, entries=[], fingerprint=None,
    )

    assert coverage_counts(db_path, "Movies") == {"sidecar": 2, "embedded": 1}
    assert coverage_counts(db_path, "3D") == {"sidecar": 1, "embedded": 0}


def test_coverage_counts_on_missing_db_returns_zeros(tmp_path):
    assert coverage_counts(tmp_path / "does-not-exist.db", "Movies") == {"sidecar": 0, "embedded": 0}


def test_coverage_counts_on_unknown_library_returns_zeros(tmp_path):
    db_path = tmp_path / "quote_index.db"
    upsert_title(
        db_path, guid="g1", rating_key=1, title="Film", library_name="Movies",
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
        rating_key=101,
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
        rating_key=101,
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
        rating_key=102,
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
