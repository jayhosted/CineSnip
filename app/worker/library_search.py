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
    per_title_limit: int = 3,
) -> list[LibraryQuoteMatch]:
    """Fuzzy-search a quote across every already-cached title's subtitles.

    Deliberately skips read_cached_subtitles()'s sidecar/video freshness
    check (no path args passed) — this is what keeps a library-wide search
    fast (no Plex/filesystem calls per title), at the cost of occasionally
    matching against a title whose subtitles changed since caching. That
    trade-off matches this feature's "instant" design goal; an individual
    /cinesnip flow on that title still re-validates freshness as normal.

    Diversity-first ranking, not a hard one-per-title cap: every title's
    best-scoring line competes for a slot (ranked by score) before any
    title's second-best line does, which is what keeps results spread
    across films once the cache has real breadth. But when result_limit
    isn't filled by best-lines alone (a small cache, or few titles actually
    matching this quote), remaining slots backfill with each title's next
    line, again ranked by score across titles rather than tied to whichever
    title happens to be checked first. So a title with several strong hits
    can still show up more than once — just never at the expense of a
    better match elsewhere.
    """
    ranked: list[tuple[int, LibraryQuoteMatch]] = []

    for cached in cached_titles:
        subtitle_result = read_cached_subtitles(cache_dir, cached.guid)
        if subtitle_result is None or not subtitle_result.entries:
            continue

        matches = find_quote_matches(
            subtitle_result.entries,
            quote,
            limit=per_title_limit,
            min_score=min_score,
            max_window_gap_seconds=max_window_gap_seconds,
            context_lines=context_lines,
        )

        for rank, match in enumerate(matches):
            ranked.append(
                (
                    rank,
                    LibraryQuoteMatch(
                        rating_key=cached.rating_key,
                        title=cached.title,
                        library_name=cached.library_name,
                        match=match,
                    ),
                )
            )

    # Sort by rank first (every title's best line outranks any title's
    # second-best line), then by score within a rank tier.
    ranked.sort(key=lambda item: (item[0], -item[1].match.score))
    return [match for _, match in ranked[:result_limit]]
