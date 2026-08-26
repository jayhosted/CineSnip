from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.worker.quote_index import CachedTitle
from app.worker.quotes import QuoteMatch, find_quote_matches
from app.worker.subtitles import read_cached_subtitles


@dataclass(frozen=True)
class LibraryQuoteMatch:
    rating_key: int
    title: str
    library_name: str
    match: QuoteMatch


def search_cached_library(
    cache_dir: Path,
    cached_titles: list[CachedTitle],
    quote: str,
    result_limit: int,
    min_score: float,
    max_window_gap_seconds: float,
    context_lines: int,
) -> list[LibraryQuoteMatch]:
    """Fuzzy-search a quote across every already-cached title's subtitles.

    Deliberately skips read_cached_subtitles()'s sidecar/video freshness
    check (no path args passed) — this is what keeps a library-wide search
    fast (no Plex/filesystem calls per title), at the cost of occasionally
    matching against a title whose subtitles changed since caching. That
    trade-off matches this feature's "instant" design goal; an individual
    /cinesnip flow on that title still re-validates freshness as normal.

    Caps at one match per title (the title's single best line) so results
    stay diverse across films rather than one film's several lines crowding
    out the rest.
    """
    results: list[LibraryQuoteMatch] = []

    for cached in cached_titles:
        subtitle_result = read_cached_subtitles(cache_dir, cached.guid)
        if subtitle_result is None or not subtitle_result.entries:
            continue

        matches = find_quote_matches(
            subtitle_result.entries,
            quote,
            limit=1,
            min_score=min_score,
            max_window_gap_seconds=max_window_gap_seconds,
            context_lines=context_lines,
        )
        if not matches:
            continue

        results.append(
            LibraryQuoteMatch(
                rating_key=cached.rating_key,
                title=cached.title,
                library_name=cached.library_name,
                match=matches[0],
            )
        )

    results.sort(key=lambda r: -r.match.score)
    return results[:result_limit]
