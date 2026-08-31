from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

# Lives in the SAME db file as search_index.py/quote_index.py's bookkeeping
# tables (Settings.quote_index_db_path) — the project's one existing
# per-title SQLite cache file — rather than a separate one.
#
# Keyed by file path + an mtime/size fingerprint (mirroring
# search_index.py's titles.fingerprint_mtime/fingerprint_size), not by
# Plex media_id/guid: the detected crop is purely a property of this
# file's own pixels, so the fingerprint alone is what determines whether a
# cached value can still be trusted — a media_id would be a second,
# redundant key carrying no extra correctness guarantee.
#
# A cropdetect probe genuinely costs real time (~3s even on a short clip
# window, measured against a real 4K HDR remux — no cache would mean
# paying that on every render). Most titles have NO baked-in bars at all,
# so "confirmed: no crop needed" must itself be cacheable (crop_w IS NULL
# on a HIT) and stay distinct from "never probed" (a MISS, i.e.
# get_cached_crop returning None) — otherwise every ordinary title would
# re-probe forever.


@dataclass(frozen=True)
class CachedCrop:
    crop_box: tuple[int, int, int, int] | None


@contextmanager
def _connect(db_path: Path) -> Iterator[sqlite3.Connection]:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS crop_cache("
            "path TEXT PRIMARY KEY, "
            "crop_w INTEGER, "
            "crop_h INTEGER, "
            "crop_x INTEGER, "
            "crop_y INTEGER, "
            "fingerprint_mtime REAL NOT NULL, "
            "fingerprint_size INTEGER NOT NULL, "
            "probed_at TEXT NOT NULL"
            ")"
        )
        yield conn
        conn.commit()
    finally:
        conn.close()


def get_cached_crop(
    db_path: Path, path: str, fingerprint: tuple[float, int]
) -> CachedCrop | None:
    """None means "not cached, must probe" — including a fingerprint
    mismatch, which invalidates any previously cached value the same way.
    A cache hit's own CachedCrop.crop_box may itself be None, meaning
    "already probed, confirmed no bars"."""
    mtime, size = fingerprint
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT crop_w, crop_h, crop_x, crop_y, fingerprint_mtime, fingerprint_size "
            "FROM crop_cache WHERE path = ?",
            (str(path),),
        ).fetchone()
    if row is None:
        return None
    w, h, x, y, cached_mtime, cached_size = row
    if cached_mtime != mtime or cached_size != size:
        return None
    if w is None:
        return CachedCrop(crop_box=None)
    return CachedCrop(crop_box=(w, h, x, y))


def set_cached_crop(
    db_path: Path,
    path: str,
    crop_box: tuple[int, int, int, int] | None,
    fingerprint: tuple[float, int],
) -> None:
    w, h, x, y = crop_box if crop_box is not None else (None, None, None, None)
    mtime, size = fingerprint
    probed_at = datetime.now(timezone.utc).isoformat()
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT INTO crop_cache"
            "(path, crop_w, crop_h, crop_x, crop_y, fingerprint_mtime, fingerprint_size, probed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(path) DO UPDATE SET "
            "crop_w=excluded.crop_w, "
            "crop_h=excluded.crop_h, "
            "crop_x=excluded.crop_x, "
            "crop_y=excluded.crop_y, "
            "fingerprint_mtime=excluded.fingerprint_mtime, "
            "fingerprint_size=excluded.fingerprint_size, "
            "probed_at=excluded.probed_at",
            (str(path), w, h, x, y, mtime, size, probed_at),
        )
