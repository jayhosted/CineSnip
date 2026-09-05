from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

# A short-lived connection is opened per call rather than kept as long-lived
# state, matching the existing flat-JSON subtitle cache's own
# read/write-per-operation style (app/worker/subtitles.py) — this sidesteps
# sqlite3's cross-thread connection-sharing caveats given FastAPI handlers
# here run across threadpool threads (asyncio.to_thread).


@dataclass(frozen=True)
class CachedTitle:
    guid: str
    media_id: str
    title: str
    library_name: str
    source: str = ""


@contextmanager
def _connect(db_path: Path) -> Iterator[sqlite3.Connection]:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        # search_index.py's connections onto this SAME db file set
        # journal_mode=WAL, which makes WAL mode sticky file-wide — but
        # WAL's default busy_timeout is still 0, so a writer holding a
        # transaction open (e.g. upsert_title's full-title insert) can make
        # a concurrent read here raise "database is locked" instead of
        # just waiting briefly. Match search_index.py's own busy_timeout
        # value so every connection onto this file behaves consistently.
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS cached_titles ("
            "guid TEXT PRIMARY KEY, "
            "media_id TEXT NOT NULL, "
            "title TEXT NOT NULL, "
            "library_name TEXT NOT NULL, "
            "cached_at TEXT NOT NULL"
            ")"
        )
        # Tracks the last-seen Plex section.updatedAt per configured
        # library — library_sync.py's cheap "did anything change" check.
        # Lives in the same DB as cached_titles (not a separate file) since
        # it's the same class of small, rebuildable-if-lost bookkeeping data,
        # and the removal diff needs to read both in the same operation.
        conn.execute(
            "CREATE TABLE IF NOT EXISTS library_sync_state ("
            "library_name TEXT PRIMARY KEY, "
            "last_seen_updated_at INTEGER NOT NULL, "
            "last_synced_at TEXT NOT NULL"
            ")"
        )
        # Which subtitle source a cached title actually came from
        # (SubtitleSource.value — "sidecar"/"embedded") — added after
        # cached_titles already existed on real installs, so it's a
        # guarded ALTER rather than part of the CREATE TABLE above.
        existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(cached_titles)")}
        if "source" not in existing_cols:
            conn.execute("ALTER TABLE cached_titles ADD COLUMN source TEXT")
        # Same issue #24 rating_key->media_id rename as titles/
        # no_subtitle_titles — cached_titles predates it too, and
        # scripts/migrate_to_fts5.py still reads media_id off this table.
        if "media_id" not in existing_cols and "rating_key" in existing_cols:
            conn.execute("ALTER TABLE cached_titles RENAME COLUMN rating_key TO media_id")

        # The negative case cached_titles never recorded: a title that was
        # checked and found to have neither a sidecar nor an embedded
        # subtitle track. Mirrors cached_titles's shape.
        conn.execute(
            "CREATE TABLE IF NOT EXISTS no_subtitle_titles ("
            "guid TEXT PRIMARY KEY, "
            "media_id TEXT NOT NULL, "
            "title TEXT NOT NULL, "
            "library_name TEXT NOT NULL, "
            "checked_at TEXT NOT NULL"
            ")"
        )
        # Same issue #24 rating_key->media_id migration search_index.py's
        # titles table needed — no_subtitle_titles predates the rename too,
        # and CREATE TABLE IF NOT EXISTS is a no-op against an existing
        # table under the old column name.
        no_sub_columns = {row[1] for row in conn.execute("PRAGMA table_info(no_subtitle_titles)")}
        if "media_id" not in no_sub_columns and "rating_key" in no_sub_columns:
            conn.execute("ALTER TABLE no_subtitle_titles RENAME COLUMN rating_key TO media_id")

        # Single-row table (id is always 1) tracking the current/last
        # library_sync run, for the dashboard's live panel.
        conn.execute(
            "CREATE TABLE IF NOT EXISTS library_sync_progress ("
            "id INTEGER PRIMARY KEY CHECK (id = 1), "
            "status TEXT NOT NULL, "
            "current_library TEXT, "
            "current_title TEXT, "
            "processed INTEGER NOT NULL DEFAULT 0, "
            "total INTEGER NOT NULL DEFAULT 0, "
            "started_at TEXT, "
            "last_synced_at TEXT, "
            "last_run_new_count INTEGER NOT NULL DEFAULT 0"
            ")"
        )
        conn.execute(
            "INSERT OR IGNORE INTO library_sync_progress "
            "(id, status, processed, total, last_run_new_count) VALUES (1, 'idle', 0, 0, 0)"
        )
        # Added after library_sync_progress already existed on real installs
        # (same guarded-ALTER pattern as cached_titles.source above) — lets
        # the dashboard's "last run" line show what actually happened
        # (removed/errors), not just how many titles were added.
        progress_cols = {row[1] for row in conn.execute("PRAGMA table_info(library_sync_progress)")}
        if "last_run_removed_count" not in progress_cols:
            conn.execute("ALTER TABLE library_sync_progress ADD COLUMN last_run_removed_count INTEGER NOT NULL DEFAULT 0")
        if "last_run_error_count" not in progress_cols:
            conn.execute("ALTER TABLE library_sync_progress ADD COLUMN last_run_error_count INTEGER NOT NULL DEFAULT 0")

        # Capped scrolling log for the dashboard's sync panel — trimmed to
        # the last 50 rows on every insert (see append_sync_log below), so
        # this table never grows unbounded.
        conn.execute(
            "CREATE TABLE IF NOT EXISTS library_sync_log ("
            "seq INTEGER PRIMARY KEY AUTOINCREMENT, "
            "ts TEXT NOT NULL, "
            "message TEXT NOT NULL"
            ")"
        )

        # Each library's total item count as of the last time sync_library
        # actually enumerated it (not updated on a "no changes" skip) —
        # lets the dashboard show a "titles cached / library total"
        # denominator without ever calling Plex's expensive enumerate_section
        # from a page-load hot path (see library_sync.py's sync_library).
        conn.execute(
            "CREATE TABLE IF NOT EXISTS library_item_counts ("
            "library_name TEXT PRIMARY KEY, "
            "item_count INTEGER NOT NULL, "
            "updated_at TEXT NOT NULL"
            ")"
        )
        yield conn
        conn.commit()
    finally:
        conn.close()


def get_section_updated_at(db_path: Path, library_name: str) -> int | None:
    if not db_path.exists():
        return None
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT last_seen_updated_at FROM library_sync_state WHERE library_name = ?",
            (library_name,),
        ).fetchone()
    return row[0] if row else None


def set_section_updated_at(db_path: Path, library_name: str, updated_at: int) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT INTO library_sync_state (library_name, last_seen_updated_at, last_synced_at) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(library_name) DO UPDATE SET "
            "last_seen_updated_at=excluded.last_seen_updated_at, "
            "last_synced_at=excluded.last_synced_at",
            (library_name, updated_at, datetime.now(timezone.utc).isoformat()),
        )


def list_cached_titles(db_path: Path) -> list[CachedTitle]:
    """Reads the legacy pre-FTS5 cache table — no production code writes to
    it anymore, but scripts/migrate_to_fts5.py (the one-time upgrade path
    for installs that predate the FTS5 migration) still reads whatever rows
    were written there before the migration."""
    if not db_path.exists():
        return []
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT guid, media_id, title, library_name, source FROM cached_titles"
        ).fetchall()
    return [
        CachedTitle(guid=r[0], media_id=r[1], title=r[2], library_name=r[3], source=r[4] or "")
        for r in rows
    ]


def upsert_no_subtitle_title(
    db_path: Path, guid: str, media_id: str, title: str, library_name: str
) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT INTO no_subtitle_titles (guid, media_id, title, library_name, checked_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(guid) DO UPDATE SET "
            "media_id=excluded.media_id, "
            "title=excluded.title, "
            "library_name=excluded.library_name, "
            "checked_at=excluded.checked_at",
            (guid, media_id, title, library_name, datetime.now(timezone.utc).isoformat()),
        )


def is_no_subtitle_title(db_path: Path, guid: str) -> bool:
    if not db_path.exists():
        return False
    with _connect(db_path) as conn:
        row = conn.execute("SELECT 1 FROM no_subtitle_titles WHERE guid = ?", (guid,)).fetchone()
    return row is not None


def list_no_subtitle_guids(db_path: Path) -> set[str]:
    """One-query bulk fetch of every guid in no_subtitle_titles, for a
    caller that needs to check membership across many items (e.g.
    /search-quote-extend's per-item filtering loop) without opening a
    fresh blocking SQLite connection per item."""
    if not db_path.exists():
        return set()
    with _connect(db_path) as conn:
        rows = conn.execute("SELECT guid FROM no_subtitle_titles").fetchall()
    return {row[0] for row in rows}


@dataclass(frozen=True)
class LibraryCoverage:
    sidecar_count: int
    embedded_count: int
    no_subtitle_count: int


def library_coverage(db_path: Path, library_name: str) -> LibraryCoverage:
    if not db_path.exists():
        return LibraryCoverage(sidecar_count=0, embedded_count=0, no_subtitle_count=0)
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT source, COUNT(*) FROM cached_titles WHERE library_name = ? GROUP BY source",
            (library_name,),
        ).fetchall()
        no_sub_row = conn.execute(
            "SELECT COUNT(*) FROM no_subtitle_titles WHERE library_name = ?",
            (library_name,),
        ).fetchone()
    counts = {row[0]: row[1] for row in rows}
    return LibraryCoverage(
        sidecar_count=counts.get("sidecar", 0),
        embedded_count=counts.get("embedded", 0),
        no_subtitle_count=no_sub_row[0] if no_sub_row else 0,
    )


@dataclass(frozen=True)
class SyncProgress:
    status: str
    current_library: str | None
    current_title: str | None
    processed: int
    total: int
    started_at: str | None
    last_synced_at: str | None
    last_run_new_count: int
    last_run_removed_count: int
    last_run_error_count: int


def get_sync_progress(db_path: Path) -> SyncProgress:
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT status, current_library, current_title, processed, total, "
            "started_at, last_synced_at, last_run_new_count, "
            "last_run_removed_count, last_run_error_count "
            "FROM library_sync_progress WHERE id = 1"
        ).fetchone()
    return SyncProgress(*row)


def start_sync_run(db_path: Path) -> bool:
    """Atomically flips status idle->running. Returns False (no-op) if a
    run is already in progress — the one concurrency guard shared by both
    the manual "Sync now" trigger and the scheduled library_sync_task, so
    they can never race each other for the same rows."""
    with _connect(db_path) as conn:
        cursor = conn.execute(
            "UPDATE library_sync_progress SET status='running', current_library=NULL, "
            "current_title=NULL, processed=0, total=0, started_at=? "
            "WHERE id=1 AND status='idle'",
            (datetime.now(timezone.utc).isoformat(),),
        )
        return cursor.rowcount == 1


def update_sync_progress(
    db_path: Path, current_library: str, current_title: str, processed: int, total: int
) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE library_sync_progress SET current_library=?, current_title=?, "
            "processed=?, total=? WHERE id=1",
            (current_library, current_title, processed, total),
        )


def finish_sync_run(db_path: Path, new_count: int, removed_count: int = 0, error_count: int = 0) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE library_sync_progress SET status='idle', current_library=NULL, "
            "current_title=NULL, last_synced_at=?, last_run_new_count=?, "
            "last_run_removed_count=?, last_run_error_count=? WHERE id=1",
            (datetime.now(timezone.utc).isoformat(), new_count, removed_count, error_count),
        )


def reset_stale_running_status(db_path: Path) -> None:
    """Called once at process startup (app/main.py) — a 'running' row left
    over from an unclean exit (container restart mid-sync) has no task
    actually running behind it, and would otherwise leave the dashboard
    stuck showing "Syncing" forever."""
    if not db_path.exists():
        return
    with _connect(db_path) as conn:
        conn.execute("UPDATE library_sync_progress SET status='idle' WHERE id=1 AND status='running'")


@dataclass(frozen=True)
class SyncLogLine:
    seq: int
    ts: str
    message: str


def append_sync_log(db_path: Path, message: str) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT INTO library_sync_log (ts, message) VALUES (?, ?)",
            (datetime.now(timezone.utc).isoformat(), message),
        )
        conn.execute(
            "DELETE FROM library_sync_log WHERE seq NOT IN "
            "(SELECT seq FROM library_sync_log ORDER BY seq DESC LIMIT 50)"
        )


def tail_sync_log(db_path: Path, limit: int = 50) -> list[SyncLogLine]:
    if not db_path.exists():
        return []
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT seq, ts, message FROM library_sync_log ORDER BY seq DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [SyncLogLine(seq=r[0], ts=r[1], message=r[2]) for r in reversed(rows)]


def set_library_item_count(db_path: Path, library_name: str, item_count: int) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT INTO library_item_counts (library_name, item_count, updated_at) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(library_name) DO UPDATE SET "
            "item_count=excluded.item_count, updated_at=excluded.updated_at",
            (library_name, item_count, datetime.now(timezone.utc).isoformat()),
        )


def get_library_item_count(db_path: Path, library_name: str) -> int | None:
    if not db_path.exists():
        return None
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT item_count FROM library_item_counts WHERE library_name = ?",
            (library_name,),
        ).fetchone()
    return row[0] if row else None
