# Dashboard — Design Spec

*Brainstormed 2026-08-28. Architectural-path spec per superpowers:brainstorming.*

## Summary

Add a "Dashboard" page to the local web app (`app/web/`) that becomes the new
default landing page (`/` once setup is complete — the wizard still owns `/`
pre-setup). It shows subtitle-cache coverage across the installer's
configured Plex libraries and a live view of `library_sync`'s background
sync activity, with a manual "Sync now" trigger. Based on a design mockup
already produced in an earlier Claude Design canvas session (artboard
`Dashboard.dc.html`), reviewed against the current codebase.

This is real new-subsystem work, not just a template: two backend gaps have
to be filled before the mockup's stat cards and live panel can show real
data (see "Why this isn't just UI work" below).

## Why this isn't just UI work

1. **No sidecar/embedded/none breakdown is persisted anywhere.**
   `quote_index.db`'s `cached_titles` table (`app/worker/quote_index.py`)
   only stores `guid/rating_key/title/library_name` — not subtitle source
   type, even though `SubtitleSource` (sidecar/embedded/none) is already
   computed per-title and written into each title's cache JSON payload
   (`app/worker/subtitles.py`). And titles with **no** subtitles are never
   written to the index at all — `sync_one_title`
   (`app/worker/library_sync.py:76`) only calls
   `quote_index.upsert_cached_title` when a source was actually found. The
   "764 titles with no subs" figure in CLAUDE.md Section 5 came from a
   one-off manual count during a `scripts/build_full_cache.py` run, not a
   live-queryable stat.
2. **No live progress-reporting mechanism exists.** `library_sync_task`
   (`app/worker/library_sync.py:219`) loops silently — nothing a web route
   could poll or subscribe to for "61% · 7 of 12" or a log tail. This is the
   gap CLAUDE.md Section 14/V3 Phase 4 flagged ("the natural home for a real
   progress/ETA UI" that Phase 4's `/snip-search` Tier 2 depends on) — this
   dashboard is what builds it.

## Decisions (from brainstorming Q&A)

- **Route**: Dashboard becomes the new default landing page. `/` serves the
  wizard pre-setup (unchanged) and the Dashboard once setup is complete,
  replacing whatever `/` currently falls through to. Generate/Settings
  remain the other sidebar items.
- **Source-type tracking**: build it now, not deferred. Add a `source`
  column to `cached_titles` and a new `no_subtitle_titles` table, so the
  stat cards show real, live numbers rather than an approximation.
- **Progress source of truth**: persisted in SQLite (`quote_index.db`), not
  in-memory — survives a page refresh or the web process restarting
  mid-sync.
- **Live update mechanism**: Server-Sent Events (`GET
  /dashboard/sync-stream`), not htmx polling — a new pattern for this
  codebase (which otherwise uses htmx partial swaps throughout), justified
  here by the update-fidelity trade-off explored in brainstorming.
- **Manual "Sync now"**: build it. Enabled when idle, triggers an immediate
  cycle via the same code path as the scheduled sync
  (`run_library_sync_once`/`sync_library`); disabled only while a cycle is
  already running.
- **When `library_sync.enabled` is off**: the full sync panel still renders,
  always idle, with "Sync now" available as a one-off manual run even though
  the recurring background task isn't scheduled.

## Architecture

Three pieces:

### 1. New persisted state in `quote_index.db`

- `cached_titles.source TEXT` — `"sidecar"` or `"embedded"` (mirrors
  `SubtitleSource`).
- `no_subtitle_titles` table — `guid, rating_key, title, library_name,
  checked_at` — the negative case `cached_titles` never recorded. Mirrors
  `cached_titles`'s shape/style.
- `library_sync_progress` table — single current-run row:
  `status` ("idle"/"running"), `current_library`, `current_title`,
  `processed`, `total`, `started_at`, `last_synced_at`,
  `last_run_new_count`. Seeded with an idle row on first use so the
  dashboard always has something to read (first-run / never-synced
  zero-state).
- `library_sync_log` table — `seq INTEGER PRIMARY KEY, ts, message` capped
  to the last ~50 rows (trimmed each insert), for the scrolling log tail.

### 2. `library_sync.py` changes

- `sync_one_title`: before doing any work, also skip (without re-probing)
  a title already recorded in `no_subtitle_titles`, unless `force=True` —
  closes the "no-sub titles get fully reprocessed every single sync cycle
  forever" gap that exists today, as a side effect of adding the tracking.
  When a probe comes back `SubtitleSource.NONE`, write to
  `no_subtitle_titles` instead of silently discarding the result.
- `sync_library`: per-item loop now also upserts `library_sync_progress`
  (current library/title, processed/total) and inserts one
  `library_sync_log` line per item, trimming to the last 50. Cycle end
  flips `status` back to `"idle"` and stamps `last_synced_at`. Wrapped so
  Plex-unreachable and per-title-error paths (already handled, see
  `library_sync.py:120-127`) still reach the `status = "idle"` reset in a
  `finally` — a Plex outage mid-cycle must not leave the dashboard stuck
  showing "Syncing" forever.
- `library_sync_task`'s scheduled loop gains the same
  `status == "running"` guard the manual trigger uses, so a scheduled tick
  that lands while a manual run is in flight skips that cycle instead of
  racing it for the same rows.
- Startup: any row left in `"running"` state from a prior process's
  unclean exit gets reset to `"idle"` before anything else touches it —
  mirrors the existing `_clear_scratch_dir` on-startup pattern
  (`app/main.py:23-25`).

### 3. Web app gets Plex/sync access it doesn't have today

`app/runtime.py`'s `SettingsHolder` already threads the live bot reference
from `main.py`'s loop into the web app (`settings_holder.bot`). Extend the
same pattern:

- `settings_holder.plex_client` — set/updated by `main.py`'s loop whenever
  the worker is (re)built, same lifecycle as `settings_holder.bot`.
- `settings_holder.trigger_sync_now()` — a callback the web app calls to
  spawn `run_library_sync_once` as an `asyncio.Task` on the shared event
  loop, reusing the exact same function the scheduled task calls. No second
  sync code path.

New web routes (`app/web/app.py`):

- `GET /` — Dashboard (post-setup default; wizard pre-setup, unchanged
  logic).
- `POST /sync/run` — checks `library_sync_progress.status`; if idle, calls
  `trigger_sync_now()` and returns immediately (200, or a fragment
  reflecting the now-"running" state for htmx to swap in). If already
  running, returns a no-op response (button should already be disabled
  client-side, but the guard is server-side, not just cosmetic).
- `GET /dashboard/sync-stream` — `StreamingResponse`, `text/event-stream`.
  Polls `library_sync_progress` + latest `library_sync_log` seq roughly
  every 1s server-side, emits an SSE `message` event only when something
  changed (poll-and-diff, not true push — the DB stays the single source of
  truth, so a second tab or a refresh mid-sync sees identical, correct
  state). Exits cleanly on client disconnect (standard `StreamingResponse`
  generator behavior — no extra teardown needed).

## Data flow — stat cards

Computed once per page load (not part of the SSE stream — not worth
polling every second): for each configured library, join
`cached_titles` (grouped by `source`) + `no_subtitle_titles` count against
that library's live Plex item count (`PlexClient.enumerate_section`,
already exists) for the "X / library total" denominator, since the index
alone can't say how big the library actually is.

## Migration for existing installs

This developer's real `quote_index.db` already has ~10K cached titles with
no `source` column. A one-time backfill (read each cached title's existing
cache JSON payload's `source` field, populate the new column) runs
automatically on startup (`ALTER TABLE ... ADD COLUMN` + backfill loop),
not as a manual `scripts/` step — consistent with this project's existing
aversion to manual migration steps (e.g. `build_full_cache.py`'s
incremental-by-default design).

## Error handling summary

| Scenario | Handling |
|---|---|
| Plex unreachable during manual sync | Existing per-library try/except degrades gracefully; progress row still resets to `"idle"` in a `finally`. |
| Concurrent trigger (button-mash, two tabs, or manual vs. scheduled overlap) | `status == "running"` guard, checked by both `POST /sync/run` and the scheduled loop. |
| SSE client disconnect | Standard `StreamingResponse` generator teardown — no new machinery. |
| Web process restart mid-sync | Startup resets any stale `"running"` row to `"idle"`. |
| First run / sync never run | `library_sync_progress` seeded with an idle zero-state row; stat cards show 0/0 rather than erroring. |
| Existing installs without `source` column | Automatic on-startup backfill migration. |

## Testing

- Unit tests for new `quote_index.py` functions: progress upsert, log
  trim-to-50, no-subtitle-title upsert/lookup, source-column backfill.
- Unit tests for `sync_one_title`'s new skip-if-already-no-sub branch, and
  for per-item progress/log writes in `sync_library`.
- Integration test for `POST /sync/run`'s concurrency guard (two rapid
  triggers → one actual run).
- **Verify the SSE panel and a real sync cycle end-to-end against the real
  dev environment** (per CLAUDE.md's standing rule for worker-layer
  changes) — real timing/Plex behavior is what a synthetic test won't
  catch, and this project has been bitten by exactly this class of gap
  before (Section 4's cross-media quote_index leak, found only by running
  real data).

## Out of scope for this pass

- Historical sync-run history (only the current/last run is tracked, no
  run log beyond the single progress row + capped log tail).
- Per-library sync-now (the trigger runs a full cycle across all
  configured libraries, matching the scheduled task's existing behavior).
- Any change to `/generate` or `/settings` beyond adding the Dashboard nav
  item and making `/` route to it.
