import sqlite3
import threading
import time

from app.worker.quote_index import CachedTitle
from app.worker.search_index import (
    _connect,
    fetch_entries_for_titles,
    get_entries,
    get_fingerprint,
    has_title,
    iter_all_entries,
    list_titles,
    list_titles_for_library,
    remove_title,
    search_title_ids,
    upsert_title,
)
from app.worker.subtitles import SubtitleEntry


def _entries(*texts):
    return [
        SubtitleEntry(index=i, start=float(i), end=float(i) + 1.0, text=text)
        for i, text in enumerate(texts)
    ]


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
    assert search_title_ids(db_path, ["first"]) == []


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
    ids_before = search_title_ids(db_path, ["zephyr"])
    assert len(ids_before) == 1

    remove_title(db_path, "guid-2")

    assert has_title(db_path, "guid-2") is False
    assert list_titles_for_library(db_path, "3D") == []
    assert get_entries(db_path, "guid-2") is None
    # no orphaned entries/entries_fts rows left behind
    assert search_title_ids(db_path, ["zephyr"]) == []


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

    fox_ids = search_title_ids(db_path, ["fox"])
    assert len(fox_ids) == 1

    zephyr_ids = search_title_ids(db_path, ["zephyr"])
    assert len(zephyr_ids) == 1
    assert zephyr_ids != fox_ids

    assert search_title_ids(db_path, ["nonexistentword"]) == []
    assert search_title_ids(db_path, []) == []


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
    assert search_title_ids(db_path, ["a"]) != []
    assert search_title_ids(db_path, ["007"]) != []


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
