from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.worker import search_index
from app.worker.quote_index import CachedTitle
from app.worker.quotes import QuoteMatch, find_quote_matches, normalize_for_match
from app.worker.subtitles import SubtitleEntry


@dataclass(frozen=True)
class LibraryQuoteMatch:
    rating_key: int
    title: str
    library_name: str
    match: QuoteMatch


def _diversify_and_rank(
    per_title_matches: list[tuple[CachedTitle, list[QuoteMatch]]],
    result_limit: int,
) -> list[LibraryQuoteMatch]:
    """Diversity-first ranking shared by the fast path and the fallback path.

    Every title's best-scoring line competes for a results slot before any
    title's second-best line does, but unfilled slots backfill with each
    title's next-best line (up to per_title_limit, already applied by the
    caller via find_quote_matches' own `limit`) — never at the expense of a
    better match from another title. Implemented by tagging each match with
    its own per-title rank (0 = that title's best line) and sorting globally
    by (rank, -score), so all rank-0 matches sort before any rank-1 match
    regardless of score, and within a rank ties break by score descending.
    """
    ranked: list[tuple[int, LibraryQuoteMatch]] = []

    for cached, matches in per_title_matches:
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

    ranked.sort(key=lambda item: (item[0], -item[1].match.score))
    return [match for _, match in ranked[:result_limit]]


def search_cached_library(
    db_path: Path,
    cached_titles: list[CachedTitle],
    quote: str,
    result_limit: int,
    min_score: float,
    max_window_gap_seconds: float,
    context_lines: int,
    per_title_limit: int = 3,
) -> list[LibraryQuoteMatch]:
    """Fuzzy-search a quote across every already-cached title's subtitles.

    `db_path` is the SQLite search-index DB file (app/worker/search_index.py),
    not a directory — subtitle text lives in its `entries`/`entries_fts`
    tables, not flat JSON cache files.

    An FTS5 pre-filter (search_index.search_title_ids) narrows the search to
    titles whose subtitle text contains at least one of the quote's tokens
    before the expensive fuzzy-scoring pass runs — this is what keeps
    /snip-search fast at full-library scale (see
    docs/design/fts5-search-migration.md). FTS5's tokenizer can miss a
    typo'd or otherwise non-literal query that fuzzy matching would still
    find, so an empty pre-filter result falls back to a full scan of every
    cached title via search_index.iter_all_entries rather than silently
    returning nothing. Both paths funnel through _diversify_and_rank so
    their result ordering can never silently diverge.
    """
    normalized_quote = normalize_for_match(quote)
    if not normalized_quote:
        return []

    title_ids = search_index.search_title_ids(db_path, normalized_quote.split())

    per_title_matches: list[tuple[CachedTitle, list[QuoteMatch]]] = []

    if not title_ids:
        # Fallback: full scan. Iterate every cached title's entries straight
        # from the DB and match each guid back to the caller's CachedTitle
        # list, skipping any guid with no corresponding CachedTitle (mirrors
        # the old JSON-cache behavior of skipping titles with no cache
        # entry).
        titles_by_guid = {cached.guid: cached for cached in cached_titles}
        for guid, entries in search_index.iter_all_entries(db_path):
            cached = titles_by_guid.get(guid)
            if cached is None or not entries:
                continue
            matches = find_quote_matches(
                entries,
                quote,
                limit=per_title_limit,
                min_score=min_score,
                max_window_gap_seconds=max_window_gap_seconds,
                context_lines=context_lines,
            )
            if matches:
                per_title_matches.append((cached, matches))
    else:
        # Fast path: filter cached_titles down to survivors of the FTS5
        # pre-filter by resolving each guid to a title_id, then fetch just
        # those titles' entries in one query.
        survivor_ids = set(title_ids)
        survivors: list[tuple[CachedTitle, int]] = []
        for cached in cached_titles:
            title_id = search_index.get_title_id(db_path, cached.guid)
            if title_id is not None and title_id in survivor_ids:
                survivors.append((cached, title_id))

        entries_by_title_id = search_index.fetch_entries_for_titles(
            db_path, [title_id for _, title_id in survivors]
        )

        for cached, title_id in survivors:
            entries: list[SubtitleEntry] = entries_by_title_id.get(title_id, [])
            if not entries:
                continue
            matches = find_quote_matches(
                entries,
                quote,
                limit=per_title_limit,
                min_score=min_score,
                max_window_gap_seconds=max_window_gap_seconds,
                context_lines=context_lines,
            )
            if matches:
                per_title_matches.append((cached, matches))

    return _diversify_and_rank(per_title_matches, result_limit)
