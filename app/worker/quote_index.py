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
    rating_key: int
    title: str
    library_name: str


@contextmanager
def _connect(db_path: Path) -> Iterator[sqlite3.Connection]:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS cached_titles ("
            "guid TEXT PRIMARY KEY, "
            "rating_key INTEGER NOT NULL, "
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
        yield conn
        conn.commit()
    finally:
        conn.close()


def upsert_cached_title(
    db_path: Path, guid: str, rating_key: int, title: str, library_name: str
) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT INTO cached_titles (guid, rating_key, title, library_name, cached_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(guid) DO UPDATE SET "
            "rating_key=excluded.rating_key, "
            "title=excluded.title, "
            "library_name=excluded.library_name, "
            "cached_at=excluded.cached_at",
            (guid, rating_key, title, library_name, datetime.now(timezone.utc).isoformat()),
        )


def list_cached_titles(db_path: Path) -> list[CachedTitle]:
    if not db_path.exists():
        return []
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT guid, rating_key, title, library_name FROM cached_titles"
        ).fetchall()
    return [CachedTitle(guid=r[0], rating_key=r[1], title=r[2], library_name=r[3]) for r in rows]


def list_cached_titles_for_library(db_path: Path, library_name: str) -> list[CachedTitle]:
    if not db_path.exists():
        return []
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT guid, rating_key, title, library_name FROM cached_titles "
            "WHERE library_name = ?",
            (library_name,),
        ).fetchall()
    return [CachedTitle(guid=r[0], rating_key=r[1], title=r[2], library_name=r[3]) for r in rows]


def remove_cached_title(db_path: Path, guid: str) -> None:
    with _connect(db_path) as conn:
        conn.execute("DELETE FROM cached_titles WHERE guid = ?", (guid,))


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
