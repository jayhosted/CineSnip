# Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a "Dashboard" page to the local web app that becomes the new default landing page, showing subtitle-cache coverage stats and a live view of `library_sync`'s background activity with a manual "Sync now" trigger.

**Architecture:** New persisted state in `quote_index.db` (source-type per cached title, a table for "checked, no subtitles" titles, a single-row sync-progress table, a capped sync-log table) feeds both a one-shot stats query and a Server-Sent-Events stream that a new `/dashboard` route in `app/web/` renders. `library_sync.py` is extended to write that state as it runs; nothing about its existing sync/removal logic changes.

**Tech Stack:** FastAPI + Jinja2 + htmx (existing web app), sqlite3 (existing `quote_index.db`), vanilla `EventSource` for the one new SSE consumer (no new JS library).

**Spec:** [docs/superpowers/specs/2026-08-28-dashboard-design.md](../specs/2026-08-28-dashboard-design.md)

## Global Constraints

- No client-side JS framework — server-rendered Jinja2 + htmx partial swaps is the existing pattern; the one exception is a small inline `EventSource` script for the SSE panel, matching `shell.html`'s existing inline-script precedent (`cinesnipOpenNav`/`cinesnipCloseNav`).
- Mint/teal accent (`--accent`, `--accent-strong`) on dark backgrounds must be kept — reuse existing CSS custom properties, add new ones only where the mockup needs a color that doesn't exist yet (`--neutral-fill`).
- Tokens (Discord/Plex) must never be logged — not touched by this feature, but no new code path here should start doing so either.
- `quote_index.py`'s existing style: a short-lived `sqlite3` connection opened per call via `_connect()`, not a long-lived shared connection (cross-thread safety, since FastAPI handlers run across threadpool threads).
- Every new/changed `quote_index.py` function needs a plain `def test_...(tmp_path)` unit test, matching `tests/test_quote_index.py`'s existing style (no pytest-asyncio; async code under test is driven via `asyncio.run(...)` per `tests/test_library_sync.py`).

## Corrections made during planning (vs. the spec)

Two things the spec got wrong, caught by re-reading the actual code before writing tasks — noted here so the discrepancy isn't a surprise mid-implementation:

1. **The spec's claim that a no-subtitle title "gets fully reprocessed every single sync cycle forever" is incorrect.** `subtitles.get_subtitles()` already writes a cache JSON file even for a `SubtitleSource.NONE` result ([subtitles.py:388-390](../../../app/worker/subtitles.py)), and `sync_one_title`'s existing `cache_path_for_guid(...).exists()` check already skips re-probing it. The real gap is narrower and cheaper to fix: `quote_index.upsert_cached_title` is simply never called for a `NONE` result, so nothing about it is queryable. Task 2 below adds a `no_subtitle_titles` write on the `NONE` branch — no changes to the skip logic itself.
2. **No separate Plex-querying backfill script is needed or possible** for historical no-subtitle titles: their cache JSON only ever stored a bare `guid` (no `rating_key`/`title`/`library_name` — those were never worth recording for a title that isn't searchable). Task 2 instead makes `sync_one_title` self-healing: the very first time it revisits a title whose cache file exists but isn't yet in either `cached_titles` or `no_subtitle_titles` (true for every historical no-subtitle title, and for pre-this-feature `cached_titles` rows missing their `source` value), it does one cheap local JSON read (no ffmpeg) to classify and backfill it — then never touches it again. This happens automatically the next time `library_sync` runs (scheduled, or a manual "Sync now" click) — no separate migration step, consistent with this project's existing aversion to manual migrations (`build_full_cache.py`'s incremental-by-default design).

## File Structure

**Create:**
- `app/web/dashboard.py` — `register_dashboard_routes()`: `/dashboard`, `POST /sync/run`, `GET /dashboard/sync-stream`, plus the `_coverage_stats()` helper.
- `app/web/templates/panel_dashboard.html` — main dashboard content (stat cards, coverage bar, sync panel container, the `EventSource` script).
- `app/web/templates/panel_dashboard_sync.html` — the live sync panel fragment, rendered both on initial page load and by every SSE update/`/sync/run` response.
- `tests/test_dashboard.py` — unit tests for `_coverage_stats()`.

**Modify:**
- `app/worker/quote_index.py` — schema (source column + 3 new tables) and CRUD functions.
- `app/worker/library_sync.py` — `sync_one_title`, `sync_library`, `run_library_sync_once`, plus a new `backfill_missing_source_values()`.
- `app/worker/api.py` — `_index_if_searchable` records source / no-subtitle state too.
- `app/runtime.py` — `SettingsHolder` gets a `plex_client` field.
- `app/main.py` — sets `settings_holder.plex_client`, calls the startup reset/backfill.
- `app/web/app.py` — registers dashboard routes, `/` now redirects to `/dashboard` instead of `/generate`.
- `app/web/templates/shell.html` — new "Dashboard" sidebar nav item.
- `app/web/static/style.css` — new dashboard-specific classes.
- `tests/test_quote_index.py` — updated for the `source` param/column.
- `tests/test_library_sync.py` — updated `_precache` helper, new progress/log/backfill tests.

---

### Task 1: `quote_index.py` — schema and CRUD for source/coverage/progress/log

**Files:**
- Modify: `app/worker/quote_index.py`
- Test: `tests/test_quote_index.py`

**Interfaces:**
- Produces (used by Task 2, Task 4, Task 5):
  - `upsert_cached_title(db_path, guid, rating_key, title, library_name, source: str) -> None` (signature changed — `source` is now a required 6th positional/keyword arg)
  - `CachedTitle` gains a `source: str = ""` field
  - `has_cached_title(db_path, guid) -> bool`
  - `list_cached_titles_missing_source(db_path) -> list[CachedTitle]`
  - `set_cached_title_source(db_path, guid, source) -> None`
  - `upsert_no_subtitle_title(db_path, guid, rating_key, title, library_name) -> None`
  - `is_no_subtitle_title(db_path, guid) -> bool`
  - `@dataclass(frozen=True) class LibraryCoverage: sidecar_count: int; embedded_count: int; no_subtitle_count: int`
  - `library_coverage(db_path, library_name) -> LibraryCoverage`
  - `@dataclass(frozen=True) class SyncProgress: status: str; current_library: str | None; current_title: str | None; processed: int; total: int; started_at: str | None; last_synced_at: str | None; last_run_new_count: int`
  - `get_sync_progress(db_path) -> SyncProgress`
  - `start_sync_run(db_path) -> bool` (atomic idle→running flip; `False` if already running)
  - `update_sync_progress(db_path, current_library, current_title, processed, total) -> None`
  - `finish_sync_run(db_path, new_count) -> None`
  - `reset_stale_running_status(db_path) -> None`
  - `@dataclass(frozen=True) class SyncLogLine: seq: int; ts: str; message: str`
  - `append_sync_log(db_path, message) -> None` (trims to last 50)
  - `tail_sync_log(db_path, limit: int = 50) -> list[SyncLogLine]`
  - `latest_sync_log_seq(db_path) -> int`

- [ ] **Step 1: Add the schema changes to `_connect()`**

Add inside the `try:` block of `_connect()` in `app/worker/quote_index.py`, right after the existing `library_sync_state` table creation:

```python
        # Which subtitle source a cached title actually came from
        # (SubtitleSource.value — "sidecar"/"embedded") — added after
        # cached_titles already existed on real installs, so it's a
        # guarded ALTER rather than part of the CREATE TABLE above.
        existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(cached_titles)")}
        if "source" not in existing_cols:
            conn.execute("ALTER TABLE cached_titles ADD COLUMN source TEXT")

        # The negative case cached_titles never recorded: a title that was
        # checked and found to have neither a sidecar nor an embedded
        # subtitle track. Mirrors cached_titles's shape.
        conn.execute(
            "CREATE TABLE IF NOT EXISTS no_subtitle_titles ("
            "guid TEXT PRIMARY KEY, "
            "rating_key INTEGER NOT NULL, "
            "title TEXT NOT NULL, "
            "library_name TEXT NOT NULL, "
            "checked_at TEXT NOT NULL"
            ")"
        )

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
```

- [ ] **Step 2: Update `CachedTitle` and `upsert_cached_title`/`list_cached_titles*`**

Replace the `CachedTitle` dataclass:

```python
@dataclass(frozen=True)
class CachedTitle:
    guid: str
    rating_key: int
    title: str
    library_name: str
    source: str = ""
```

Replace `upsert_cached_title`:

```python
def upsert_cached_title(
    db_path: Path, guid: str, rating_key: int, title: str, library_name: str, source: str
) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT INTO cached_titles (guid, rating_key, title, library_name, cached_at, source) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(guid) DO UPDATE SET "
            "rating_key=excluded.rating_key, "
            "title=excluded.title, "
            "library_name=excluded.library_name, "
            "cached_at=excluded.cached_at, "
            "source=excluded.source",
            (guid, rating_key, title, library_name, datetime.now(timezone.utc).isoformat(), source),
        )
```

Replace `list_cached_titles` and `list_cached_titles_for_library` to select/return `source`:

```python
def list_cached_titles(db_path: Path) -> list[CachedTitle]:
    if not db_path.exists():
        return []
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT guid, rating_key, title, library_name, source FROM cached_titles"
        ).fetchall()
    return [
        CachedTitle(guid=r[0], rating_key=r[1], title=r[2], library_name=r[3], source=r[4] or "")
        for r in rows
    ]


def list_cached_titles_for_library(db_path: Path, library_name: str) -> list[CachedTitle]:
    if not db_path.exists():
        return []
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT guid, rating_key, title, library_name, source FROM cached_titles "
            "WHERE library_name = ?",
            (library_name,),
        ).fetchall()
    return [
        CachedTitle(guid=r[0], rating_key=r[1], title=r[2], library_name=r[3], source=r[4] or "")
        for r in rows
    ]
```

- [ ] **Step 3: Update existing tests in `tests/test_quote_index.py` for the new `source` param**

Replace the whole file's `upsert_cached_title` calls and equality assertions:

```python
def test_upsert_and_list_round_trip(tmp_path):
    db_path = tmp_path / "quote_index.db"
    upsert_cached_title(db_path, "guid-1", 101, "Film One", "Movies", "sidecar")
    upsert_cached_title(db_path, "guid-2", 102, "Film Two", "3D", "embedded")

    titles = list_cached_titles(db_path)

    assert set(titles) == {
        CachedTitle(guid="guid-1", rating_key=101, title="Film One", library_name="Movies", source="sidecar"),
        CachedTitle(guid="guid-2", rating_key=102, title="Film Two", library_name="3D", source="embedded"),
    }


def test_upsert_overwrites_existing_guid(tmp_path):
    db_path = tmp_path / "quote_index.db"
    upsert_cached_title(db_path, "guid-1", 101, "Old Title", "Movies", "sidecar")
    upsert_cached_title(db_path, "guid-1", 101, "New Title", "Movies", "embedded")

    titles = list_cached_titles(db_path)

    assert len(titles) == 1
    assert titles[0].title == "New Title"
    assert titles[0].source == "embedded"


def test_list_cached_titles_for_library_filters_correctly(tmp_path):
    db_path = tmp_path / "quote_index.db"
    upsert_cached_title(db_path, "guid-1", 101, "Film One", "Movies", "sidecar")
    upsert_cached_title(db_path, "guid-2", 102, "Film Two", "3D", "sidecar")

    titles = list_cached_titles_for_library(db_path, "Movies")

    assert [t.guid for t in titles] == ["guid-1"]


def test_remove_cached_title_deletes_the_right_row(tmp_path):
    db_path = tmp_path / "quote_index.db"
    upsert_cached_title(db_path, "guid-1", 101, "Film One", "Movies", "sidecar")
    upsert_cached_title(db_path, "guid-2", 102, "Film Two", "Movies", "sidecar")

    remove_cached_title(db_path, "guid-1")

    assert [t.guid for t in list_cached_titles(db_path)] == ["guid-2"]


def test_remove_cached_title_is_a_noop_for_unknown_guid(tmp_path):
    db_path = tmp_path / "quote_index.db"
    upsert_cached_title(db_path, "guid-1", 101, "Film One", "Movies", "sidecar")

    remove_cached_title(db_path, "guid-never-existed")

    assert [t.guid for t in list_cached_titles(db_path)] == ["guid-1"]
```

(Leave every other existing test in the file — `test_list_cached_titles_on_missing_db_returns_empty`, the `section_updated_at` tests — untouched; they don't call `upsert_cached_title`.)

- [ ] **Step 4: Run the updated tests, confirm they pass**

Run: `pytest tests/test_quote_index.py -v`
Expected: all PASS.

- [ ] **Step 5: Add `has_cached_title`, `list_cached_titles_missing_source`, `set_cached_title_source`**

Append to `app/worker/quote_index.py`:

```python
def has_cached_title(db_path: Path, guid: str) -> bool:
    if not db_path.exists():
        return False
    with _connect(db_path) as conn:
        row = conn.execute("SELECT 1 FROM cached_titles WHERE guid = ?", (guid,)).fetchone()
    return row is not None


def list_cached_titles_missing_source(db_path: Path) -> list[CachedTitle]:
    if not db_path.exists():
        return []
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT guid, rating_key, title, library_name, source FROM cached_titles "
            "WHERE source IS NULL OR source = ''"
        ).fetchall()
    return [
        CachedTitle(guid=r[0], rating_key=r[1], title=r[2], library_name=r[3], source=r[4] or "")
        for r in rows
    ]


def set_cached_title_source(db_path: Path, guid: str, source: str) -> None:
    with _connect(db_path) as conn:
        conn.execute("UPDATE cached_titles SET source = ? WHERE guid = ?", (source, guid))
```

- [ ] **Step 6: Write failing tests for Step 5's functions**

Add to `tests/test_quote_index.py`:

```python
from app.worker.quote_index import (
    has_cached_title,
    list_cached_titles_missing_source,
    set_cached_title_source,
)


def test_has_cached_title_true_and_false(tmp_path):
    db_path = tmp_path / "quote_index.db"
    upsert_cached_title(db_path, "guid-1", 101, "Film One", "Movies", "sidecar")

    assert has_cached_title(db_path, "guid-1") is True
    assert has_cached_title(db_path, "guid-missing") is False


def test_has_cached_title_on_missing_db_returns_false(tmp_path):
    assert has_cached_title(tmp_path / "does-not-exist.db", "guid-1") is False


def test_list_cached_titles_missing_source_finds_null_source_rows(tmp_path):
    db_path = tmp_path / "quote_index.db"
    # Simulate a pre-migration row written before the source column existed.
    upsert_cached_title(db_path, "guid-1", 101, "Film One", "Movies", "sidecar")
    set_cached_title_source(db_path, "guid-1", "")

    missing = list_cached_titles_missing_source(db_path)

    assert [t.guid for t in missing] == ["guid-1"]


def test_set_cached_title_source_updates_in_place(tmp_path):
    db_path = tmp_path / "quote_index.db"
    upsert_cached_title(db_path, "guid-1", 101, "Film One", "Movies", "")

    set_cached_title_source(db_path, "guid-1", "embedded")

    assert list_cached_titles(db_path)[0].source == "embedded"
```

- [ ] **Step 7: Run tests, confirm pass**

Run: `pytest tests/test_quote_index.py -v`
Expected: all PASS.

- [ ] **Step 8: Add `upsert_no_subtitle_title` / `is_no_subtitle_title`**

Append to `app/worker/quote_index.py`:

```python
def upsert_no_subtitle_title(
    db_path: Path, guid: str, rating_key: int, title: str, library_name: str
) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT INTO no_subtitle_titles (guid, rating_key, title, library_name, checked_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(guid) DO UPDATE SET "
            "rating_key=excluded.rating_key, "
            "title=excluded.title, "
            "library_name=excluded.library_name, "
            "checked_at=excluded.checked_at",
            (guid, rating_key, title, library_name, datetime.now(timezone.utc).isoformat()),
        )


def is_no_subtitle_title(db_path: Path, guid: str) -> bool:
    if not db_path.exists():
        return False
    with _connect(db_path) as conn:
        row = conn.execute("SELECT 1 FROM no_subtitle_titles WHERE guid = ?", (guid,)).fetchone()
    return row is not None
```

- [ ] **Step 9: Write failing tests, then confirm pass**

```python
from app.worker.quote_index import is_no_subtitle_title, upsert_no_subtitle_title


def test_no_subtitle_title_round_trip(tmp_path):
    db_path = tmp_path / "quote_index.db"
    assert is_no_subtitle_title(db_path, "guid-1") is False

    upsert_no_subtitle_title(db_path, "guid-1", 101, "Film One", "Movies")

    assert is_no_subtitle_title(db_path, "guid-1") is True


def test_no_subtitle_title_upsert_overwrites(tmp_path):
    db_path = tmp_path / "quote_index.db"
    upsert_no_subtitle_title(db_path, "guid-1", 101, "Old Title", "Movies")
    upsert_no_subtitle_title(db_path, "guid-1", 101, "New Title", "Movies")

    # No exception on the second call is the behavior under test here —
    # ON CONFLICT DO UPDATE, not a duplicate-key error.
    assert is_no_subtitle_title(db_path, "guid-1") is True
```

Run: `pytest tests/test_quote_index.py -v` — Expected: all PASS.

- [ ] **Step 10: Add `LibraryCoverage` / `library_coverage`**

Append:

```python
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
```

- [ ] **Step 11: Write failing test, then confirm pass**

```python
from app.worker.quote_index import LibraryCoverage, library_coverage


def test_library_coverage_counts_by_source_and_no_subtitle(tmp_path):
    db_path = tmp_path / "quote_index.db"
    upsert_cached_title(db_path, "guid-1", 101, "Film One", "Movies", "sidecar")
    upsert_cached_title(db_path, "guid-2", 102, "Film Two", "Movies", "sidecar")
    upsert_cached_title(db_path, "guid-3", 103, "Film Three", "Movies", "embedded")
    upsert_no_subtitle_title(db_path, "guid-4", 104, "Film Four", "Movies")
    upsert_cached_title(db_path, "guid-5", 105, "Other Library Film", "3D", "sidecar")

    coverage = library_coverage(db_path, "Movies")

    assert coverage == LibraryCoverage(sidecar_count=2, embedded_count=1, no_subtitle_count=1)


def test_library_coverage_on_missing_db_returns_zeros(tmp_path):
    assert library_coverage(tmp_path / "does-not-exist.db", "Movies") == LibraryCoverage(0, 0, 0)
```

Run: `pytest tests/test_quote_index.py -v` — Expected: all PASS.

- [ ] **Step 12: Add `SyncProgress` and the progress functions**

Append:

```python
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


def get_sync_progress(db_path: Path) -> SyncProgress:
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT status, current_library, current_title, processed, total, "
            "started_at, last_synced_at, last_run_new_count "
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


def finish_sync_run(db_path: Path, new_count: int) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE library_sync_progress SET status='idle', current_library=NULL, "
            "current_title=NULL, last_synced_at=?, last_run_new_count=? WHERE id=1",
            (datetime.now(timezone.utc).isoformat(), new_count),
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
```

- [ ] **Step 13: Write failing tests, then confirm pass**

```python
from app.worker.quote_index import (
    SyncProgress,
    finish_sync_run,
    get_sync_progress,
    reset_stale_running_status,
    start_sync_run,
    update_sync_progress,
)


def test_sync_progress_seeded_idle_zero_state(tmp_path):
    db_path = tmp_path / "quote_index.db"
    progress = get_sync_progress(db_path)

    assert progress.status == "idle"
    assert progress.processed == 0
    assert progress.total == 0
    assert progress.last_synced_at is None


def test_start_sync_run_succeeds_when_idle(tmp_path):
    db_path = tmp_path / "quote_index.db"
    assert start_sync_run(db_path) is True
    assert get_sync_progress(db_path).status == "running"


def test_start_sync_run_fails_when_already_running(tmp_path):
    db_path = tmp_path / "quote_index.db"
    assert start_sync_run(db_path) is True
    assert start_sync_run(db_path) is False


def test_update_and_finish_sync_run(tmp_path):
    db_path = tmp_path / "quote_index.db"
    start_sync_run(db_path)
    update_sync_progress(db_path, "Movies", "Some Film", 3, 10)

    mid = get_sync_progress(db_path)
    assert mid.current_library == "Movies"
    assert mid.current_title == "Some Film"
    assert mid.processed == 3
    assert mid.total == 10

    finish_sync_run(db_path, new_count=7)

    done = get_sync_progress(db_path)
    assert done.status == "idle"
    assert done.current_library is None
    assert done.last_run_new_count == 7
    assert done.last_synced_at is not None

    # A subsequent run can start again now that status is back to idle.
    assert start_sync_run(db_path) is True


def test_reset_stale_running_status(tmp_path):
    db_path = tmp_path / "quote_index.db"
    start_sync_run(db_path)
    assert get_sync_progress(db_path).status == "running"

    reset_stale_running_status(db_path)

    assert get_sync_progress(db_path).status == "idle"


def test_reset_stale_running_status_on_missing_db_is_a_noop(tmp_path):
    reset_stale_running_status(tmp_path / "does-not-exist.db")  # must not raise
```

Run: `pytest tests/test_quote_index.py -v` — Expected: all PASS.

- [ ] **Step 14: Add `SyncLogLine` and the log functions**

Append:

```python
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


def latest_sync_log_seq(db_path: Path) -> int:
    if not db_path.exists():
        return 0
    with _connect(db_path) as conn:
        row = conn.execute("SELECT MAX(seq) FROM library_sync_log").fetchone()
    return row[0] or 0
```

- [ ] **Step 15: Write failing tests, then confirm pass**

```python
from app.worker.quote_index import append_sync_log, latest_sync_log_seq, tail_sync_log


def test_append_and_tail_sync_log_preserves_order(tmp_path):
    db_path = tmp_path / "quote_index.db"
    append_sync_log(db_path, "first")
    append_sync_log(db_path, "second")
    append_sync_log(db_path, "third")

    lines = tail_sync_log(db_path)

    assert [line.message for line in lines] == ["first", "second", "third"]


def test_append_sync_log_trims_to_50(tmp_path):
    db_path = tmp_path / "quote_index.db"
    for i in range(60):
        append_sync_log(db_path, f"line-{i}")

    lines = tail_sync_log(db_path, limit=100)

    assert len(lines) == 50
    assert lines[0].message == "line-10"
    assert lines[-1].message == "line-59"


def test_tail_sync_log_on_missing_db_returns_empty(tmp_path):
    assert tail_sync_log(tmp_path / "does-not-exist.db") == []


def test_latest_sync_log_seq_tracks_inserts(tmp_path):
    db_path = tmp_path / "quote_index.db"
    assert latest_sync_log_seq(db_path) == 0

    append_sync_log(db_path, "one")
    first_seq = latest_sync_log_seq(db_path)
    assert first_seq >= 1

    append_sync_log(db_path, "two")
    assert latest_sync_log_seq(db_path) == first_seq + 1
```

Run: `pytest tests/test_quote_index.py -v` — Expected: all PASS.

- [ ] **Step 16: Commit**

```bash
git add app/worker/quote_index.py tests/test_quote_index.py
git commit -m "Add source/coverage/sync-progress/sync-log tracking to quote_index.db"
```

---

### Task 2: `library_sync.py` — write the new state, self-healing backfill, concurrency guard

**Files:**
- Modify: `app/worker/library_sync.py`
- Test: `tests/test_library_sync.py`

**Interfaces:**
- Consumes: everything produced in Task 1 (`quote_index.upsert_cached_title(..., source)`, `upsert_no_subtitle_title`, `has_cached_title`, `is_no_subtitle_title`, `start_sync_run`, `update_sync_progress`, `finish_sync_run`, `append_sync_log`), and `subtitles.read_cached_subtitles(cache_dir, guid)`.
- Produces (used by Task 3): `backfill_missing_source_values(settings) -> int`.

- [ ] **Step 1: Update `sync_one_title`'s skip check and result recording**

Replace the whole function body in `app/worker/library_sync.py`:

```python
async def sync_one_title(settings: Settings, item: MovieResult, *, force: bool = False) -> str:
    """Extracted from scripts/build_full_cache.py's process_one() — the one
    shared implementation for both the manual script and the automatic
    sync task. Skips a title entirely (bare file-existence check, no read/
    parse) unless forced, so re-running this against an already-synced
    library costs almost nothing — UNLESS the cache file exists but isn't
    indexed yet (a title cached before source/no-subtitle tracking existed,
    or a title only ever touched via /snip rather than a sync). That case
    is self-healing: one cheap local JSON read (no ffmpeg) classifies and
    backfills it, and it's never revisited after that.
    """
    db_path = settings.quote_index_db_path
    cache_file_exists = cache_path_for_guid(settings.cache_dir, item.guid).exists()

    if not force and cache_file_exists:
        already_indexed = has_cached_title(db_path, item.guid) or is_no_subtitle_title(db_path, item.guid)
        if already_indexed:
            return f"CACHED (already have it): {item.title}"

        cached = read_cached_subtitles(settings.cache_dir, item.guid)
        if cached is not None:
            if cached.source is SubtitleSource.NONE:
                upsert_no_subtitle_title(db_path, item.guid, item.rating_key, item.title, item.library_name)
            else:
                upsert_cached_title(
                    db_path, item.guid, item.rating_key, item.title, item.library_name, cached.source.value
                )
                get_or_build_candidates(
                    settings.cache_dir, cached.guid, cached.entries, settings.quote_match.max_window_gap_seconds
                )
            return f"CACHED (backfilled index): {item.title}"

    try:
        container_path = resolve_container_path(
            item.plex_path, settings.path_mappings_for(item.library_name)
        )
    except NoPathMappingError as exc:
        return f"SKIP (no path mapping): {item.title}: {exc}"

    if not os.path.exists(container_path):
        return f"SKIP (file not found on disk): {item.title}"

    timeout = settings.subtitle_defaults.extraction_timeout_seconds
    try:
        result = await get_subtitles(
            item,
            container_path,
            settings.cache_dir,
            ffprobe_timeout=timeout,
            ffmpeg_timeout=timeout,
        )
    except Exception as exc:  # noqa: BLE001 - one bad file must not stop the run
        return f"ERROR: {item.title}: {exc}"

    if result.source is SubtitleSource.NONE:
        upsert_no_subtitle_title(db_path, result.guid, item.rating_key, item.title, item.library_name)
    elif result.entries:
        upsert_cached_title(
            db_path, result.guid, item.rating_key, item.title, item.library_name, result.source.value
        )
        get_or_build_candidates(
            settings.cache_dir, result.guid, result.entries, settings.quote_match.max_window_gap_seconds
        )

    return f"OK ({result.source.value}, {len(result.entries)} entries): {item.title}"
```

- [ ] **Step 2: Update imports**

Replace the existing import block at the top of `app/worker/library_sync.py`:

```python
from app.worker.quote_index import (
    append_sync_log,
    finish_sync_run,
    get_section_updated_at,
    has_cached_title,
    is_no_subtitle_title,
    list_cached_titles_for_library,
    remove_cached_title,
    set_section_updated_at,
    start_sync_run,
    update_sync_progress,
    upsert_cached_title,
    upsert_no_subtitle_title,
)
from app.worker.subtitles import (
    SubtitleSource,
    cache_path_for_guid,
    delete_cached_subtitles,
    get_subtitles,
    read_cached_subtitles,
)
```

(Drop the old `from app.worker import quote_index` module-level import and the bare `from app.worker.subtitles import (SubtitleSource, cache_path_for_guid, delete_cached_subtitles, get_subtitles)` — replace every remaining `quote_index.xxx(...)` call elsewhere in the file with the now-directly-imported name, e.g. `quote_index.list_cached_titles_for_library(...)` → `list_cached_titles_for_library(...)`, `quote_index.remove_cached_title(...)` → `remove_cached_title(...)`, `quote_index.set_section_updated_at(...)` → `set_section_updated_at(...)`, `quote_index.get_section_updated_at(...)` → `get_section_updated_at(...)`.)

- [ ] **Step 3: Update `tests/test_library_sync.py`'s `_precache` helper for the new `upsert_cached_title` signature**

```python
def _precache(settings: Settings, guid: str, library_name: str = "Movies") -> None:
    write_cached_subtitles(
        settings.cache_dir,
        SubtitleResult(
            guid=guid,
            source=SubtitleSource.SIDECAR,
            entries=[SubtitleEntry(index=1, start=0.0, end=1.0, text="Hi")],
        ),
    )
    quote_index.upsert_cached_title(settings.quote_index_db_path, guid, 1, "Film", library_name, "sidecar")
```

- [ ] **Step 4: Run the existing test_library_sync.py suite, confirm still green**

Run: `pytest tests/test_library_sync.py -v`
Expected: all PASS (this only exercises `sync_library`/`_mount_check`, which don't call `sync_one_title` directly in most of these tests — they precache via the helper above; anything that does now reads `cached` back via the new path, but with the sidecar entry already written the branch taken is the "CACHED (already have it)" fast path exactly as before, since `has_cached_title` will now find the precached row).

- [ ] **Step 5: Write failing tests for `sync_one_title`'s new behavior**

Add to `tests/test_library_sync.py`:

```python
import asyncio as _asyncio  # already imported as asyncio at top of file — reuse that import, don't add a second one

from app.worker.library_sync import sync_one_title
from app.worker.quote_index import has_cached_title, is_no_subtitle_title, library_coverage


def test_sync_one_title_records_no_subtitle_titles(tmp_path):
    settings = _settings(tmp_path)
    item = _item("guid-1", 101)

    # No sidecar, no path mapping matches a real file — the extraction path
    # naturally can't find anything and falls through to SubtitleSource.NONE
    # once ffprobe/ffmpeg see a genuinely nonexistent/unmapped file... but
    # to keep this test hermetic (no real ffmpeg/ffprobe process), precache
    # a NONE result directly instead of exercising get_subtitles().
    from app.worker.subtitles import SubtitleResult, SubtitleSource, write_cached_subtitles
    write_cached_subtitles(settings.cache_dir, SubtitleResult(guid="guid-1", source=SubtitleSource.NONE, entries=[]))

    outcome = asyncio.run(sync_one_title(settings, item))

    assert outcome.startswith("CACHED (backfilled index)")
    assert is_no_subtitle_title(settings.quote_index_db_path, "guid-1") is True
    assert has_cached_title(settings.quote_index_db_path, "guid-1") is False


def test_sync_one_title_backfills_legacy_cached_title_missing_from_index(tmp_path):
    settings = _settings(tmp_path)
    item = _item("guid-1", 101, title="Film One")

    from app.worker.subtitles import SubtitleEntry, SubtitleResult, SubtitleSource, write_cached_subtitles
    write_cached_subtitles(
        settings.cache_dir,
        SubtitleResult(
            guid="guid-1", source=SubtitleSource.SIDECAR,
            entries=[SubtitleEntry(index=1, start=0.0, end=1.0, text="Hi")],
        ),
    )
    # Deliberately NOT calling quote_index.upsert_cached_title — simulates
    # a cache file from before source/no-subtitle tracking existed.

    outcome = asyncio.run(sync_one_title(settings, item))

    assert outcome.startswith("CACHED (backfilled index)")
    assert has_cached_title(settings.quote_index_db_path, "guid-1") is True
    coverage = library_coverage(settings.quote_index_db_path, "Movies")
    assert coverage.sidecar_count == 1


def test_sync_one_title_skips_already_indexed_no_subtitle_title(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    item = _item("guid-1", 101)

    from app.worker.subtitles import SubtitleResult, SubtitleSource, write_cached_subtitles
    write_cached_subtitles(settings.cache_dir, SubtitleResult(guid="guid-1", source=SubtitleSource.NONE, entries=[]))
    quote_index.upsert_no_subtitle_title(settings.quote_index_db_path, "guid-1", 101, "Film", "Movies")

    def _boom(*args, **kwargs):
        raise AssertionError("read_cached_subtitles should not be called for an already-indexed title")
    monkeypatch.setattr("app.worker.library_sync.read_cached_subtitles", _boom)

    outcome = asyncio.run(sync_one_title(settings, item))

    assert outcome.startswith("CACHED (already have it)")
```

- [ ] **Step 6: Run tests, confirm pass**

Run: `pytest tests/test_library_sync.py -v` — Expected: all PASS.

- [ ] **Step 7: Add progress/log writes to `sync_library`**

Replace the `for item in live_items:` loop in `sync_library`:

```python
    total_items = len(live_items)
    append_sync_log(
        settings.quote_index_db_path, f"Checking library: {library_name} — {total_items} items"
    )

    for index, item in enumerate(live_items, start=1):
        update_sync_progress(settings.quote_index_db_path, library_name, item.title, index - 1, total_items)
        outcome = await sync_one_title(settings, item)
        if outcome.startswith("CACHED"):
            result.already_cached += 1
        elif outcome.startswith("OK"):
            result.added += 1
            append_sync_log(settings.quote_index_db_path, f"Extracted subtitles — {item.title}")
        elif outcome.startswith("SKIP (no path mapping"):
            result.skipped_no_mapping += 1
        elif outcome.startswith("SKIP (file not found"):
            result.skipped_missing_file += 1
        elif outcome.startswith("ERROR"):
            result.errors += 1
            logger.warning("library sync: %s", outcome)
            append_sync_log(settings.quote_index_db_path, f"Error — {item.title}")
        update_sync_progress(settings.quote_index_db_path, library_name, item.title, index, total_items)
```

(This replaces the existing loop body one-for-one — the `if outcome.startswith(...)` branches and their bodies are unchanged except for the two new `append_sync_log` calls; everything after the loop, i.e. the removal-candidate logic, is untouched.)

- [ ] **Step 8: Write a failing test for the progress writes, then confirm pass**

Add to `tests/test_library_sync.py`:

```python
from app.worker.quote_index import get_sync_progress


def test_sync_library_writes_progress_per_item(tmp_path):
    settings = _settings(tmp_path)
    plex = _FakePlex(items=[_item("guid-1", 101, title="Film One"), _item("guid-2", 102, title="Film Two")])
    _precache(settings, "guid-1")
    _precache(settings, "guid-2")

    asyncio.run(sync_library(settings, plex, "Movies", section=None, updated_at=200))

    progress = get_sync_progress(settings.quote_index_db_path)
    # sync_library doesn't flip status itself (run_library_sync_once owns
    # that, Step 9 below) — this test only checks the per-item counters
    # landed correctly by the time the loop finished.
    assert progress.processed == 2
    assert progress.total == 2
    assert progress.current_title == "Film Two"
```

Run: `pytest tests/test_library_sync.py -v` — Expected: PASS.

- [ ] **Step 9: Add the concurrency guard and status lifecycle to `run_library_sync_once`**

Replace the whole function:

```python
async def run_library_sync_once(settings: Settings, plex: PlexClient) -> list[LibrarySyncResult]:
    db_path = settings.quote_index_db_path
    if not start_sync_run(db_path):
        logger.info("library sync: skipped — a run is already in progress")
        return []

    results: list[LibrarySyncResult] = []
    try:
        try:
            current = await asyncio.to_thread(plex.current_section_updated_ats)
        except Exception as exc:  # noqa: BLE001 - Plex being briefly unreachable must not crash the sync loop
            logger.warning("library sync: could not check for library changes — Plex unreachable: %s", exc)
            return []

        sections_by_name = dict(plex.library_sections())

        for library_name, updated_at in current.items():
            stored = get_section_updated_at(db_path, library_name)
            if stored == updated_at:
                continue

            section = sections_by_name[library_name]
            result = await sync_library(settings, plex, library_name, section, updated_at)
            results.append(result)
            skip_note = f" (cleanup skipped: {result.removal_skipped_reason})" if result.removal_skipped_reason else ""
            logger.info(
                "library sync: '%s' changed — added=%d already_cached=%d removed=%d errors=%d%s",
                library_name,
                result.added,
                result.already_cached,
                result.removed,
                result.errors,
                skip_note,
            )

        if not results:
            logger.info("library sync: no changes (%d libraries checked)", len(current))

        return results
    finally:
        finish_sync_run(db_path, new_count=sum(r.added for r in results))
```

- [ ] **Step 10: Write failing tests for the guard, then confirm pass**

```python
from app.worker.library_sync import run_library_sync_once
from app.worker.quote_index import start_sync_run


def test_run_library_sync_once_skips_when_already_running(tmp_path):
    settings = _settings(tmp_path)
    plex = _FakePlex()
    start_sync_run(settings.quote_index_db_path)  # simulate a run already in progress

    results = asyncio.run(run_library_sync_once(settings, plex))

    assert results == []


def test_run_library_sync_once_resets_status_to_idle_on_completion(tmp_path):
    settings = _settings(tmp_path)

    class _PlexWithSections(_FakePlex):
        def current_section_updated_ats(self):
            return {"Movies": 999}

        def library_sections(self):
            return [("Movies", object())]

    plex = _PlexWithSections(items=[])
    asyncio.run(run_library_sync_once(settings, plex))

    from app.worker.quote_index import get_sync_progress
    assert get_sync_progress(settings.quote_index_db_path).status == "idle"


def test_run_library_sync_once_resets_status_even_on_plex_error(tmp_path):
    settings = _settings(tmp_path)

    class _RaisingPlex:
        def current_section_updated_ats(self):
            raise ConnectionError("plex unreachable")

    asyncio.run(run_library_sync_once(settings, _RaisingPlex()))

    from app.worker.quote_index import get_sync_progress
    assert get_sync_progress(settings.quote_index_db_path).status == "idle"
```

(`_FakePlex.current_section_updated_ats`/`library_sections` don't exist on the base fixture — the second test defines its own small subclass rather than growing `_FakePlex`'s constructor further, matching this file's existing pattern of small purpose-built fakes per test where needed.)

Run: `pytest tests/test_library_sync.py -v` — Expected: all PASS.

- [ ] **Step 11: Add `backfill_missing_source_values`**

Append to `app/worker/library_sync.py`:

```python
def backfill_missing_source_values(settings: Settings) -> int:
    """One-time-per-install backfill for cached_titles rows written before
    the `source` column existed (real installs can have thousands of
    these). Only touches rows still missing it, so it's a cheap no-op on
    every startup after the first. Called from app/main.py at startup.
    Returns how many rows were backfilled, for a startup log line.
    """
    db_path = settings.quote_index_db_path
    backfilled = 0
    for cached in list_cached_titles_missing_source(db_path):
        cache_file = cache_path_for_guid(settings.cache_dir, cached.guid)
        if not cache_file.exists():
            continue
        try:
            payload = json.loads(cache_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        source = payload.get("source")
        if source in ("sidecar", "embedded"):
            set_cached_title_source(db_path, cached.guid, source)
            backfilled += 1
    return backfilled
```

Add `import json` and `list_cached_titles_missing_source`, `set_cached_title_source` to the `quote_index` import block from Step 2.

- [ ] **Step 12: Write failing tests, then confirm pass**

```python
from app.worker.library_sync import backfill_missing_source_values
from app.worker.quote_index import list_cached_titles_missing_source


def test_backfill_missing_source_values_reads_from_cache_json(tmp_path):
    settings = _settings(tmp_path)
    from app.worker.subtitles import SubtitleEntry, SubtitleResult, SubtitleSource, write_cached_subtitles
    write_cached_subtitles(
        settings.cache_dir,
        SubtitleResult(
            guid="guid-1", source=SubtitleSource.EMBEDDED,
            entries=[SubtitleEntry(index=1, start=0.0, end=1.0, text="Hi")],
        ),
    )
    quote_index.upsert_cached_title(settings.quote_index_db_path, "guid-1", 101, "Film", "Movies", "")

    count = backfill_missing_source_values(settings)

    assert count == 1
    assert list_cached_titles_missing_source(settings.quote_index_db_path) == []
    coverage = quote_index.library_coverage(settings.quote_index_db_path, "Movies")
    assert coverage.embedded_count == 1


def test_backfill_missing_source_values_skips_rows_with_no_cache_file(tmp_path):
    settings = _settings(tmp_path)
    quote_index.upsert_cached_title(settings.quote_index_db_path, "guid-orphan", 101, "Film", "Movies", "")

    count = backfill_missing_source_values(settings)

    assert count == 0


def test_backfill_missing_source_values_is_idempotent(tmp_path):
    settings = _settings(tmp_path)
    from app.worker.subtitles import SubtitleEntry, SubtitleResult, SubtitleSource, write_cached_subtitles
    write_cached_subtitles(
        settings.cache_dir,
        SubtitleResult(
            guid="guid-1", source=SubtitleSource.SIDECAR,
            entries=[SubtitleEntry(index=1, start=0.0, end=1.0, text="Hi")],
        ),
    )
    quote_index.upsert_cached_title(settings.quote_index_db_path, "guid-1", 101, "Film", "Movies", "")

    backfill_missing_source_values(settings)
    second_run_count = backfill_missing_source_values(settings)

    assert second_run_count == 0
```

Run: `pytest tests/test_library_sync.py -v` — Expected: all PASS.

- [ ] **Step 13: Commit**

```bash
git add app/worker/library_sync.py tests/test_library_sync.py
git commit -m "Track subtitle source/no-subtitle state and live progress in library_sync"
```

---

### Task 3: `app/runtime.py` + `app/main.py` — wire Plex access and startup housekeeping into the web app

**Files:**
- Modify: `app/runtime.py`
- Modify: `app/main.py`

**Interfaces:**
- Consumes: `library_sync.backfill_missing_source_values`, `quote_index.reset_stale_running_status` (Task 1/2).
- Produces (used by Task 5): `SettingsHolder.plex_client: PlexClient | None`.

- [ ] **Step 1: Add `plex_client` to `SettingsHolder`**

In `app/runtime.py`, add the import and field:

```python
from app.worker.plex_client import PlexClient
```

```python
    settings: Settings | None = None
    bot: CineSnipBot | None = None
    plex_client: PlexClient | None = None
```

(Add a docstring paragraph mirroring the existing `bot` one: `` `plex_client` mirrors the same pattern for the worker's live PlexClient: main()'s loop sets it whenever the worker is (re)built, and the dashboard (app/web/dashboard.py) reads this field live rather than holding its own copy — same reasoning as `bot` above. ``)

- [ ] **Step 2: Set `plex_client` in `main.py`'s loop, and run startup housekeeping**

In `app/main.py`, add imports:

```python
from app.worker import quote_index
from app.worker.library_sync import backfill_missing_source_values
```

Right after `worker_app, worker_server, worker_task = await _start_worker(settings)` inside the `while True:` loop:

```python
            worker_app, worker_server, worker_task = await _start_worker(settings)
            settings_holder.plex_client = worker_app.state.plex
```

Right after `settings.cache_dir.mkdir(parents=True, exist_ok=True)` (same loop, a few lines above):

```python
            settings.cache_dir.mkdir(parents=True, exist_ok=True)
            quote_index.reset_stale_running_status(settings.quote_index_db_path)
            backfilled = await run_in_threadpool(backfill_missing_source_values, settings)
            if backfilled:
                logger.info("startup: backfilled source for %d previously-untyped cached titles", backfilled)
```

Add `from fastapi.concurrency import run_in_threadpool` to `app/main.py`'s imports (backfill does file I/O across potentially thousands of small JSON files — worth keeping off the event loop, same reasoning already used elsewhere in this codebase for Plex/file calls).

- [ ] **Step 3: Manual verification (no automated test — this is startup wiring)**

Run: `python -m app.main` (or however this project's dev startup is normally invoked — check README.md's "Running locally" section) against the real dev environment, and confirm in the logs:
- No traceback on startup.
- If this is a fresh run after Task 1/2 landed, a `startup: backfilled source for N previously-untyped cached titles` line appears (or is silently skipped if `N == 0`, which is also correct — nothing to log).

- [ ] **Step 4: Commit**

```bash
git add app/runtime.py app/main.py
git commit -m "Wire live PlexClient reference and startup housekeeping into the web app"
```

---

### Task 4: `app/worker/api.py` — record source/no-subtitle state from the normal `/snip` flow too

**Files:**
- Modify: `app/worker/api.py:172-181` (`_index_if_searchable`)
- Test: `tests/test_library_search.py` or a new small test in `tests/test_quote_index.py`-adjacent location — see Step 3.

**Interfaces:**
- Consumes: Task 1's `upsert_cached_title(..., source)`, `upsert_no_subtitle_title`.

- [ ] **Step 1: Update `_index_if_searchable`**

Replace `app/worker/api.py:172-181`:

```python
def _index_if_searchable(settings: Settings, movie: MovieResult, result: SubtitleResult) -> None:
    # Every title actually checked gets *some* record — a searchable
    # result goes into cached_titles (with its source type, for the
    # dashboard's coverage stats), a NONE result goes into
    # no_subtitle_titles instead (Section 5's documented gap: not
    # searchable, but still worth knowing "this was checked").
    if result.source is not SubtitleSource.NONE and result.entries:
        quote_index.upsert_cached_title(
            settings.quote_index_db_path,
            result.guid,
            movie.rating_key,
            movie.title,
            movie.library_name,
            result.source.value,
        )
    elif result.source is SubtitleSource.NONE:
        quote_index.upsert_no_subtitle_title(
            settings.quote_index_db_path,
            result.guid,
            movie.rating_key,
            movie.title,
            movie.library_name,
        )
```

- [ ] **Step 2: Find and update this function's existing test coverage**

Run: `grep -rn "_index_if_searchable" tests/` to find any existing direct test. If found, update its `upsert_cached_title` assertion/mock the same way Task 1 Step 3 updated `tests/test_quote_index.py` (add the `source` argument to whatever assertion checks the call). If no direct test exists (the function may currently only be covered indirectly via a `/render` or `/resolve-quote` integration test), add one:

```python
# In a suitable existing test file for app/worker/api.py, or a new
# tests/test_api_indexing.py if none of the existing files import from
# app.worker.api at module level (check first with:
#   grep -l "from app.worker.api\|from app.worker import api" tests/*.py
# and add alongside that file if one exists).

from app.worker.api import _index_if_searchable
from app.worker.quote_index import has_cached_title, is_no_subtitle_title, library_coverage
from app.worker.subtitles import SubtitleEntry, SubtitleResult, SubtitleSource
from app.settings import Settings


def _settings(tmp_path) -> Settings:
    return Settings(
        discord_token="x", plex_url="http://localhost", plex_token="x",
        cache_dir=tmp_path / "cache",
    )


def _movie(rating_key=101, title="Film", library_name="Movies"):
    from app.worker.plex_client import MovieResult
    return MovieResult(
        rating_key=rating_key, title=title, year=2000, duration_ms=1000,
        thumb_url=None, plex_path="D:\\Movies\\film.mkv", guid="guid-1", library_name=library_name,
    )


def test_index_if_searchable_records_source_for_a_real_match(tmp_path):
    settings = _settings(tmp_path)
    result = SubtitleResult(
        guid="guid-1", source=SubtitleSource.SIDECAR,
        entries=[SubtitleEntry(index=1, start=0.0, end=1.0, text="Hi")],
    )

    _index_if_searchable(settings, _movie(), result)

    assert has_cached_title(settings.quote_index_db_path, "guid-1") is True
    assert library_coverage(settings.quote_index_db_path, "Movies").sidecar_count == 1


def test_index_if_searchable_records_no_subtitle_titles(tmp_path):
    settings = _settings(tmp_path)
    result = SubtitleResult(guid="guid-1", source=SubtitleSource.NONE, entries=[])

    _index_if_searchable(settings, _movie(), result)

    assert is_no_subtitle_title(settings.quote_index_db_path, "guid-1") is True
    assert has_cached_title(settings.quote_index_db_path, "guid-1") is False
```

- [ ] **Step 3: Run tests, confirm pass**

Run: `pytest tests/ -k "index_if_searchable" -v` — Expected: PASS.
Then run the full suite once: `pytest tests/ -v` — Expected: all PASS (this confirms Tasks 1-4 haven't broken anything elsewhere in the worker).

- [ ] **Step 4: Commit**

```bash
git add app/worker/api.py tests/
git commit -m "Record subtitle source/no-subtitle state from the normal /snip flow too"
```

---

### Task 5: `app/web/dashboard.py` — routes and stats

**Files:**
- Create: `app/web/dashboard.py`
- Create: `tests/test_dashboard.py`

**Interfaces:**
- Consumes: `SettingsHolder.settings`, `SettingsHolder.plex_client` (Task 3); `quote_index.library_coverage`, `get_sync_progress`, `tail_sync_log`, `latest_sync_log_seq` (Task 1); `library_sync.run_library_sync_once` (Task 2).
- Produces (used by Task 6): `register_dashboard_routes(app, templates, settings_holder)`, `@dataclass class CoverageStats` with fields `cached_total, library_total, sidecar_count, embedded_count, no_subtitle_count, library_count`.

- [ ] **Step 1: Write `_coverage_stats` and its test first (TDD)**

Create `tests/test_dashboard.py`:

```python
from app.settings import LibraryConfig, Settings
from app.web.dashboard import CoverageStats, _coverage_stats
from app.runtime import SettingsHolder
from app.worker import quote_index


class _FakePlex:
    def __init__(self, section_counts: dict[str, int]):
        self._section_counts = section_counts

    def library_sections(self):
        return [(name, object()) for name in self._section_counts]

    def enumerate_section(self, section):
        # Tests never pass a real plexapi section object through — instead
        # they look the count up by identity isn't possible here, so this
        # fake ignores `section` and returns a list of that length for
        # whichever name was looked up last. See test below for how this
        # is actually driven via a per-name callable instead.
        raise NotImplementedError


def _settings(tmp_path, library_names: list[str]) -> Settings:
    return Settings(
        discord_token="x", plex_url="http://localhost", plex_token="x",
        cache_dir=tmp_path / "cache",
        libraries=[LibraryConfig(name=name) for name in library_names],
    )


class _FakePlexByName:
    """Keyed by section identity isn't practical for a fake — instead this
    fake's enumerate_section looks up counts via a small wrapper object
    library_sections() returns, so _coverage_stats' `sections.get(name)`
    -> `plex.enumerate_section(section)` round-trip works without needing
    real plexapi Section objects."""

    class _Section:
        def __init__(self, count: int):
            self.count = count

    def __init__(self, section_counts: dict[str, int]):
        self._counts = section_counts

    def library_sections(self):
        return [(name, self._Section(count)) for name, count in self._counts.items()]

    def enumerate_section(self, section):
        return [object()] * section.count


def test_coverage_stats_aggregates_across_libraries(tmp_path):
    settings = _settings(tmp_path, ["Movies", "3D"])
    quote_index.upsert_cached_title(settings.quote_index_db_path, "g1", 1, "F1", "Movies", "sidecar")
    quote_index.upsert_cached_title(settings.quote_index_db_path, "g2", 2, "F2", "Movies", "embedded")
    quote_index.upsert_no_subtitle_title(settings.quote_index_db_path, "g3", 3, "F3", "Movies")
    quote_index.upsert_cached_title(settings.quote_index_db_path, "g4", 4, "F4", "3D", "sidecar")

    holder = SettingsHolder(settings=settings, plex_client=_FakePlexByName({"Movies": 5, "3D": 2}))

    stats = _coverage_stats(holder)

    assert stats == CoverageStats(
        cached_total=3, library_total=7, sidecar_count=2, embedded_count=1,
        no_subtitle_count=1, library_count=2,
    )


def test_coverage_stats_handles_no_plex_client(tmp_path):
    settings = _settings(tmp_path, ["Movies"])
    holder = SettingsHolder(settings=settings, plex_client=None)

    stats = _coverage_stats(holder)

    assert stats.library_total == 0
    assert stats.library_count == 1


def test_coverage_stats_handles_no_libraries_configured(tmp_path):
    settings = _settings(tmp_path, [])
    holder = SettingsHolder(settings=settings, plex_client=_FakePlexByName({}))

    stats = _coverage_stats(holder)

    assert stats == CoverageStats(0, 0, 0, 0, 0, 0)
```

(Delete the unused `_FakePlex` class from the draft above — only `_FakePlexByName` is actually used; it's included in this step purely to explain why the simpler fake doesn't work, not to be left in the file.)

- [ ] **Step 2: Run the test, confirm it fails with `ModuleNotFoundError`/`ImportError`**

Run: `pytest tests/test_dashboard.py -v`
Expected: FAIL — `app.web.dashboard` doesn't exist yet.

- [ ] **Step 3: Create `app/web/dashboard.py` with `CoverageStats` and `_coverage_stats`**

```python
from __future__ import annotations

import asyncio
from dataclasses import dataclass

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from app.runtime import SettingsHolder
from app.worker import quote_index
from app.worker.library_sync import run_library_sync_once

# How often the SSE stream re-checks quote_index.db for a change. Progress
# is persisted there (not held in memory), so this is a poll-and-diff loop,
# not true push — the DB stays the single source of truth, meaning a page
# refresh or a second tab mid-sync always sees identical, correct state.
_SSE_POLL_INTERVAL_SECONDS = 1.0


@dataclass(frozen=True)
class CoverageStats:
    cached_total: int
    library_total: int
    sidecar_count: int
    embedded_count: int
    no_subtitle_count: int
    library_count: int


def _coverage_stats(settings_holder: SettingsHolder) -> CoverageStats:
    settings = settings_holder.settings
    plex = settings_holder.plex_client
    sections_by_name = dict(plex.library_sections()) if plex is not None else {}

    sidecar = embedded = no_subtitle = library_total = 0
    for library in settings.libraries:
        coverage = quote_index.library_coverage(settings.quote_index_db_path, library.name)
        sidecar += coverage.sidecar_count
        embedded += coverage.embedded_count
        no_subtitle += coverage.no_subtitle_count
        section = sections_by_name.get(library.name)
        if plex is not None and section is not None:
            library_total += len(plex.enumerate_section(section))

    return CoverageStats(
        cached_total=sidecar + embedded,
        library_total=library_total,
        sidecar_count=sidecar,
        embedded_count=embedded,
        no_subtitle_count=no_subtitle,
        library_count=len(settings.libraries),
    )
```

- [ ] **Step 4: Run the test, confirm it passes**

Run: `pytest tests/test_dashboard.py -v` — Expected: all PASS.

- [ ] **Step 5: Add the routes**

Append to `app/web/dashboard.py`:

```python
def _sync_panel_html(templates: Jinja2Templates, request: Request, settings_holder: SettingsHolder) -> str:
    settings = settings_holder.settings
    progress = quote_index.get_sync_progress(settings.quote_index_db_path)
    log_lines = quote_index.tail_sync_log(settings.quote_index_db_path)
    return templates.env.get_template("panel_dashboard_sync.html").render(
        {
            "request": request,
            "progress": progress,
            "log_lines": log_lines,
            "sync_enabled": settings.library_sync.enabled,
        }
    )


def register_dashboard_routes(app: FastAPI, templates: Jinja2Templates, settings_holder: SettingsHolder) -> None:
    @app.get("/dashboard", response_class=HTMLResponse)
    async def dashboard(request: Request):
        stats = await run_in_threadpool(_coverage_stats, settings_holder)
        settings = settings_holder.settings
        progress = quote_index.get_sync_progress(settings.quote_index_db_path)
        log_lines = quote_index.tail_sync_log(settings.quote_index_db_path)
        context = {
            "request": request,
            "stats": stats,
            "progress": progress,
            "log_lines": log_lines,
            "sync_enabled": settings.library_sync.enabled,
            "content_template": "panel_dashboard.html",
            "page_title": "Dashboard",
            "current_page": "dashboard",
        }
        return templates.TemplateResponse(request, "shell.html", context)

    @app.post("/sync/run", response_class=HTMLResponse)
    async def sync_run(request: Request):
        # start_sync_run's atomic guard (Task 1/2) makes this safe to call
        # unconditionally — if a run is already in progress (scheduled or a
        # previous manual click), run_library_sync_once no-ops immediately
        # rather than racing it, so no separate check is needed here.
        settings = settings_holder.settings
        plex = settings_holder.plex_client
        if plex is not None:
            asyncio.create_task(run_library_sync_once(settings, plex))
        return HTMLResponse(_sync_panel_html(templates, request, settings_holder))

    @app.get("/dashboard/sync-stream")
    async def dashboard_sync_stream(request: Request):
        async def event_source():
            last_payload = None
            while True:
                if await request.is_disconnected():
                    break
                settings = settings_holder.settings
                progress = quote_index.get_sync_progress(settings.quote_index_db_path)
                log_seq = quote_index.latest_sync_log_seq(settings.quote_index_db_path)
                payload = (progress.status, progress.current_title, progress.processed, progress.total, log_seq)
                if payload != last_payload:
                    last_payload = payload
                    html = _sync_panel_html(templates, request, settings_holder).replace("\n", "")
                    yield f"data: {html}\n\n"
                await asyncio.sleep(_SSE_POLL_INTERVAL_SECONDS)

        return StreamingResponse(event_source(), media_type="text/event-stream")
```

- [ ] **Step 6: Run the full test file, confirm pass**

Run: `pytest tests/test_dashboard.py -v` — Expected: all PASS (the route functions themselves aren't unit-tested here — Task 6 wires them into the real app, and end-to-end verification against the real dev environment in Task 8 is what actually exercises them, consistent with this project's standing rule to verify worker-layer changes against real data rather than relying only on synthetic route tests).

- [ ] **Step 7: Commit**

```bash
git add app/web/dashboard.py tests/test_dashboard.py
git commit -m "Add dashboard routes: stats page, manual sync trigger, SSE progress stream"
```

---

### Task 6: Templates and app wiring

**Files:**
- Create: `app/web/templates/panel_dashboard.html`
- Create: `app/web/templates/panel_dashboard_sync.html`
- Modify: `app/web/templates/shell.html`
- Modify: `app/web/app.py`

**Interfaces:**
- Consumes: `register_dashboard_routes` (Task 5); context keys `stats` (`CoverageStats`), `progress` (`SyncProgress`), `log_lines` (`list[SyncLogLine]`), `sync_enabled` (bool) as produced by Task 5's routes.

- [ ] **Step 1: Create `panel_dashboard_sync.html`**

```html
<div class="card sync-card">
  <div class="sync-header">
    <div>
      <div class="sync-title">Library sync</div>
      <div class="sync-subtitle">
        {% if progress.last_synced_at %}
          Last run {{ progress.last_synced_at }} &middot; found {{ progress.last_run_new_count }} new titles
        {% else %}
          Sync has never run
        {% endif %}
      </div>
    </div>
    <div class="sync-controls">
      {% if progress.status == 'running' %}
        <span class="sync-pill sync-pill-active"><span class="sync-pulse"></span>Syncing</span>
        <button class="btn btn-secondary" type="button" disabled>Sync now</button>
      {% else %}
        <span class="sync-pill">Idle</span>
        <button class="btn btn-secondary" type="button"
          hx-post="/sync/run" hx-target="#sync-panel-container" hx-swap="innerHTML">Sync now</button>
      {% endif %}
    </div>
  </div>

  {% if progress.status == 'running' %}
    {% set pct = ((100 * progress.processed / progress.total) | round(0, 'floor')) if progress.total else 0 %}
    <div class="sync-progress-track"><div class="sync-progress-fill" style="width: {{ pct }}%;"></div></div>
    <div class="sync-progress-label">
      <span>{{ progress.current_title or 'Checking libraries' }}{% if progress.current_library %} &middot; {{ progress.current_library }}{% endif %}</span>
      <span>{{ pct | int }}% &middot; {{ progress.processed }} of {{ progress.total }}</span>
    </div>
  {% endif %}

  {% if not sync_enabled %}
    <p class="footnote sync-disabled-note" style="text-align:left; max-width:none;">
      Automatic sync is off (<a href="/settings/cache">enable it in Settings</a>) &mdash; "Sync now" still runs a one-off check.
    </p>
  {% endif %}

  <div class="sync-log">
    {% for line in log_lines %}
      <div class="sync-log-line">[{{ line.ts }}] {{ line.message }}</div>
    {% else %}
      <div class="sync-log-line sync-log-empty">No activity yet.</div>
    {% endfor %}
  </div>
</div>
```

- [ ] **Step 2: Create `panel_dashboard.html`**

```html
<div id="dashboard-content">
  <div class="page-header">
    <h1>Library status</h1>
    <p>Subtitle cache across your {{ stats.library_count }} configured librar{{ 'y' if stats.library_count == 1 else 'ies' }}.</p>
  </div>

  <div class="stat-grid">
    <div class="stat-card">
      <div class="stat-label">TITLES CACHED</div>
      <div class="stat-value">{{ stats.cached_total }}<span class="stat-value-sub"> / {{ stats.library_total }}</span></div>
      {% set pct = ((100 * stats.cached_total / stats.library_total) | round(0, 'floor')) if stats.library_total else 0 %}
      <div class="stat-bar"><div class="stat-bar-fill" style="width: {{ pct }}%;"></div></div>
      <div class="stat-bar-label">{{ pct | int }}% complete</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">SIDECAR SUBS</div>
      <div class="stat-value">{{ stats.sidecar_count }}</div>
      <div class="stat-note">{{ ((100 * stats.sidecar_count / stats.library_total) | round(1)) if stats.library_total else 0 }}% of library</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">EMBEDDED SUBS</div>
      <div class="stat-value">{{ stats.embedded_count }}</div>
      <div class="stat-note">{{ ((100 * stats.embedded_count / stats.library_total) | round(1)) if stats.library_total else 0 }}% of library</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">NO SUBTITLES</div>
      <div class="stat-value stat-value-muted">{{ stats.no_subtitle_count }}</div>
      <div class="stat-note">timecode-only</div>
    </div>
  </div>

  <div class="card coverage-card">
    <div class="coverage-header">Coverage breakdown</div>
    {% set sidecar_pct = (100 * stats.sidecar_count / stats.library_total) if stats.library_total else 0 %}
    {% set embedded_pct = (100 * stats.embedded_count / stats.library_total) if stats.library_total else 0 %}
    {% set none_pct = (100 * stats.no_subtitle_count / stats.library_total) if stats.library_total else 0 %}
    <div class="coverage-bar">
      <div class="coverage-seg coverage-seg-sidecar" style="width: {{ sidecar_pct }}%;"></div>
      <div class="coverage-seg coverage-seg-embedded" style="width: {{ embedded_pct }}%;"></div>
      <div class="coverage-seg coverage-seg-none" style="width: {{ none_pct }}%;"></div>
    </div>
    <div class="coverage-legend">
      <span><i class="dot dot-sidecar"></i>Sidecar</span>
      <span><i class="dot dot-embedded"></i>Embedded</span>
      <span><i class="dot dot-none"></i>None</span>
    </div>
  </div>

  <div id="sync-panel-container">
    {% include "panel_dashboard_sync.html" %}
  </div>
</div>

<script>
  (function () {
    var container = document.getElementById('sync-panel-container');
    var source = new EventSource('/dashboard/sync-stream');
    source.onmessage = function (event) {
      container.innerHTML = event.data;
    };
    window.addEventListener('beforeunload', function () { source.close(); });
  })();
</script>
```

- [ ] **Step 3: Add the "Dashboard" nav item to `shell.html`**

In `app/web/templates/shell.html`, insert before the existing `<a href="/generate" ...>` link:

```html
      <a href="/dashboard" class="navitem {{ 'active' if current_page == 'dashboard' else '' }}">
        <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="7" height="9" rx="1.5"/><rect x="14" y="3" width="7" height="5" rx="1.5"/><rect x="14" y="12" width="7" height="9" rx="1.5"/><rect x="3" y="16" width="7" height="5" rx="1.5"/></svg>
        <span>Dashboard</span>
      </a>
```

- [ ] **Step 4: Register the routes and change `/`'s redirect in `app/web/app.py`**

Add the import near the other `register_*_routes` imports:

```python
from app.web.dashboard import register_dashboard_routes
```

Add the call alongside the existing two, in `create_web_app`:

```python
    register_generate_routes(app, templates, settings_holder)
    register_settings_routes(app, templates, settings_holder, on_setup_complete)
    register_dashboard_routes(app, templates, settings_holder)
```

Change the `index()` route's post-setup redirect (`app/web/app.py:359-367`):

```python
    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        if settings_holder.settings is None:
            state: WizardState = request.app.state.wizard
            step = state.current_step
            return RedirectResponse(
                {1: "/wizard/discord", 2: "/wizard/plex", 3: "/wizard/libraries", 4: "/wizard/validate"}[step]
            )
        return RedirectResponse("/dashboard")
```

- [ ] **Step 5: Run the full test suite**

Run: `pytest tests/ -v`
Expected: all PASS. (No new automated tests are added in this step — templates and route wiring are verified end-to-end in Task 8, consistent with how `/generate` and `/setup`'s own templates were verified per their commit history.)

- [ ] **Step 6: Commit**

```bash
git add app/web/templates/panel_dashboard.html app/web/templates/panel_dashboard_sync.html app/web/templates/shell.html app/web/app.py
git commit -m "Wire the dashboard page into the web app; make it the new default landing page"
```

---

### Task 7: CSS

**Files:**
- Modify: `app/web/static/style.css`

**Interfaces:**
- Consumes: class names referenced by `panel_dashboard.html`/`panel_dashboard_sync.html` (Task 6): `.stat-grid`, `.stat-card`, `.stat-label`, `.stat-value`, `.stat-value-sub`, `.stat-value-muted`, `.stat-bar`, `.stat-bar-fill`, `.stat-bar-label`, `.stat-note`, `.coverage-card`, `.coverage-header`, `.coverage-bar`, `.coverage-seg-sidecar`, `.coverage-seg-embedded`, `.coverage-seg-none`, `.coverage-legend`, `.dot`, `.dot-sidecar`, `.dot-embedded`, `.dot-none`, `.sync-card`, `.sync-header`, `.sync-title`, `.sync-subtitle`, `.sync-controls`, `.sync-pill`, `.sync-pill-active`, `.sync-pulse`, `.sync-progress-track`, `.sync-progress-fill`, `.sync-progress-label`, `.sync-disabled-note`, `.sync-log`, `.sync-log-line`, `.sync-log-empty`. Reuses existing `.card`, `.btn`, `.btn-secondary`, `.footnote`, `.page-header`.

- [ ] **Step 1: Add `--neutral-fill` to `:root`**

In `app/web/static/style.css`, add to the existing `:root { ... }` block (alongside `--danger-soft`):

```css
  --neutral-fill: oklch(0.42 0.008 250);
```

- [ ] **Step 2: Append the dashboard CSS block**

Append to the end of `app/web/static/style.css`:

```css
/* ---- Dashboard --------------------------------------------------------- */

.stat-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 16px;
  margin-top: 20px;
}
@media (max-width: 720px) {
  .stat-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
.stat-card {
  background: var(--surface);
  border: 1px solid var(--border-subtle);
  border-radius: 12px;
  padding: 20px;
}
.stat-label { font-size: 12px; color: var(--text-tertiary); font-weight: 500; margin-bottom: 10px; }
.stat-value { font-family: var(--font-display); font-size: 26px; font-weight: 700; }
.stat-value-sub { font-size: 14px; color: var(--text-tertiary); font-weight: 500; }
.stat-value-muted { color: var(--text-secondary); }
.stat-bar { margin-top: 12px; height: 5px; border-radius: 999px; background: var(--surface-2); overflow: hidden; }
.stat-bar-fill { height: 100%; background: var(--accent); border-radius: 999px; }
.stat-bar-label { margin-top: 7px; font-size: 12px; color: var(--accent); }
.stat-note { margin-top: 12px; font-size: 12px; color: var(--text-secondary); }

.coverage-card { margin-top: 20px; padding: 22px 24px; }
.coverage-header { font-size: 13.5px; font-weight: 600; margin-bottom: 14px; }
.coverage-bar { display: flex; height: 10px; border-radius: 999px; overflow: hidden; background: var(--surface-2); }
.coverage-seg-sidecar { background: var(--accent); }
.coverage-seg-embedded { background: var(--neutral-fill); }
.coverage-seg-none { background: var(--border); }
.coverage-legend { display: flex; gap: 22px; margin-top: 12px; flex-wrap: wrap; }
.coverage-legend span { display: flex; align-items: center; gap: 7px; font-size: 12px; color: var(--text-secondary); }
.dot { width: 8px; height: 8px; border-radius: 2px; display: inline-block; }
.dot-sidecar { background: var(--accent); }
.dot-embedded { background: var(--neutral-fill); }
.dot-none { background: var(--border); }

.sync-card { margin-top: 20px; padding: 24px; }
.sync-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 18px; flex-wrap: wrap; gap: 12px; }
.sync-title { font-size: 15px; font-weight: 600; margin-bottom: 3px; }
.sync-subtitle { font-size: 12.5px; color: var(--text-tertiary); }
.sync-controls { display: flex; align-items: center; gap: 10px; }
.sync-pill { display: flex; align-items: center; gap: 7px; padding: 6px 12px; border-radius: 999px; background: var(--surface-2); font-size: 12px; font-weight: 600; color: var(--text-tertiary); }
.sync-pill-active { background: var(--accent-soft); color: var(--accent); }
.sync-pulse { width: 6px; height: 6px; border-radius: 999px; background: var(--accent); animation: dashboard-pulse 1.4s ease-in-out infinite; }
@keyframes dashboard-pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.35; } }

.sync-progress-track { height: 7px; border-radius: 999px; background: var(--surface-2); overflow: hidden; margin-bottom: 8px; margin-top: 4px; }
.sync-progress-fill {
  height: 100%;
  border-radius: 999px;
  background-image: repeating-linear-gradient(45deg, var(--accent) 0 7px, var(--accent-strong) 7px 14px);
  background-size: 28px 100%;
  animation: dashboard-stripes 0.9s linear infinite;
}
@keyframes dashboard-stripes { from { background-position: 0 0; } to { background-position: 28px 0; } }
.sync-progress-label { display: flex; justify-content: space-between; font-size: 12px; color: var(--text-tertiary); margin-bottom: 16px; }

.sync-disabled-note { margin: 16px 0 0; }

.sync-log {
  background: var(--bg);
  border: 1px solid var(--border-subtle);
  border-radius: 8px;
  padding: 12px 14px;
  font-family: var(--font-mono);
  font-size: 12px;
  line-height: 1.9;
  color: var(--text-secondary);
  max-height: 132px;
  overflow-y: auto;
  margin-top: 16px;
}
.sync-log-line { color: var(--text-tertiary); }
.sync-log-empty { font-style: italic; }
```

(Named the keyframes `dashboard-pulse`/`dashboard-stripes` rather than reusing the mockup's bare `pulse`/`stripes` names — `style.css` may already define differently-behaving animations under those names elsewhere in the file; check with `grep -n "@keyframes" app/web/static/style.css` before this step and reuse the existing name instead if an identical-behavior one already exists, to avoid a duplicate/conflicting keyframes definition.)

- [ ] **Step 3: Commit**

```bash
git add app/web/static/style.css
git commit -m "Add dashboard CSS: stat cards, coverage bar, sync panel"
```

---

### Task 8: End-to-end verification against the real dev environment

**Files:** none (verification only — no code changes).

Per this project's standing rule (CLAUDE.md's environment notes: "Verify worker-layer changes end-to-end against real media during development, not just with unit tests"), and because this feature's core value (the live SSE panel, real sync timing) can't be proven by unit tests alone:

- [ ] **Step 1: Start the real stack**

```bash
sg docker -c 'docker compose up --build'
```

- [ ] **Step 2: Confirm the startup backfill ran**

Check the container logs for either a `startup: backfilled source for N previously-untyped cached titles` line, or confirm its absence is correct (N == 0) by spot-checking a few rows:

```bash
sg docker -c 'docker compose exec <service> python -c "
from app.settings import load_settings
from app.worker import quote_index
s = load_settings()
titles = quote_index.list_cached_titles(s.quote_index_db_path)
print(len(titles), \"cached titles\")
print(sum(1 for t in titles if not t.source), \"still missing source\")
"'
```

Expected: "still missing source" is 0 (or drops to 0 after the container's had one full startup cycle).

- [ ] **Step 3: Load `/dashboard` in a browser and confirm the stat cards show real numbers**

Navigate to `http://<server-host>:1919/dashboard` (or whatever `WIZARD_PORT` is configured to). Confirm:
- The 4 stat cards show non-zero, plausible numbers matching this developer's real library scale (thousands, not the mockup's placeholder figures).
- The coverage bar's three segments sum to ~100% (allow for rounding).
- The sidebar's "Dashboard" nav item is highlighted as active, and `/` now redirects here instead of `/generate`.

- [ ] **Step 4: Trigger a manual sync and watch the live panel**

Click "Sync now". Confirm:
- The button disables and the pill flips to "Syncing" within ~1s (via SSE, no manual page refresh).
- The progress bar and "X of Y" counter actually advance as titles are processed (if the library is already fully synced, this may complete near-instantly with 0 new titles — to see real in-flight progress, either point at a library with at least a few new/never-synced titles, or temporarily add a test title to a configured library folder first).
- The log tail shows real lines (`Checking library: ...`, `Extracted subtitles — ...` for anything genuinely new).
- The panel returns to "Idle" and the "Last run ... found N new titles" line updates once the cycle completes.

- [ ] **Step 5: Confirm a second browser tab reflects the same state**

Open `/dashboard` in a second tab while a sync is running (or trigger one while both tabs are open). Confirm both tabs show consistent, matching progress — this is the check that the SQLite-backed poll-and-diff design actually gives a consistent shared view, not per-tab drift.

- [ ] **Step 6: Confirm `library_sync.enabled: false` still allows a manual run**

If this developer's `config.yaml` currently has `library_sync.enabled: true`, temporarily flip it to `false` via `/settings/cache`, reload `/dashboard`, and confirm: the sync panel still renders (not hidden), the disabled-sync note with the link to Settings appears, and "Sync now" still works. Revert the setting afterward if it was intentionally on.

- [ ] **Step 7: No commit for this task** — if Step 2-6 surface a real bug, fix it as a small follow-up commit referencing which step caught it (matching this project's existing "found via real-library verification" commit-message convention, e.g. `git log --oneline | grep -i "fix.*found"` for examples), then re-run the affected steps above.
