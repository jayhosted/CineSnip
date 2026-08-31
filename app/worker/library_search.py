from __future__ import annotations

import random
from dataclasses import dataclass
from pathlib import Path

from app.worker import search_index
from app.worker.quote_index import CachedTitle
from app.worker.quotes import QuoteMatch, find_quote_matches, normalize_for_match, strip_markup
from app.worker.subtitles import SubtitleEntry

# Below this many words (after strip_markup), a line is excluded from a
# "filtered random" pick (movie/tv random-with-no-quote) — keeps a random
# pick from landing on filler like "Okay." or "Yeah.". Not applied to
# /snip random's own pre-existing no-quote/with-quote paths (min_words=1
# there), only to the new per-title/per-show random flow.
_DEFAULT_RANDOM_MIN_WORDS = 1

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
    media_id: str
    title: str
    library_name: str
    match: QuoteMatch
    # The underlying entries.id primary key for this match's first cue —
    # only populated by pick_random_quote (search_cached_library's ranked
    # results have no use for it). Serves as an opaque per-pick identity a
    # caller can echo back as exclude_entry_ids/most_recent_entry_id on a
    # reroll, so a shuffle journey never repeats a line it's already shown.
    entry_id: int | None = None


@dataclass(frozen=True)
class RandomPick:
    pick: LibraryQuoteMatch
    # Size of the eligible candidate pool for this exact query scope +
    # quality filter, ignoring exclude_entry_ids — lets a caller detect a
    # single-match pool up front (CLAUDE.md's "Celina" fix: disable Shuffle
    # and say so, rather than leaving a button that looks broken).
    pool_size: int
    # True if every eligible candidate had already been excluded and the
    # pool had to reset to produce this pick (still avoiding an immediate
    # repeat of most_recent_entry_id where possible).
    exhausted: bool


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
                        media_id=cached.media_id,
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
    corpus. This matters for two reasons: it's what makes /snip tv's
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


def _resolve_pool_pick(
    pool: list[LibraryQuoteMatch],
    exclude_entry_ids: frozenset[int],
    most_recent_entry_id: int | None,
) -> RandomPick | None:
    """Shared exclusion/exhaustion-reset logic for both pick_random_quote
    branches, once each has materialized its candidate pool as a list of
    LibraryQuoteMatch (each carrying its own entry_id). Fixes the "Celina"
    bug: a narrow pool must not repeat an entry already shown this
    reroll journey, and once every candidate has been excluded, the pool
    resets (still dodging an immediate repeat of most_recent_entry_id)
    rather than returning nothing.
    """
    if not pool:
        return None

    pool_size = len(pool)
    eligible = [m for m in pool if m.entry_id not in exclude_entry_ids]
    exhausted = False
    if not eligible:
        exhausted = True
        eligible = [m for m in pool if m.entry_id != most_recent_entry_id]
        if not eligible:
            eligible = pool

    return RandomPick(pick=random.choice(eligible), pool_size=pool_size, exhausted=exhausted)


def pick_random_quote(
    db_path: Path,
    cached_titles: list[CachedTitle],
    quote: str | None,
    exclude_entry_ids: frozenset[int] = frozenset(),
    most_recent_entry_id: int | None = None,
    min_words: int = _DEFAULT_RANDOM_MIN_WORDS,
    max_window_gap_seconds: float = 3.0,
    context_lines: int = 1,
) -> RandomPick | None:
    """Pick one random cached line, scoped to `cached_titles` (the caller's
    already-resolved media-type filter, same convention as
    search_cached_library above). Used by /snip random and by the
    per-title/per-show random flow (/snip movie, /snip tv with no
    quote/timecode given).

    Without a quote, picks a genuinely random cached entry (optionally
    filtered by min_words — see _DEFAULT_RANDOM_MIN_WORDS).

    With a quote, restricts to WHOLE-WORD matches only (find_quote_matches'
    literal-substring tier, which is force-scored to exactly 100.0 — see
    quotes.py) rather than any fuzzy-only match, then picks randomly among
    those literal hits instead of returning the top-ranked one. min_score=
    100.0 is what selects literal-only: the partial-word-overlap bonus tier
    tops out at 95.0, so it can never leak in here.

    `exclude_entry_ids`/`most_recent_entry_id` let a caller track a reroll
    journey's history — see _resolve_pool_pick.
    """
    if not cached_titles:
        return None

    guid_to_cached = {cached.guid: cached for cached in cached_titles}
    guid_to_title_id = search_index.get_title_ids_by_guid(db_path, list(guid_to_cached))
    if not guid_to_title_id:
        return None

    title_id_to_cached = {
        title_id: guid_to_cached[guid] for guid, title_id in guid_to_title_id.items()
    }
    scope_title_ids = list(title_id_to_cached.keys())

    if quote is None:
        if min_words <= 1:
            # Efficient SQL-only path — scales to a whole-library scope
            # without fetching every candidate's text into Python.
            pool_size = search_index.count_entries(db_path, scope_title_ids)
            if pool_size == 0:
                return None
            picked = search_index.pick_random_entry_id(
                db_path, scope_title_ids, exclude_entry_ids=exclude_entry_ids
            )
            exhausted = False
            if picked is None:
                exhausted = True
                retry_exclude = (
                    frozenset({most_recent_entry_id})
                    if most_recent_entry_id is not None
                    else frozenset()
                )
                picked = search_index.pick_random_entry_id(
                    db_path, scope_title_ids, exclude_entry_ids=retry_exclude
                )
                if picked is None:
                    picked = search_index.pick_random_entry_id(db_path, scope_title_ids)
            if picked is None:
                return None
            entry_id, title_id, idx = picked
            cached = title_id_to_cached[title_id]
            windowed = search_index.fetch_entry_windows(db_path, [(title_id, idx, idx)])
            windowed_entries = windowed.get(title_id)
            if not windowed_entries:
                return None
            _, entry = windowed_entries[0]
            match = QuoteMatch(
                start=entry.start,
                end=entry.end,
                text=strip_markup(entry.text),
                score=0.0,
                entry_indices=(entry.index,),
                context_before=(),
                context_after=(),
            )
            pick = LibraryQuoteMatch(
                media_id=cached.media_id,
                title=cached.title,
                library_name=cached.library_name,
                match=match,
                entry_id=entry_id,
            )
            return RandomPick(pick=pick, pool_size=pool_size, exhausted=exhausted)

        # Quality-filtered path (movie/tv random): scope is always a single
        # title or one show's episodes, small enough to fetch in full.
        rows = search_index.list_entry_rows_for_titles(db_path, scope_title_ids)
        pool = []
        for entry_id, title_id, idx, start, end, display_text in rows:
            text = strip_markup(display_text)
            if len(text.split()) < min_words:
                continue
            cached = title_id_to_cached.get(title_id)
            if cached is None:
                continue
            match = QuoteMatch(
                start=start,
                end=end,
                text=text,
                score=0.0,
                entry_indices=(idx,),
                context_before=(),
                context_after=(),
            )
            pool.append(
                LibraryQuoteMatch(
                    media_id=cached.media_id,
                    title=cached.title,
                    library_name=cached.library_name,
                    match=match,
                    entry_id=entry_id,
                )
            )
        return _resolve_pool_pick(pool, exclude_entry_ids, most_recent_entry_id)

    normalized_quote = normalize_for_match(quote)
    if not normalized_quote:
        return None

    hits = search_index.search_entry_ids(
        db_path,
        normalized_quote.split(),
        limit=_DEFAULT_ENTRY_SCAN_LIMIT,
        title_ids=scope_title_ids,
    )
    if not hits:
        return None

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

    pool: list[LibraryQuoteMatch] = []
    for title_id, merged in ranges_by_title.items():
        cached = title_id_to_cached.get(title_id)
        windowed_entries = entries_by_title.get(title_id)
        if cached is None or not windowed_entries:
            continue
        for lo, hi in merged:
            # find_quote_matches' entry_indices are positions within
            # whatever list it's given (slice_entries), NOT the absolute
            # SubtitleEntry.index — so entry_id lookup must go through the
            # parallel slice_entry_ids list, not windowed_entries directly.
            slice_pairs = [(eid, entry) for eid, entry in windowed_entries if lo <= entry.index <= hi]
            if not slice_pairs:
                continue
            slice_entry_ids = [eid for eid, _entry in slice_pairs]
            slice_entries = [entry for _eid, entry in slice_pairs]
            literal_matches = find_quote_matches(
                slice_entries,
                quote,
                limit=len(slice_entries),
                min_score=100.0,
                max_window_gap_seconds=max_window_gap_seconds,
                context_lines=context_lines,
            )
            for match in literal_matches:
                pool.append(
                    LibraryQuoteMatch(
                        media_id=cached.media_id,
                        title=cached.title,
                        library_name=cached.library_name,
                        match=match,
                        entry_id=slice_entry_ids[match.entry_indices[0]],
                    )
                )

    return _resolve_pool_pick(pool, exclude_entry_ids, most_recent_entry_id)
