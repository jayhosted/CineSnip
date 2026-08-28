"""One-off migration: backfill the legacy quote_index.cached_titles table +
per-title JSON subtitle cache into the new SQLite+FTS5 search_index schema
(app/worker/search_index.py). See docs/design/fts5-search-migration.md for
the full design/measurements behind the new schema.

Structured as a thin sibling of scripts/build_full_cache.py: a
Settings-loading module-level script, a --force flag, periodic heartbeat
progress, and a final summary line. Unlike build_full_cache.py, this script
never talks to Plex or ffmpeg — it's a pure local DB/file transformation
reading the already-synced legacy cache, so it's fast and has no external
dependencies.

Non-destructive: never deletes or modifies the legacy `cached_titles` table
or any per-title JSON cache file. Both remain untouched on disk after this
runs — the new search_index tables live in the SAME db file
(Settings.quote_index_db_path) as cached_titles, added alongside it, not
replacing it.

Idempotent/resumable: a title already present in search_index (per
search_index.has_title) is skipped unless --force is passed, so
interrupting and re-running is cheap and safe — no separate resume-state
file needed, this falls out of the upsert-by-guid schema directly.

Fingerprint handling (see CLAUDE.md's fts5-search-migration task notes for
the full reasoning): a SIDECAR-sourced title gets a real fingerprint by
stat-ing its sidecar_path directly (recorded in the legacy JSON, no Plex
needed). An EMBEDDED-sourced title's fingerprint would require resolving
the video file's path via live Plex + path mappings, which this script
deliberately doesn't depend on — so it gets fingerprint=None, same
self-healing tradeoff library_sync.py's backfill path already established
(re-extracted fresh next time it's actually touched via get_subtitles()).

Run from the repo root:

    .venv/bin/python scripts/migrate_to_fts5.py [--force]

Back up cache/quote_index.db (a plain file copy) before running against
real data — this is the rollback path if anything looks wrong afterward.
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from pathlib import Path

from app.settings import load_settings
from app.worker import quote_index, search_index
from app.worker.subtitles import SubtitleSource, _fingerprint, read_cached_subtitles

FORCE = "--force" in sys.argv


@dataclass(frozen=True)
class MigrationOutcome:
    guid: str
    status: str  # "migrated" | "skipped" | "missing_json" | "corrupt_json"
    fingerprint_kind: str | None = None  # "sidecar" | "embedded" | "none" | None


def _tuple_fingerprint(path: Path | None) -> tuple[float, int] | None:
    """search_index.upsert_title wants a (mtime, size) tuple; subtitles.py's
    _fingerprint returns [mtime, size] (a list, for JSON round-tripping)."""
    fp = _fingerprint(path)
    return (fp[0], fp[1]) if fp is not None else None


def migrate_one_title(
    db_path: Path, cache_dir: Path, cached: quote_index.CachedTitle, force: bool = False
) -> MigrationOutcome:
    """Migrate a single legacy cached_titles row into search_index. Pure
    function of its arguments (no globals) so it's directly testable
    without going through the CLI/module-level Settings machinery."""
    if not force and search_index.has_title(db_path, cached.guid):
        return MigrationOutcome(guid=cached.guid, status="skipped")

    result = read_cached_subtitles(cache_dir, cached.guid)
    if result is None:
        return MigrationOutcome(guid=cached.guid, status="missing_json")

    fingerprint: tuple[float, int] | None
    fingerprint_kind: str
    if result.source is SubtitleSource.SIDECAR and result.sidecar_path:
        fingerprint = _tuple_fingerprint(Path(result.sidecar_path))
        fingerprint_kind = "sidecar"
    elif result.source is SubtitleSource.EMBEDDED:
        # No video path available without live Plex — deliberately left
        # unfingerprinted; self-heals on next real touch (see module
        # docstring).
        fingerprint = None
        fingerprint_kind = "embedded"
    else:
        fingerprint = None
        fingerprint_kind = "none"

    search_index.upsert_title(
        db_path,
        cached.guid,
        cached.rating_key,
        cached.title,
        cached.library_name,
        result.source.value,
        result.sidecar_path,
        result.stream_index,
        result.entries,
        fingerprint,
    )
    return MigrationOutcome(guid=cached.guid, status="migrated", fingerprint_kind=fingerprint_kind)


def main() -> None:
    settings = load_settings()
    db_path = settings.quote_index_db_path
    cache_dir = settings.cache_dir

    cached_titles = quote_index.list_cached_titles(db_path)
    total = len(cached_titles)
    print(f"Found {total} legacy cached titles. Starting migration…", flush=True)

    counts = {
        "migrated": 0,
        "skipped": 0,
        "missing_json": 0,
        "corrupt_json": 0,
    }
    fingerprint_counts = {"sidecar": 0, "embedded": 0, "none": 0}
    failures: list[str] = []

    t0 = time.monotonic()

    for i, cached in enumerate(cached_titles, start=1):
        try:
            outcome = migrate_one_title(db_path, cache_dir, cached, force=FORCE)
        except Exception as exc:  # noqa: BLE001 - report and continue, don't abort the run
            outcome = MigrationOutcome(guid=cached.guid, status="corrupt_json")
            failures.append(f"{cached.title!r} (guid={cached.guid}): {exc}")

        counts[outcome.status] += 1
        if outcome.fingerprint_kind is not None:
            fingerprint_counts[outcome.fingerprint_kind] += 1
        if outcome.status == "missing_json":
            failures.append(f"{cached.title!r} (guid={cached.guid}): legacy JSON missing/unreadable")

        if outcome.status in ("missing_json", "corrupt_json") or i % 250 == 0 or i == total:
            elapsed = time.monotonic() - t0
            print(
                f"[{i}/{total}] ({elapsed:.0f}s elapsed) "
                f"migrated={counts['migrated']} skipped={counts['skipped']} "
                f"missing_json={counts['missing_json']} corrupt_json={counts['corrupt_json']}"
                + (f" -- {outcome.status}: {cached.title!r}" if outcome.status in ("missing_json", "corrupt_json") else ""),
                flush=True,
            )

    elapsed = time.monotonic() - t0
    print(
        f"\nDONE in {elapsed:.0f}s ({elapsed/60:.1f}min). "
        f"total={total} migrated={counts['migrated']} skipped={counts['skipped']} "
        f"missing_json={counts['missing_json']} corrupt_json={counts['corrupt_json']}",
        flush=True,
    )
    print(
        f"Fingerprint split among migrated titles: "
        f"sidecar={fingerprint_counts['sidecar']} embedded={fingerprint_counts['embedded']} "
        f"none={fingerprint_counts['none']}",
        flush=True,
    )
    if failures:
        print(f"\n{len(failures)} title(s) failed to migrate cleanly:", flush=True)
        for line in failures:
            print(f"  - {line}", flush=True)


if __name__ == "__main__":
    main()
