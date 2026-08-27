from __future__ import annotations

import asyncio
import logging
import os
import random
from dataclasses import dataclass

from app.settings import Settings, SettingsError
from app.worker import quote_index
from app.worker.path_mapper import NoPathMappingError, resolve_container_path
from app.worker.plex_client import MovieResult, PlexClient
from app.worker.quotes import delete_cached_candidates, get_or_build_candidates
from app.worker.subtitles import (
    SubtitleSource,
    cache_path_for_guid,
    delete_cached_subtitles,
    get_subtitles,
)

logger = logging.getLogger("cinesnip.library_sync")

# How many titles Plex still lists as present get spot-checked on disk
# before trusting an apparent removal enough to actually delete cache
# entries — not config, since it's a safety-margin constant, not something
# an installer should need to tune.
_SPOT_CHECK_SAMPLE_SIZE = 10


@dataclass
class LibrarySyncResult:
    library_name: str
    plex_error: bool = False
    added: int = 0
    already_cached: int = 0
    skipped_no_mapping: int = 0
    skipped_missing_file: int = 0
    errors: int = 0
    removed: int = 0
    # "mount_check_failed" | "spot_check_failed" | None
    removal_skipped_reason: str | None = None


async def sync_one_title(settings: Settings, item: MovieResult, *, force: bool = False) -> str:
    """Extracted from scripts/build_full_cache.py's process_one() — the one
    shared implementation for both the manual script and the automatic
    sync task. Skips a title entirely (bare file-existence check, no read/
    parse) unless forced, so re-running this against an already-synced
    library costs almost nothing.
    """
    if not force and cache_path_for_guid(settings.cache_dir, item.guid).exists():
        return f"CACHED (already have it): {item.title}"

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

    if result.source is not SubtitleSource.NONE and result.entries:
        quote_index.upsert_cached_title(
            settings.quote_index_db_path, result.guid, item.rating_key, item.title, item.library_name
        )
        get_or_build_candidates(
            settings.cache_dir, result.guid, result.entries, settings.quote_match.max_window_gap_seconds
        )

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


async def sync_library(
    settings: Settings,
    plex: PlexClient,
    library_name: str,
    section,
    updated_at: int,
) -> LibrarySyncResult:
    result = LibrarySyncResult(library_name=library_name)

    try:
        live_items = await asyncio.to_thread(plex.enumerate_section, section)
    except Exception as exc:  # noqa: BLE001 - Plex being briefly unreachable must not crash the sync loop
        logger.warning(
            "library sync: could not enumerate '%s' — Plex unreachable, skipping this cycle: %s",
            library_name,
            exc,
        )
        result.plex_error = True
        return result

    for item in live_items:
        outcome = await sync_one_title(settings, item)
        if outcome.startswith("CACHED"):
            result.already_cached += 1
        elif outcome.startswith("OK"):
            result.added += 1
        elif outcome.startswith("SKIP (no path mapping"):
            result.skipped_no_mapping += 1
        elif outcome.startswith("SKIP (file not found"):
            result.skipped_missing_file += 1
        elif outcome.startswith("ERROR"):
            result.errors += 1
            logger.warning("library sync: %s", outcome)

    live_guids = {item.guid for item in live_items}
    existing = quote_index.list_cached_titles_for_library(settings.quote_index_db_path, library_name)
    removal_candidates = [t for t in existing if t.guid not in live_guids]

    if removal_candidates:
        if not _mount_check(settings, library_name):
            result.removal_skipped_reason = "mount_check_failed"
            return result

        sample = random.sample(live_items, min(_SPOT_CHECK_SAMPLE_SIZE, len(live_items))) if live_items else []
        for item in sample:
            try:
                container_path = resolve_container_path(
                    item.plex_path, settings.path_mappings_for(item.library_name)
                )
            except NoPathMappingError:
                continue  # a separate, unrelated problem — not evidence of a mount failure
            if not os.path.exists(container_path):
                logger.warning(
                    "library sync: spot check failed for '%s' — a title Plex still lists "
                    "('%s') has no file on disk — skipping cleanup this cycle",
                    library_name,
                    item.title,
                )
                result.removal_skipped_reason = "spot_check_failed"
                return result

        for cached in removal_candidates:
            quote_index.remove_cached_title(settings.quote_index_db_path, cached.guid)
            delete_cached_subtitles(settings.cache_dir, cached.guid)
            delete_cached_candidates(settings.cache_dir, cached.guid)
            result.removed += 1

    # Only persisted once the cycle (including any removal work) has fully
    # completed for this library — if either safety layer aborted above,
    # this is never reached, so the next cycle's cheap check still sees
    # "changed" and retries the whole thing rather than giving up silently.
    quote_index.set_section_updated_at(settings.quote_index_db_path, library_name, updated_at)
    return result


async def run_library_sync_once(settings: Settings, plex: PlexClient) -> list[LibrarySyncResult]:
    try:
        current = await asyncio.to_thread(plex.current_section_updated_ats)
    except Exception as exc:  # noqa: BLE001 - Plex being briefly unreachable must not crash the sync loop
        logger.warning("library sync: could not check for library changes — Plex unreachable: %s", exc)
        return []

    sections_by_name = dict(plex.library_sections())
    results: list[LibrarySyncResult] = []

    for library_name, updated_at in current.items():
        stored = quote_index.get_section_updated_at(settings.quote_index_db_path, library_name)
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


async def library_sync_task(settings: Settings, plex: PlexClient) -> None:
    """Joined into app/main.py's asyncio.gather() when library_sync.enabled
    is set. Runs once immediately (so enabling the feature doesn't wait a
    full interval), then sleeps and repeats. Wraps each cycle so one bad
    cycle never kills the whole gather() and takes the bot down with it.
    """
    while True:
        try:
            await run_library_sync_once(settings, plex)
        except Exception:
            logger.exception("library sync: unexpected error in sync cycle")
        await asyncio.sleep(settings.library_sync.interval_hours * 3600)
