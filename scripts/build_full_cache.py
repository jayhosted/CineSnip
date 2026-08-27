"""One-off script: extract + cache subtitles (and warm the candidate cache)
for every movie and TV episode across all configured libraries. Sequential,
not concurrent — deliberately mirrors /search-episodes-quote's own reasoning
(avoid hammering Plex/ffmpeg with many simultaneous extractions).

Not part of the running app — a manual utility for warming the whole
library's cache ahead of time, e.g. to test /snip-search at real scale.
Also the closest thing to a working prototype of the "manual cache build"
idea discussed for V3 Phase 6 (CLAUDE.md Section 13) — deliberately kept
here as a reference starting point, not under app/, and not under scratch/
(which app/main.py wipes on every container start).

Run inside the container (PYTHONPATH must include /app, since running a
script by file path doesn't add the working directory to sys.path the way
`python -m` does):

    docker compose exec -e PYTHONPATH=/app cinesnip python /app/scripts/build_full_cache.py

(mount or copy this file into the container first if scripts/ isn't already
bind-mounted — it isn't, by default, in docker-compose.yml.)
"""

from __future__ import annotations

import asyncio
import os
import time

from app.settings import load_settings
from app.worker import quote_index
from app.worker.path_mapper import NoPathMappingError, resolve_container_path
from app.worker.plex_client import MovieResult, PlexClient
from app.worker.quotes import get_or_build_candidates
from app.worker.subtitles import SubtitleSource, get_subtitles

settings = load_settings()
plex = PlexClient(settings)


async def process_one(item: MovieResult) -> str:
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


async def main() -> None:
    items: list[MovieResult] = []
    for section in plex._movie_sections:
        items.extend(plex._to_result(m) for m in section.search(libtype="movie"))

    show_count = 0
    for section in plex._show_sections:
        for show in section.search(libtype="show"):
            show_count += 1
            items.extend(plex._to_result(ep) for ep in show.episodes())

    total = len(items)
    print(f"Found {total} titles ({show_count} shows expanded to episodes). Starting…", flush=True)

    counts = {"sidecar": 0, "embedded": 0, "none": 0, "skip": 0, "error": 0}
    t0 = time.monotonic()

    for i, item in enumerate(items, start=1):
        outcome = await process_one(item)
        if outcome.startswith("OK (sidecar"):
            counts["sidecar"] += 1
        elif outcome.startswith("OK (embedded"):
            counts["embedded"] += 1
        elif outcome.startswith("OK (none"):
            counts["none"] += 1
        elif outcome.startswith("SKIP"):
            counts["skip"] += 1
        elif outcome.startswith("ERROR"):
            counts["error"] += 1

        # Always print errors/skips immediately; otherwise a periodic heartbeat.
        if outcome.startswith(("ERROR", "SKIP")) or i % 100 == 0 or i == total:
            elapsed = time.monotonic() - t0
            print(f"[{i}/{total}] ({elapsed:.0f}s elapsed) {outcome}", flush=True)

    elapsed = time.monotonic() - t0
    print(
        f"\nDONE in {elapsed:.0f}s ({elapsed/60:.1f}min). "
        f"sidecar={counts['sidecar']} embedded={counts['embedded']} none={counts['none']} "
        f"skip={counts['skip']} error={counts['error']}",
        flush=True,
    )


if __name__ == "__main__":
    asyncio.run(main())
