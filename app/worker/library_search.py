from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.worker import search_index
from app.worker.quote_index import CachedTitle
from app.worker.quotes import QuoteMatch, find_quote_matches, normalize_for_match
from app.worker.subtitles import SubtitleEntry

# Default number of entries either side of an FTS5 hit pulled into its
# adjacent-cue slice before fuzzy-scoring — must be at least
# context_lines + 1 (see _window_pad below) so find_quote_matches' own
# context/adjacent-pair logic has enough surrounding entries to work with.
_DEFAULT_WINDOW_PAD = 3

# How many individual FTS5 entry rows the pre-filter is allowed to surface
# before the fuzzy-scoring pass runs — see search_index.search_entry_ids'
# docstring for why this is entry-level, not title-level.
_DEFAULT_ENTRY_SCAN_LIMIT = 4000


@dataclass(frozen=True)
class LibraryQuoteMatch:
    rating_key: int
    title: str
    library_name: str
    match: QuoteMatch


def _window_pad(context_lines: int) -> int:
    return max(_DEFAULT_WINDOW_PAD, context_lines + 1)


def _merge_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Merge overlapping/touching (lo, hi) index ranges. Touching ranges
    (lo <= previous hi + 1) are merged too, not just overlapping ones, so
    two adjacent hit windows never produce duplicate boundary entries when
    sliced independently."""
    if not ranges:
        return []
    ordered = sorted(ranges)
    merged = [ordered[0]]
    for lo, hi in ordered[1:]:
        last_lo, last_hi = merged[-1]
        if lo <= last_hi + 1:
            merged[-1] = (last_lo, max(last_hi, hi))
        else:
            merged.append((lo, hi))
    return merged


def _matches_for_title(
    windowed_entries: list[tuple[int, SubtitleEntry]],
    merged_ranges: list[tuple[int, int]],
    quote: str,
    min_score: float,
    max_window_gap_seconds: float,
    context_lines: int,
    per_title_limit: int,
) -> list[QuoteMatch]:
    """Fuzzy-score each of a title's merged adjacent-cue windows (already
    fetched — just this title's windowed entries, not its full track), then
    combine and re-limit to per_title_limit BEFORE the caller's diversity
    ranking — a title with many scattered hits must not flood the final
    results at another title's expense."""
    title_matches: list[QuoteMatch] = []
    for lo, hi in merged_ranges:
        slice_entries = [entry for _, entry in windowed_entries if lo <= entry.index <= hi]
        if not slice_entries:
            continue
        title_matches.extend(
            find_quote_matches(
                slice_entries,
                quote,
                limit=per_title_limit,
                min_score=min_score,
                max_window_gap_seconds=max_window_gap_seconds,
                context_lines=context_lines,
            )
        )

    title_matches.sort(key=lambda m: (-m.score, len(m.entry_indices), m.start))
    return title_matches[:per_title_limit]


def _diversify_and_rank(
    per_title_matches: list[tuple[CachedTitle, list[QuoteMatch]]],
    result_limit: int,
) -> list[LibraryQuoteMatch]:
    """Diversity-first ranking shared by the fast path and the fallback path.

    Every title's best-scoring line competes for a results slot before any
    title's second-best line does, but unfilled slots backfill with each
    title's next-best line (up to per_title_limit, already applied by the
    caller) — never at the expense of a better match from another title.
    Implemented by tagging each match with its own per-title rank (0 = that
    title's best line) and sorting globally by (rank, -score), so all
    rank-0 matches sort before any rank-1 match regardless of score, and
    within a rank ties break by score descending.
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

    Both the FTS5 pre-filter and the fallback full scan are scoped to just
    `cached_titles` (resolved to title_ids up front) — never the whole
    corpus. This matters for two reasons: it's what makes /snip-tv's
    whole-show search correct (a global top-N cap could otherwise silently
    exclude some of a show's own episodes — confirmed on the real library:
    only 5 of a 12-episode show's episodes survived an unscoped cap) and
    fast (an unscoped fallback streamed the entire ~7.5M-entry corpus,
    ~9.6s, instead of just the show's own handful of episodes); for
    /search-quote's library-wide search, `cached_titles` already IS the
    full scope the caller wants searched (every cached movie-library
    title), so scoping to it changes nothing about what's searched — it
    just also stops TV episodes indexed via the same shared DB from
    consuming the pre-filter's limited entry budget.

    Entry-level pre-filter, not title-level: search_index.search_entry_ids
    returns up to ~4000 individual surviving FTS5 ENTRY rows (not distinct
    titles — capping to titles let the fuzzy-scoring pass balloon to
    scoring every entry of every survivor title, up to 46% of the real
    corpus). Each surviving entry is expanded into a small adjacent-cue
    window within its own title, multiple windows within the same title are
    merged and re-limited to per_title_limit before ranking, and only those
    windowed slices — not each title's full entry list — get fuzzy-scored.
    See docs/design/fts5-search-migration.md for the full design and
    real-scale numbers.

    FTS5's tokenizer can miss a typo'd or otherwise non-literal query that
    fuzzy matching would still find, so an empty pre-filter result falls
    back to a full scan of every cached title in scope via
    search_index.iter_all_entries rather than silently returning nothing.
    Both paths funnel through _diversify_and_rank so their result ordering
    can never silently diverge.
    """
    normalized_quote = normalize_for_match(quote)
    if not normalized_quote:
        return []
    if not cached_titles:
        return []

    guid_to_cached = {cached.guid: cached for cached in cached_titles}
    guid_to_title_id = search_index.get_title_ids_by_guid(db_path, list(guid_to_cached))
    if not guid_to_title_id:
        return []

    title_id_to_cached = {
        title_id: guid_to_cached[guid] for guid, title_id in guid_to_title_id.items()
    }
    scope_title_ids = list(title_id_to_cached.keys())

    hits = search_index.search_entry_ids(
        db_path,
        normalized_quote.split(),
        limit=_DEFAULT_ENTRY_SCAN_LIMIT,
        title_ids=scope_title_ids,
    )

    per_title_matches: list[tuple[CachedTitle, list[QuoteMatch]]] = []

    if not hits:
        # Fallback: full scan, scoped to just this call's title set.
        for guid, entries in search_index.iter_all_entries(db_path, title_ids=scope_title_ids):
            cached = guid_to_cached.get(guid)
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
        # Group hits by title, and — WITHOUT fetching each title's full
        # entry list — turn each title's hit idx values directly into
        # merged (idx_lo, idx_hi) windows. Only those windows' rows are
        # then fetched in bulk (fetch_entry_windows), not every entry of
        # every survivor title: fetching whole titles here was measured to
        # dominate real-library search time even after the entry-level
        # FTS5 LIMIT already kept the fuzzy-scoring pass itself small.
        pad = _window_pad(context_lines)
        idxs_by_title: dict[int, list[int]] = {}
        for _entry_id, title_id, idx in hits:
            idxs_by_title.setdefault(title_id, []).append(idx)

        ranges_by_title: dict[int, list[tuple[int, int]]] = {}
        windows: list[tuple[int, int, int]] = []
        for title_id, idxs in idxs_by_title.items():
            merged = _merge_ranges([(idx - pad, idx + pad) for idx in idxs])
            ranges_by_title[title_id] = merged
            windows.extend((title_id, lo, hi) for lo, hi in merged)

        entries_by_title = search_index.fetch_entry_windows(db_path, windows)

        for title_id, merged in ranges_by_title.items():
            cached = title_id_to_cached.get(title_id)
            windowed_entries = entries_by_title.get(title_id)
            if cached is None or not windowed_entries:
                continue

            title_matches = _matches_for_title(
                windowed_entries,
                merged,
                quote,
                min_score=min_score,
                max_window_gap_seconds=max_window_gap_seconds,
                context_lines=context_lines,
                per_title_limit=per_title_limit,
            )
            if title_matches:
                per_title_matches.append((cached, title_matches))

    return _diversify_and_rank(per_title_matches, result_limit)
