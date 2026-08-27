"""One-off/manual CLI: extract + cache subtitles (and warm the candidate
cache) for every movie and TV episode across all configured libraries.
Sequential, not concurrent — deliberately mirrors /search-episodes-quote's
own reasoning (avoid hammering Plex/ffmpeg with many simultaneous
extractions).

All real per-title logic lives in app/worker/library_sync.py's
sync_one_title() — this script is just enumeration + a progress-printing
loop around it, so the manual CLI and the automatic library_sync_task()
(app/main.py, opt-in via config.yaml's library_sync.enabled) share exactly
one implementation of "how to cache a title."

Incremental by default: a title whose subtitle cache file already exists is
skipped with only a cheap path.exists() check (no read/parse, no Plex path
resolution beyond what enumeration already gave for free) — so re-running
this after the first full build costs almost nothing for the vast majority
of the library and only does real work for titles that are new or were
previously skipped (e.g. a path-mapping gap that's since been fixed). Pass
--force to ignore existing cache files and reprocess everything (e.g. after
a config.yaml change that could affect every title, or a fresh full build).

This script does not detect or clean up removed titles — that's what the
automatic library_sync feature (config.yaml's library_sync.enabled) does,
since removal needs the change-detection + safety-guard logic in
library_sync.py, not just a one-off enumeration pass.

Run inside the container (PYTHONPATH must include /app, since running a
script by file path doesn't add the working directory to sys.path the way
`python -m` does):

    docker compose exec -e PYTHONPATH=/app cinesnip python /app/scripts/build_full_cache.py [--force]

(mount or copy this file into the container first if scripts/ isn't already
bind-mounted — it isn't, by default, in docker-compose.yml.)
"""

from __future__ import annotations

import asyncio
import sys
import time

from app.settings import load_settings
from app.worker.library_sync import sync_one_title
from app.worker.plex_client import MovieResult, PlexClient

settings = load_settings()
plex = PlexClient(settings)

FORCE = "--force" in sys.argv


async def main() -> None:
    items: list[MovieResult] = []
    for library_name, section in plex.library_sections():
        items.extend(plex.enumerate_section(section))

    total = len(items)
    print(f"Found {total} titles across all configured libraries. Starting…", flush=True)

    counts = {"cached": 0, "sidecar": 0, "embedded": 0, "none": 0, "skip": 0, "error": 0}
    t0 = time.monotonic()

    for i, item in enumerate(items, start=1):
        outcome = await sync_one_title(settings, item, force=FORCE)
        if outcome.startswith("CACHED"):
            counts["cached"] += 1
        elif outcome.startswith("OK (sidecar"):
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
        # "CACHED" outcomes are deliberately silent even on the heartbeat —
        # on an incremental run almost everything is CACHED, and printing it
        # every 100 items would bury the handful of lines that actually
        # matter (new titles, errors, skips).
        if outcome.startswith(("ERROR", "SKIP")) or (not outcome.startswith("CACHED") and i % 100 == 0) or i == total:
            elapsed = time.monotonic() - t0
            print(f"[{i}/{total}] ({elapsed:.0f}s elapsed) {outcome}", flush=True)

    elapsed = time.monotonic() - t0
    print(
        f"\nDONE in {elapsed:.0f}s ({elapsed/60:.1f}min). "
        f"already_cached={counts['cached']} sidecar={counts['sidecar']} embedded={counts['embedded']} "
        f"none={counts['none']} skip={counts['skip']} error={counts['error']}",
        flush=True,
    )


if __name__ == "__main__":
    asyncio.run(main())
