from __future__ import annotations

import asyncio
import logging
import os
import random
from dataclasses import dataclass
from pathlib import Path

from app.settings import Settings, SettingsError
from app.worker import search_index
from app.worker.media_client import MediaClient, MovieResult
from app.worker.path_mapper import NoPathMappingError, resolve_container_path
from app.worker.quote_index import (
    append_sync_log,
    finish_sync_run,
    get_library_item_count,
    get_section_updated_at,
    is_no_subtitle_title,
    list_no_subtitle_guids,
    set_library_item_count,
    set_section_updated_at,
    start_sync_run,
    update_sync_progress,
    upsert_no_subtitle_title,
)
from app.worker.subtitles import (
    SubtitleSource,
    cache_path_for_guid,
    find_sidecar_subtitle,
    get_subtitles,
    read_cached_subtitles,
)

logger = logging.getLogger("cinesnip.library_sync")

# How many titles the media server still lists as present get spot-checked on disk
# before trusting an apparent removal enough to actually delete cache
# entries — not config, since it's a safety-margin constant, not something
# an installer should need to tune.
_SPOT_CHECK_SAMPLE_SIZE = 10


def _sidecar_now_exists(settings: Settings, item: MovieResult) -> bool:
    """A NONE (no-subtitles-found) result caches without a freshness check —
    no single file backs "no subtitles", so unlike SIDECAR/EMBEDDED results
    it never self-invalidates (docs/build-notes/subtitles-and-search.md).
    Cheap to recheck anyway: a sync pass already has item.source_path fresh
    from its own enumerate_section() call, so this costs one filesystem
    check (find_sidecar_subtitle — no ffmpeg, no Plex), not a live lookup.
    Only catches a sidecar dropped in later, not a video replaced with a
    remux that now has embedded subs — that would need a stored fingerprint
    for NONE entries, which doesn't exist and isn't the documented escape
    hatch (CLAUDE.md decision #7 is specifically "drop a sidecar .srt").
    A resolution failure (e.g. path mapping changed) is treated as
    inconclusive, not as "a sidecar exists" — stay cached rather than
    force a reprocess neither this item nor its mapping actually earned.
    """
    try:
        container_path = resolve_container_path(
            item.source_path, settings.path_mappings_for(item.library_name)
        )
    except NoPathMappingError:
        return False
    return find_sidecar_subtitle(Path(container_path)) is not None


@dataclass
class LibrarySyncResult:
    library_name: str
    media_error: bool = False
    added: int = 0
    already_cached: int = 0
    skipped_no_mapping: int = 0
    skipped_missing_file: int = 0
    errors: int = 0
    removed: int = 0
    # "mount_check_failed" | "spot_check_failed" | None
    removal_skipped_reason: str | None = None


async def sync_one_title(
    settings: Settings,
    item: MovieResult,
    *,
    force: bool = False,
    known_guids: frozenset[str] | None = None,
    no_subtitle_guids: frozenset[str] | None = None,
) -> str:
    """Extracted from scripts/build_full_cache.py's process_one() — the one
    shared implementation for both the manual script and the automatic
    sync task. Skips a title entirely once it's authoritatively indexed in
    search_index (or no_subtitle_titles) unless forced, so re-running this
    against an already-synced library costs almost nothing.

    search_index is the authoritative gate, not legacy-JSON-file existence:
    since get_subtitles() (Task 3) writes only into search_index and no
    longer touches the legacy JSON cache, a title fully indexed there has
    no JSON file at all — checking JSON existence first would wrongly fall
    through to a full re-extraction on every run.

    A title cached before this migration (or before source/no-subtitle
    tracking existed) still has its legacy JSON file with nothing in
    search_index yet. That case is self-healing: one cheap local JSON read
    (no ffmpeg) classifies and backfills it into search_index, and it's
    never revisited after that.

    `known_guids`/`no_subtitle_guids`, when given, let a caller looping
    over a whole library (sync_library below) answer "already indexed?"
    from two sets preloaded once, instead of this function opening a fresh
    SQLite connection per item — confirmed by direct measurement to add
    ~14s of concurrent-request contention on a ~1400-title library (same
    class of bug api.py's search_quote_extend already fixed once for its
    own no-subtitle check; this closes the matching gap here). Omitted
    entirely by scripts/build_full_cache.py, which falls back to the
    original per-item DB check (and its own `--force` if a full recheck
    is ever wanted there).

    A guid in `no_subtitle_guids` still gets one cheap recheck
    (`_sidecar_now_exists`) before being trusted as still NONE — a NONE
    result never self-invalidates like SIDECAR/EMBEDDED do (no fingerprint
    backs "no subtitles found"), so a sidecar dropped in after the fact
    would otherwise be invisible forever. Costs one filesystem check per
    NONE title, using item.source_path already fresh from this pass's own
    enumerate_section() — no extra Plex calls.
    """
    db_path = settings.quote_index_db_path
    found_new_sidecar = False

    if not force:
        if known_guids is not None and no_subtitle_guids is not None:
            if item.guid in known_guids:
                return f"CACHED (already have it): {item.title}"
            if item.guid in no_subtitle_guids:
                found_new_sidecar = await asyncio.to_thread(_sidecar_now_exists, settings, item)
                if not found_new_sidecar:
                    return f"CACHED (already have it): {item.title}"
            # Either not previously seen at all, or it was marked NONE and
            # a sidecar has since appeared — either way, fall through to
            # real processing below instead of trusting the stale NONE.
        else:
            already_indexed = await asyncio.to_thread(
                lambda: search_index.has_title(db_path, item.guid) or is_no_subtitle_title(db_path, item.guid)
            )
            if already_indexed:
                return f"CACHED (already have it): {item.title}"

        # Skipped when a fresh sidecar is why we're here — the legacy JSON
        # cache (if any lingers on disk) would still say NONE from before
        # that sidecar existed, silently undoing the recheck above.
        if not found_new_sidecar and cache_path_for_guid(settings.cache_dir, item.guid).exists():
            cached = await asyncio.to_thread(read_cached_subtitles, settings.cache_dir, item.guid)
            if cached is not None:
                if cached.source is SubtitleSource.NONE:
                    await asyncio.to_thread(
                        upsert_no_subtitle_title, db_path, item.guid, item.media_id, item.title, item.library_name
                    )
                else:
                    await asyncio.to_thread(
                        search_index.upsert_title,
                        db_path,
                        item.guid,
                        item.media_id,
                        item.title,
                        item.library_name,
                        cached.source.value,
                        cached.sidecar_path,
                        cached.stream_index,
                        cached.entries,
                        None,  # same-release migration convenience, not relied on for freshness
                    )
                return f"CACHED (backfilled index): {item.title}"

    try:
        container_path = resolve_container_path(
            item.source_path, settings.path_mappings_for(item.library_name)
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
            settings.quote_index_db_path,
            ffprobe_timeout=timeout,
            ffmpeg_timeout=timeout,
        )
    except Exception as exc:  # noqa: BLE001 - one bad file must not stop the run
        return f"ERROR: {item.title}: {exc}"

    if result.source is SubtitleSource.NONE:
        # no_subtitle_titles is a separate table search_index doesn't own —
        # get_subtitles() already wrote the NONE result into search_index
        # itself, so this is the only bookkeeping left to do here.
        await asyncio.to_thread(
            upsert_no_subtitle_title, db_path, result.guid, item.media_id, item.title, item.library_name
        )
    # else: a searchable result. get_subtitles() (Task 3) already wrote it
    # into search_index itself — no further write needed here, and
    # candidates are never persisted in the new design.

    return f"OK ({result.source.value}, {len(result.entries)} entries): {item.title}"


def _mount_check(settings: Settings, library_name: str) -> bool:
    """Layer 1: before trusting any apparent removal for a library, verify
    every one of its configured mounts is actually reachable and non-empty.
    A dead drive or an unmounted share fails this instantly, for the whole
    library at once, before individual titles are even considered."""
    try:
        mappings = settings.path_mappings_for(library_name)
    except SettingsError:
        return True  # no mappings configured for this library isn't a mount failure

    for mapping in mappings:
        root = mapping.container_path
        if not os.path.isdir(root) or not os.listdir(root):
            logger.warning(
                "library sync: mount check failed for '%s' at %s — skipping cleanup this cycle",
                library_name,
                root,
            )
            return False
    return True


def _spot_check_removed_titles(
    settings: Settings, library_name: str, live_items: list[MovieResult]
) -> str | None:
    """Layer 2: a random sample of titles the media server still lists must actually
    resolve on disk before an apparent removal is trusted. Runs entirely
    synchronously (real filesystem stats against mounted media drives) —
    callers must run this via asyncio.to_thread, same as _mount_check.
    Returns a removal_skipped_reason, or None if the sample checked out."""
    sample = random.sample(live_items, min(_SPOT_CHECK_SAMPLE_SIZE, len(live_items))) if live_items else []
    for item in sample:
        try:
            container_path = resolve_container_path(item.source_path, settings.path_mappings_for(item.library_name))
        except NoPathMappingError:
            continue  # a separate, unrelated problem — not evidence of a mount failure
        if not os.path.exists(container_path):
            logger.warning(
                "library sync: spot check failed for '%s' — a title the media server still lists "
                "('%s') has no file on disk — skipping cleanup this cycle",
                library_name,
                item.title,
            )
            return "spot_check_failed"
    return None


async def sync_library(
    settings: Settings,
    media: MediaClient,
    library_name: str,
    section,
    updated_at: int,
) -> LibrarySyncResult:
    result = LibrarySyncResult(library_name=library_name)

    try:
        live_items = await asyncio.to_thread(media.enumerate_section, section)
    except Exception as exc:  # noqa: BLE001 - media server being briefly unreachable must not crash the sync loop
        logger.warning(
            "library sync: could not enumerate '%s' — media server unreachable, skipping this cycle: %s",
            library_name,
            exc,
        )
        result.media_error = True
        return result

    total_items = len(live_items)
    # Two bulk queries up front instead of sync_one_title() opening a fresh
    # SQLite connection per item to check "already indexed?" — see Fix 3's
    # precedent in api.py's search_quote_extend, which fixed the same
    # pattern for its own no-subtitle check. Directly measured: without
    # this, a concurrent search against a ~1400-title library stalls ~14s
    # while this loop runs, even though every item here is a cache hit.
    known_guids = frozenset(t.guid for t in await asyncio.to_thread(search_index.list_titles, settings.quote_index_db_path))
    no_subtitle_guids = frozenset(await asyncio.to_thread(list_no_subtitle_guids, settings.quote_index_db_path))
    await asyncio.to_thread(set_library_item_count, settings.quote_index_db_path, library_name, total_items)
    await asyncio.to_thread(
        append_sync_log, settings.quote_index_db_path, f"Checking library: {library_name} — {total_items} items"
    )
    # One upfront write so the dashboard shows the correct total/0-processed
    # state immediately, rather than waiting for the first item to finish.
    await asyncio.to_thread(update_sync_progress, settings.quote_index_db_path, library_name, None, 0, total_items)

    for index, item in enumerate(live_items, start=1):
        # Written before sync_one_title runs, not after — current_title must
        # reflect the item actually in flight (a slow embedded extraction can
        # take minutes), not the last one that finished. processed=index-1
        # keeps the count/percentage honest: this item isn't done yet.
        await asyncio.to_thread(
            update_sync_progress, settings.quote_index_db_path, library_name, item.title, index - 1, total_items
        )
        outcome = await sync_one_title(
            settings, item, known_guids=known_guids, no_subtitle_guids=no_subtitle_guids
        )
        if outcome.startswith("CACHED"):
            result.already_cached += 1
        elif outcome.startswith("OK"):
            result.added += 1
            await asyncio.to_thread(append_sync_log, settings.quote_index_db_path, f"Extracted subtitles — {item.title}")
        elif outcome.startswith("SKIP (no path mapping"):
            result.skipped_no_mapping += 1
            await asyncio.to_thread(append_sync_log, settings.quote_index_db_path, f"Skipped — {outcome}")
        elif outcome.startswith("SKIP (file not found"):
            result.skipped_missing_file += 1
            await asyncio.to_thread(append_sync_log, settings.quote_index_db_path, f"Skipped — {outcome}")
        elif outcome.startswith("ERROR"):
            result.errors += 1
            logger.warning("library sync: %s", outcome)
            await asyncio.to_thread(append_sync_log, settings.quote_index_db_path, f"Error — {outcome}")

    # One trailing write so the bar reaches a true 100% and current_title
    # clears, rather than staying pinned on the last item while the
    # removal/spot-check phase below (a different kind of work) runs.
    await asyncio.to_thread(
        update_sync_progress, settings.quote_index_db_path, library_name, None, total_items, total_items
    )

    live_guids = {item.guid for item in live_items}
    # search_index is authoritative for what sync_one_title has actually
    # indexed (quote_index.cached_titles is no longer written by this
    # file — see module docstring notes above), so the removal diff reads
    # from there, not the old bookkeeping table.
    existing = await asyncio.to_thread(search_index.list_titles_for_library, settings.quote_index_db_path, library_name)
    removal_candidates = [t for t in existing if t.guid not in live_guids]

    if removal_candidates:
        await asyncio.to_thread(
            update_sync_progress,
            settings.quote_index_db_path,
            library_name,
            f"Verifying {len(removal_candidates)} possibly-removed title(s)",
            total_items,
            total_items,
        )

        if not await asyncio.to_thread(_mount_check, settings, library_name):
            result.removal_skipped_reason = "mount_check_failed"
            return result

        spot_check_failure = await asyncio.to_thread(
            _spot_check_removed_titles, settings, library_name, live_items
        )
        if spot_check_failure is not None:
            result.removal_skipped_reason = spot_check_failure
            return result

        for cached in removal_candidates:
            await asyncio.to_thread(search_index.remove_title, settings.quote_index_db_path, cached.guid)
            # Legacy JSON (if any survives from before this migration) is
            # deliberately left on disk untouched — deleting it isn't this
            # migration's job.
            result.removed += 1

    # Only persisted once the cycle (including any removal work) has fully
    # completed for this library — if either safety layer aborted above,
    # this is never reached, so the next cycle's cheap check still sees
    # "changed" and retries the whole thing rather than giving up silently.
    await asyncio.to_thread(set_section_updated_at, settings.quote_index_db_path, library_name, updated_at)
    return result


async def run_library_sync_once(settings: Settings, media: MediaClient) -> list[LibrarySyncResult]:
    db_path = settings.quote_index_db_path
    if not await asyncio.to_thread(start_sync_run, db_path):
        logger.info("library sync: skipped — a run is already in progress")
        return []

    results: list[LibrarySyncResult] = []
    try:
        try:
            current = await asyncio.to_thread(media.current_section_updated_ats)
        except Exception as exc:  # noqa: BLE001 - media server being briefly unreachable must not crash the sync loop
            logger.warning("library sync: could not check for library changes — media server unreachable: %s", exc)
            return []

        sections_by_name = dict(media.library_sections())

        for library_name, updated_at in current.items():
            stored = await asyncio.to_thread(get_section_updated_at, db_path, library_name)
            has_count = await asyncio.to_thread(get_library_item_count, db_path, library_name) is not None
            if stored == updated_at and has_count:
                continue

            section = sections_by_name[library_name]
            result = await sync_library(settings, media, library_name, section, updated_at)
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
        await asyncio.to_thread(finish_sync_run, db_path, new_count=sum(r.added for r in results))


async def library_sync_task(settings: Settings, media: MediaClient) -> None:
    """Joined into app/main.py's asyncio.gather() when library_sync.enabled
    is set. Runs once immediately (so enabling the feature doesn't wait a
    full interval), then sleeps and repeats. Wraps each cycle so one bad
    cycle never kills the whole gather() and takes the bot down with it.
    """
    while True:
        try:
            await run_library_sync_once(settings, media)
        except Exception:
            logger.exception("library sync: unexpected error in sync cycle")
        await asyncio.sleep(settings.library_sync.interval_hours * 3600)
