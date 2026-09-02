from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Iterator

if TYPE_CHECKING:
    from app.worker.quote_index import CachedTitle
    from app.worker.subtitles import SubtitleEntry

# Short-lived connection per call, matching quote_index.py's own style —
# this module's tables live in the SAME db file as quote_index.py's
# bookkeeping tables (Settings.quote_index_db_path), not a separate file.
#
# WAL mode + a busy_timeout are set on every connection (not just writers)
# per the project's concurrency requirement: FastAPI handlers run across
# threadpool threads (asyncio.to_thread), so a read must never block behind
# or error out from a concurrent write.


@contextmanager
def _connect(db_path: Path) -> Iterator[sqlite3.Connection]:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS titles("
            "title_id INTEGER PRIMARY KEY, "
            "guid TEXT UNIQUE NOT NULL, "
            "media_id TEXT NOT NULL, "
            "title TEXT NOT NULL, "
            "library_name TEXT NOT NULL, "
            "source TEXT, "
            "sidecar_path TEXT, "
            "stream_index INTEGER, "
            "cached_at TEXT NOT NULL, "
            "fingerprint_mtime REAL, "
            "fingerprint_size INTEGER"
            ")"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS entries("
            "id INTEGER PRIMARY KEY, "
            "title_id INTEGER NOT NULL REFERENCES titles(title_id) ON DELETE CASCADE, "
            "idx INTEGER NOT NULL, "
            "start REAL NOT NULL, "
            "end REAL NOT NULL, "
            "display_text TEXT NOT NULL"
            ")"
        )
        # issue #24 renamed titles.rating_key -> titles.media_id, but
        # CREATE TABLE IF NOT EXISTS is a no-op against a titles table that
        # already existed under the old name — an install upgrading in
        # place keeps the old column forever and every media_id-based query
        # fails with "no such column: media_id" (first hit: /search-quote-extend,
        # which crashes mid-stream and surfaces to the bot as an httpx
        # "incomplete chunked read" instead of the real error). Migrate the
        # column in place on first connect after the upgrade.
        existing_columns = {row[1] for row in conn.execute("PRAGMA table_info(titles)")}
        if "media_id" not in existing_columns and "rating_key" in existing_columns:
            conn.execute("ALTER TABLE titles RENAME COLUMN rating_key TO media_id")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_entries_title_id ON entries(title_id, idx)"
        )
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS entries_fts USING fts5(normalized_text, content='')"
        )
        yield conn
        conn.commit()
    finally:
        conn.close()


def _delete_entries_for_title(conn: sqlite3.Connection, title_id: int) -> None:
    """Remove all entries/entries_fts rows for a title.

    entries_fts is a contentless FTS5 table (content=''), and this SQLite
    build doesn't have contentless_delete=1 set, so a plain
    ``DELETE FROM entries_fts WHERE rowid IN (...)`` raises
    "cannot DELETE from contentless fts5 table". Contentless tables instead
    require the special 'delete' command, passing back the same column
    value that was originally indexed — recomputed here from the still-live
    entries.display_text before the entries row itself is removed.
    """
    from app.worker.quotes import normalize_for_match

    rows = conn.execute(
        "SELECT id, display_text FROM entries WHERE title_id = ?", (title_id,)
    ).fetchall()
    for entry_id, display_text in rows:
        conn.execute(
            "INSERT INTO entries_fts(entries_fts, rowid, normalized_text) VALUES('delete', ?, ?)",
            (entry_id, normalize_for_match(display_text)),
        )
    conn.execute("DELETE FROM entries WHERE title_id = ?", (title_id,))


def upsert_title(
    db_path: Path,
    guid: str,
    media_id: str,
    title: str,
    library_name: str,
    source: str | None,
    sidecar_path: str | None,
    stream_index: int | None,
    entries: list[SubtitleEntry],
    fingerprint: tuple[float, int] | None,
) -> None:
    """Upsert a title's row plus its full set of subtitle entries (and their
    FTS index rows) in one transaction. Existing entries for the title are
    replaced wholesale rather than diffed, since a title's subtitles are
    only ever re-parsed as a unit."""
    from app.worker.quotes import normalize_for_match

    fingerprint_mtime, fingerprint_size = fingerprint if fingerprint else (None, None)
    cached_at = datetime.now(timezone.utc).isoformat()

    with _connect(db_path) as conn:
        conn.execute(
            "INSERT INTO titles "
            "(guid, media_id, title, library_name, source, sidecar_path, stream_index, "
            "cached_at, fingerprint_mtime, fingerprint_size) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(guid) DO UPDATE SET "
            "media_id=excluded.media_id, "
            "title=excluded.title, "
            "library_name=excluded.library_name, "
            "source=excluded.source, "
            "sidecar_path=excluded.sidecar_path, "
            "stream_index=excluded.stream_index, "
            "cached_at=excluded.cached_at, "
            "fingerprint_mtime=excluded.fingerprint_mtime, "
            "fingerprint_size=excluded.fingerprint_size",
            (
                guid,
                media_id,
                title,
                library_name,
                source,
                sidecar_path,
                stream_index,
                cached_at,
                fingerprint_mtime,
                fingerprint_size,
            ),
        )
        title_id = conn.execute(
            "SELECT title_id FROM titles WHERE guid = ?", (guid,)
        ).fetchone()[0]

        # Clear any previous entries/FTS rows for this title before
        # reinserting — contentless FTS5 tables don't cascade on
        # ON DELETE CASCADE, so both sides are cleared explicitly.
        _delete_entries_for_title(conn, title_id)

        for entry in entries:
            cursor = conn.execute(
                "INSERT INTO entries (title_id, idx, start, end, display_text) "
                "VALUES (?, ?, ?, ?, ?)",
                (title_id, entry.index, entry.start, entry.end, entry.text),
            )
            entry_id = cursor.lastrowid
            normalized_text = normalize_for_match(entry.text)
            conn.execute(
                "INSERT INTO entries_fts (rowid, normalized_text) VALUES (?, ?)",
                (entry_id, normalized_text),
            )


def get_entries(db_path: Path, guid: str) -> list[SubtitleEntry] | None:
    """Returns None if the guid has no title row (not yet cached), or a
    list of SubtitleEntry (possibly empty) if it is cached."""
    from app.worker.subtitles import SubtitleEntry

    if not db_path.exists():
        return None
    with _connect(db_path) as conn:
        title_row = conn.execute(
            "SELECT title_id FROM titles WHERE guid = ?", (guid,)
        ).fetchone()
        if title_row is None:
            return None
        rows = conn.execute(
            "SELECT idx, start, end, display_text FROM entries "
            "JOIN titles USING(title_id) WHERE guid = ? ORDER BY idx",
            (guid,),
        ).fetchall()
    return [SubtitleEntry(index=r[0], start=r[1], end=r[2], text=r[3]) for r in rows]


def get_source_info(db_path: Path, guid: str) -> tuple[str | None, str | None, int | None] | None:
    """Returns (source, sidecar_path, stream_index) for a cached title, or
    None if the guid has no title row at all. Split out from get_entries()/
    get_fingerprint() because subtitles.get_subtitles() needs this metadata
    to reconstruct a SubtitleResult on a cache hit, but neither of those
    two carries it."""
    if not db_path.exists():
        return None
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT source, sidecar_path, stream_index FROM titles WHERE guid = ?",
            (guid,),
        ).fetchone()
    if row is None:
        return None
    return (row[0], row[1], row[2])


def get_fingerprint(db_path: Path, guid: str) -> tuple[float, int] | None:
    if not db_path.exists():
        return None
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT fingerprint_mtime, fingerprint_size FROM titles WHERE guid = ?",
            (guid,),
        ).fetchone()
    if row is None or row[0] is None or row[1] is None:
        return None
    return (row[0], row[1])


def list_titles(db_path: Path) -> list[CachedTitle]:
    from app.worker.quote_index import CachedTitle

    if not db_path.exists():
        return []
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT guid, media_id, title, library_name, source FROM titles"
        ).fetchall()
    return [
        CachedTitle(guid=r[0], media_id=r[1], title=r[2], library_name=r[3], source=r[4] or "")
        for r in rows
    ]


def list_titles_for_library(db_path: Path, library_name: str) -> list[CachedTitle]:
    from app.worker.quote_index import CachedTitle

    if not db_path.exists():
        return []
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT guid, media_id, title, library_name, source FROM titles "
            "WHERE library_name = ?",
            (library_name,),
        ).fetchall()
    return [
        CachedTitle(guid=r[0], media_id=r[1], title=r[2], library_name=r[3], source=r[4] or "")
        for r in rows
    ]


def has_title(db_path: Path, guid: str) -> bool:
    if not db_path.exists():
        return False
    with _connect(db_path) as conn:
        row = conn.execute("SELECT 1 FROM titles WHERE guid = ?", (guid,)).fetchone()
    return row is not None


def remove_title(db_path: Path, guid: str) -> None:
    with _connect(db_path) as conn:
        title_row = conn.execute(
            "SELECT title_id FROM titles WHERE guid = ?", (guid,)
        ).fetchone()
        if title_row is None:
            return
        title_id = title_row[0]
        _delete_entries_for_title(conn, title_id)
        conn.execute("DELETE FROM titles WHERE title_id = ?", (title_id,))


def get_title_ids_by_guid(db_path: Path, guids: list[str]) -> dict[str, int]:
    """Bulk guid -> title_id lookup, used by library_search.py to resolve a
    caller-supplied list[CachedTitle] (which carries no title_id of its own
    — a CachedTitle built synthetically for a request, e.g. TV whole-show
    search, has no such field) down to a title_id scope for
    search_entry_ids()/iter_all_entries(). Guids with no matching title row
    (not yet cached) are simply absent from the result, not an error."""
    if not guids:
        return {}
    if not db_path.exists():
        return {}
    placeholders = ",".join("?" for _ in guids)
    with _connect(db_path) as conn:
        rows = conn.execute(
            f"SELECT guid, title_id FROM titles WHERE guid IN ({placeholders})",
            guids,
        ).fetchall()
    return {guid: title_id for guid, title_id in rows}


def search_entry_ids(
    db_path: Path,
    tokens: list[str],
    limit: int = 4000,
    title_ids: list[int] | None = None,
) -> list[tuple[int, int, int]]:
    """FTS5 pre-filter returning up to `limit` individual surviving ENTRY
    rows (not distinct titles) as (entry_id, title_id, idx) tuples, ranked
    by FTS5's own relevance ordering. `idx` (the entry's own subtitle-cue
    number within its title) is included so the caller can compute an
    adjacent-cue window directly, without a second query to look it up.

    This is deliberately entry-level, not title-level: capping to distinct
    titles let a single query's fuzzy-scoring pass balloon to scoring every
    entry of every title with any surviving hit — up to 46% of a real
    ~7.5M-entry corpus, a ~700x amplification over the intended budget (see
    docs/design/fts5-search-migration.md). Capping the entry rows
    themselves, then having the caller (library_search.py) expand only
    those specific survivors into small adjacent-cue windows, is what
    actually keeps the fuzzy-scoring pass bounded.

    `title_ids`, when given, scopes BOTH the FTS5 match and the ranking to
    just those titles (e.g. one show's episodes for /snip tv's whole-show
    search) — without it, a global top-`limit` cap could starve out a
    narrow-scoped caller's own titles entirely (confirmed on the real
    library: a 12-episode show's search only had 5 episodes survive the
    unscoped global cap).
    """
    if not tokens:
        return []
    if not db_path.exists():
        return []
    if title_ids is not None and not title_ids:
        return []
    match_query = " OR ".join(tokens)
    with _connect(db_path) as conn:
        if title_ids is None:
            rows = conn.execute(
                "SELECT entries.id, entries.title_id, entries.idx FROM entries "
                "JOIN entries_fts ON entries.id = entries_fts.rowid "
                "WHERE entries_fts MATCH ? "
                "ORDER BY rank LIMIT ?",
                (match_query, limit),
            ).fetchall()
        else:
            placeholders = ",".join("?" for _ in title_ids)
            rows = conn.execute(
                f"SELECT entries.id, entries.title_id, entries.idx FROM entries "
                f"JOIN entries_fts ON entries.id = entries_fts.rowid "
                f"WHERE entries_fts MATCH ? AND entries.title_id IN ({placeholders}) "
                f"ORDER BY rank LIMIT ?",
                (match_query, *title_ids, limit),
            ).fetchall()
    return [(r[0], r[1], r[2]) for r in rows]


def pick_random_entry_id(
    db_path: Path,
    title_ids: list[int],
    exclude_entry_ids: frozenset[int] = frozenset(),
) -> tuple[int, int, int] | None:
    """Pick one uniformly-random entry row, scoped to `title_ids`, as
    (entry_id, title_id, idx) — same tuple shape as search_entry_ids' rows,
    so callers can feed it straight into fetch_entry_windows. Used by
    /snip random's no-quote path (a genuinely random cached line), scoped
    to whatever media-type filter the caller already resolved to title_ids.

    `exclude_entry_ids` lets a reroll journey avoid repeating a line
    already shown this session (CLAUDE.md's /snip random shuffle-history
    fix) — returns None once every scoped entry has been excluded.
    """
    if not db_path.exists():
        return None
    if not title_ids:
        return None
    placeholders = ",".join("?" for _ in title_ids)
    params: list[int] = list(title_ids)
    exclude_clause = ""
    if exclude_entry_ids:
        exclude_placeholders = ",".join("?" for _ in exclude_entry_ids)
        exclude_clause = f" AND id NOT IN ({exclude_placeholders})"
        params.extend(exclude_entry_ids)
    with _connect(db_path) as conn:
        row = conn.execute(
            f"SELECT id, title_id, idx FROM entries "
            f"WHERE title_id IN ({placeholders}){exclude_clause} "
            f"ORDER BY RANDOM() LIMIT 1",
            params,
        ).fetchone()
    if row is None:
        return None
    return (row[0], row[1], row[2])


def count_entries(db_path: Path, title_ids: list[int]) -> int:
    """Total entry rows scoped to `title_ids` — used to report/detect a
    random-pick pool's size (e.g. "only 1 match" for a narrow quote filter,
    or when a reroll journey has exhausted every candidate)."""
    if not db_path.exists():
        return 0
    if not title_ids:
        return 0
    placeholders = ",".join("?" for _ in title_ids)
    with _connect(db_path) as conn:
        row = conn.execute(
            f"SELECT COUNT(*) FROM entries WHERE title_id IN ({placeholders})",
            title_ids,
        ).fetchone()
    return row[0] if row else 0


def list_entry_rows_for_titles(
    db_path: Path, title_ids: list[int]
) -> list[tuple[int, int, int, float, float, str]]:
    """(entry_id, title_id, idx, start, end, display_text) for every entry
    scoped to title_ids. Used by the "filtered random" pick (min-word-count
    quality filter) where the pool is small enough — a single title or one
    show's episodes — to fetch and filter in Python rather than needing a
    SQL word-count approximation, and without an N+1 fetch per candidate."""
    if not title_ids:
        return []
    if not db_path.exists():
        return []
    placeholders = ",".join("?" for _ in title_ids)
    with _connect(db_path) as conn:
        rows = conn.execute(
            f"SELECT id, title_id, idx, start, end, display_text FROM entries "
            f"WHERE title_id IN ({placeholders})",
            title_ids,
        ).fetchall()
    return [(r[0], r[1], r[2], r[3], r[4], r[5]) for r in rows]


def fetch_entry_windows(
    db_path: Path, windows: list[tuple[int, int, int]]
) -> dict[int, list[tuple[int, SubtitleEntry]]]:
    """Fetch just the entries inside a set of (title_id, idx_lo, idx_hi)
    windows, grouped by title_id and idx-ordered within each title.

    Deliberately NOT a "fetch this title's full entry list" call:
    library_search.py's slice expansion only ever needs the small
    adjacent-cue window around each FTS5 hit, and
    a title's full subtitle track can be hundreds of entries — fetching all
    of them for every title with any surviving hit was measured to
    dominate real-library search time (~4s of a ~10s query) despite the
    entry-level LIMIT already keeping the *scoring* pass small. Windows are
    batched into chunks to stay well under SQLite's default bound-parameter
    limit (999) while still sharing one connection/transaction.
    """
    from app.worker.subtitles import SubtitleEntry

    if not windows:
        return {}
    if not db_path.exists():
        return {}

    result: dict[int, list[tuple[int, SubtitleEntry]]] = {}
    chunk_size = 200  # 200 windows * 3 params/window = 600 params, under the 999 default limit
    with _connect(db_path) as conn:
        for i in range(0, len(windows), chunk_size):
            chunk = windows[i : i + chunk_size]
            clauses = " OR ".join(["(title_id = ? AND idx BETWEEN ? AND ?)"] * len(chunk))
            params = [value for window in chunk for value in window]
            rows = conn.execute(
                f"SELECT title_id, id, idx, start, end, display_text FROM entries "
                f"WHERE {clauses} ORDER BY title_id, idx",
                params,
            ).fetchall()
            for title_id, entry_id, idx, start, end, display_text in rows:
                result.setdefault(title_id, []).append(
                    (entry_id, SubtitleEntry(index=idx, start=start, end=end, text=display_text))
                )
    return result


def coverage_counts(db_path: Path, library_name: str) -> dict[str, int]:
    """sidecar/embedded row counts for a library, for the dashboard's
    coverage panel. get_subtitles() (subtitles.py) upserts a titles row for
    every outcome, including the no-subtitle case (source='none'), so that
    value is deliberately excluded here rather than counted as either
    bucket — the dashboard sources its no-subtitle count from
    quote_index.no_subtitle_titles instead, which is unaffected by this
    migration."""
    if not db_path.exists():
        return {"sidecar": 0, "embedded": 0}
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT source, COUNT(*) FROM titles WHERE library_name = ? "
            "AND source IN ('sidecar', 'embedded') GROUP BY source",
            (library_name,),
        ).fetchall()
    counts = {row[0]: row[1] for row in rows}
    return {"sidecar": counts.get("sidecar", 0), "embedded": counts.get("embedded", 0)}


def iter_all_entries(
    db_path: Path, title_ids: list[int] | None = None
) -> Iterator[tuple[str, list[SubtitleEntry]]]:
    """Full scan of every cached title's entries, used as the fallback when
    the FTS5 pre-filter finds nothing (e.g. a typo'd query). `title_ids`,
    when given, scopes the scan to just those titles — without it, a
    narrow-scoped caller (e.g. /snip tv's whole-show search) would fall
    back to streaming the ENTIRE corpus (measured: ~9.6s over 7.5M entries)
    instead of just its own handful of episodes."""
    from app.worker.subtitles import SubtitleEntry

    if not db_path.exists():
        return
    if title_ids is not None and not title_ids:
        return
    with _connect(db_path) as conn:
        if title_ids is None:
            title_rows = conn.execute(
                "SELECT title_id, guid FROM titles ORDER BY title_id"
            ).fetchall()
        else:
            placeholders = ",".join("?" for _ in title_ids)
            title_rows = conn.execute(
                f"SELECT title_id, guid FROM titles WHERE title_id IN ({placeholders}) "
                f"ORDER BY title_id",
                title_ids,
            ).fetchall()
        for title_id, guid in title_rows:
            entry_rows = conn.execute(
                "SELECT idx, start, end, display_text FROM entries "
                "WHERE title_id = ? ORDER BY idx",
                (title_id,),
            ).fetchall()
            entries = [
                SubtitleEntry(index=r[0], start=r[1], end=r[2], text=r[3]) for r in entry_rows
            ]
            yield (guid, entries)
