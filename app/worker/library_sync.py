from __future__ import annotations

import asyncio
import logging
import os
import random
from collections import Counter
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
    is_cache_fresh,
    read_cached_subtitles,
)

logger = logging.getLogger("cinesnip.library_sync")

# How many titles the media server still lists as present get spot-checked on disk
# before trusting an apparent removal enough to actually delete cache
# entries — not config, since it's a safety-margin constant, not something
# an installer should need to tune.
_SPOT_CHECK_SAMPLE_SIZE = 10

# Same reasoning/value as api.py's _EPISODE_CACHE_CHECK_CONCURRENCY: bounds
# how many titles' cache checks (or, for a never-synced library, real
# extractions) run at once in sync_library's per-item loop below.
_SYNC_TITLE_CHECK_CONCURRENCY = 8


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


def _cached_title_still_fresh(
    settings: Settings,
    item: MovieResult,
    meta: tuple[str | None, str | None, tuple[float, int] | None],
) -> bool:
    """A title already in known_guids used to be trusted forever with zero
    freshness check once indexed — unlike a live per-title touch (/snip
    movie, /snip tv's whole-show search), which always re-verifies via
    get_subtitles()'s own fingerprint compare, a scheduled sync pass never
    caught a corrected sidecar (e.g. Bazarr re-fetching) until something
    else happened to touch that exact title again. This recheck closes
    that gap: one filesystem stat per already-known title per sync pass,
    using the (source, fingerprint) already bulk-preloaded for the whole
    library in one query (search_index.get_cache_metadata_bulk — see
    sync_library) instead of get_subtitles()'s own 3 per-title SQLite
    round-trips. Still runs find_sidecar_subtitle fresh, not the stored
    sidecar_path, since a newly-appeared higher-priority sidecar must
    still invalidate a stale cache (same rule is_cache_fresh documents).
    A resolution failure is treated as "not fresh" (unlike
    _sidecar_now_exists' inconclusive-stays-cached stance above) so it
    falls through to the existing SKIP (no path mapping) handling below,
    same as a never-before-seen title.
    """
    source_value, _cached_sidecar_path, fingerprint = meta
    try:
        container_path = resolve_container_path(
            item.source_path, settings.path_mappings_for(item.library_name)
        )
    except NoPathMappingError:
        return False
    video_path = Path(container_path)
    sidecar = find_sidecar_subtitle(video_path)
    return is_cache_fresh(source_value, sidecar, video_path, fingerprint)


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
    cache_meta: dict[str, tuple[str | None, str | None, tuple[float, int] | None]] | None = None,
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

    `cache_meta`, when given, gets a SIDECAR/EMBEDDED title in `known_guids`
    the same treatment: one cheap freshness recheck (`_cached_title_still_fresh`)
    instead of trusting it forever. Omitted by scripts/build_full_cache.py
    (same as known_guids/no_subtitle_guids) and by any caller that hasn't
    bulk-preloaded it — those keep the original trust-forever behavior for
    a known guid.
    """
    db_path = settings.quote_index_db_path
    found_new_sidecar = False

    if not force:
        if known_guids is not None and no_subtitle_guids is not None:
            if item.guid in known_guids:
                meta = cache_meta.get(item.guid) if cache_meta is not None else None
                if meta is None or await asyncio.to_thread(
                    _cached_title_still_fresh, settings, item, meta
                ):
                    return f"CACHED (already have it): {item.title}"
                # else: cache_meta found this title stale — fall through to
                # real processing below instead of trusting it forever.
                # Logged because a mismatch here is otherwise invisible until
                # someone notices an unexpected re-extraction in the sync log
                # and has no stored value left to compare against (the stale
                # row gets overwritten by the very re-extraction this causes).
                logger.info(
                    "library sync: cache recheck found '%s' stale (stored fingerprint=%s) — re-extracting",
                    item.title,
                    meta[2],
                )
            elif item.guid in no_subtitle_guids:
                found_new_sidecar = await asyncio.to_thread(_sidecar_now_exists, settings, item)
                if not found_new_sidecar:
                    return f"CACHED (already have it): {item.title}"
            # Either not previously seen at all, stale, or marked NONE with
            # a sidecar that's since appeared — either way, fall through to
            # real processing below instead of trusting a stale/absent cache.
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

    # Two (or more) live items sharing one guid within the SAME library is
    # never legitimate (unlike the same guid appearing across libraries —
    # e.g. a separate 4K library, Section 3 — which is expected and handled
    # elsewhere). Real-world cause, confirmed against this project's own
    # library: a metadata-source (TVDB) episode renumbering that a *arr tool
    # re-grabbed under the new number without removing the stale file under
    # the old one — see docs/build-notes/subtitles-and-search.md. Whichever
    # duplicate this loop processes last silently wins the shared cache row,
    # so this is flagged up front rather than only showing up later as
    # unexplained "cache recheck found ... stale" churn on every sync.
    guid_counts = Counter(item.guid for item in live_items)
    duplicate_guids = {guid: count for guid, count in guid_counts.items() if count > 1}
    if duplicate_guids:
        for guid in duplicate_guids:
            titles = [item.title for item in live_items if item.guid == guid]
            logger.warning(
                "library sync: '%s' has %d items sharing one guid (%s) — %s — "
                "likely a duplicate file left behind by a metadata renumbering; "
                "the subtitle cache will keep flip-flopping between them until "
                "one is removed",
                library_name,
                len(titles),
                guid,
                ", ".join(titles),
            )
            # Also surfaced in the dashboard's own activity log (not just the
            # process logger) — this is exactly the kind of thing that
            # otherwise only shows up as unexplained repeat re-extractions,
            # noticed by chance rather than flagged where the user is
            # already looking after clicking "Sync now".
            await asyncio.to_thread(
                append_sync_log,
                settings.quote_index_db_path,
                f"Warning — duplicate guid in {library_name}: {', '.join(titles)}",
            )

    # Two bulk queries up front instead of sync_one_title() opening a fresh
    # SQLite connection per item to check "already indexed?" — see Fix 3's
    # precedent in api.py's search_quote_extend, which fixed the same
    # pattern for its own no-subtitle check. Directly measured: without
    # this, a concurrent search against a ~1400-title library stalls ~14s
    # while this loop runs, even though every item here is a cache hit.
    known_guids = frozenset(t.guid for t in await asyncio.to_thread(search_index.list_titles, settings.quote_index_db_path))
    no_subtitle_guids = frozenset(await asyncio.to_thread(list_no_subtitle_guids, settings.quote_index_db_path))
    # Same bulk-preload idea as known_guids/no_subtitle_guids above, one
    # query for the whole library instead of get_subtitles()'s own 3
    # per-title SQLite round-trips — lets sync_one_title's known_guids
    # branch below cheaply re-verify freshness instead of trusting a
    # known title forever (docs/build-notes/subtitles-and-search.md).
    cache_meta = await asyncio.to_thread(
        search_index.get_cache_metadata_bulk,
        settings.quote_index_db_path,
        [item.guid for item in live_items],
    )
    await asyncio.to_thread(set_library_item_count, settings.quote_index_db_path, library_name, total_items)
    await asyncio.to_thread(
        append_sync_log, settings.quote_index_db_path, f"Checking library: {library_name} — {total_items} items"
    )
    # One upfront write so the dashboard shows the correct total/0-processed
    # state immediately, rather than waiting for the first item to finish.
    await asyncio.to_thread(update_sync_progress, settings.quote_index_db_path, library_name, None, 0, total_items)

    # Bounded concurrency, not the old strictly-sequential loop — directly
    # measured on this project's real ~1400-movie/~10.2k-episode library:
    # adding _cached_title_still_fresh's one filesystem stat per
    # already-known title (see cache_meta above) to a sequential loop took
    # a full sync pass from ~3 minutes to ~43 minutes (~220ms/title on this
    # setup's Windows-drive/WSL2-mounted media — much higher than a native
    # Linux stat), the exact same class of bug _EPISODE_CACHE_CHECK_CONCURRENCY
    # fixed in api.py's whole-show search, just at ~40x the item count.
    # _SYNC_TITLE_CHECK_CONCURRENCY caps how many titles are ever actually
    # being checked/extracted at once (protects ffmpeg from a never-synced
    # library the same way the api.py bound does), while letting the common
    # already-cached case run near this concurrency limit's speed instead of
    # one-at-a-time.
    #
    # current_title/processed necessarily become best-effort under
    # concurrency: current_title is written when each title's check STARTS
    # (so it never shows an already-completed title stuck on screen — the
    # original issue #15 bug this guarded against), but with several titles
    # in flight it may cycle rapidly through fast cache hits before settling
    # on whichever title is genuinely slow (a real extraction). processed
    # only increments once a title's check actually finishes, so the
    # count/percentage stays honest even though completion order no longer
    # matches live_items' order.
    semaphore = asyncio.Semaphore(_SYNC_TITLE_CHECK_CONCURRENCY)
    processed = 0

    async def _bounded(item: MovieResult) -> None:
        nonlocal processed
        async with semaphore:
            await asyncio.to_thread(
                update_sync_progress, settings.quote_index_db_path, library_name, item.title, processed, total_items
            )
            outcome = await sync_one_title(
                settings, item, known_guids=known_guids, no_subtitle_guids=no_subtitle_guids,
                cache_meta=cache_meta,
            )
            processed += 1

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

    await asyncio.gather(*(_bounded(item) for item in live_items))

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
            # A per-library "changed" run already lands an activity-log line
            # via append_sync_log inside sync_one_title's callers — a
            # no-changes run wrote nothing there at all, so the dashboard's
            # scrolling log looked identical before and after a click with
            # no visible confirmation anything ran (the subtitle's updated
            # timestamp alone is too subtle to notice).
            await asyncio.to_thread(
                append_sync_log,
                db_path,
                f"No changes — {len(current)} librar{'y' if len(current) == 1 else 'ies'} checked",
            )

        return results
    finally:
        await asyncio.to_thread(
            finish_sync_run,
            db_path,
            new_count=sum(r.added for r in results),
            removed_count=sum(r.removed for r in results),
            error_count=sum(r.errors for r in results),
        )


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
