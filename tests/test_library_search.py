from app.worker import search_index
from app.worker.library_search import _merge_ranges, pick_random_quote, search_cached_library
from app.worker.quote_index import CachedTitle
from app.worker.quotes import find_quote_matches, normalize_for_match
from app.worker.subtitles import SubtitleEntry


def _db_path(tmp_path):
    return tmp_path / "search_index.db"


def _write_title(db_path, guid, rating_key, title, library_name, texts):
    entries = [
        SubtitleEntry(index=i + 1, start=float(i * 5), end=float(i * 5 + 2), text=text)
        for i, text in enumerate(texts)
    ]
    search_index.upsert_title(
        db_path,
        guid=guid,
        rating_key=rating_key,
        title=title,
        library_name=library_name,
        source="sidecar",
        sidecar_path=None,
        stream_index=None,
        entries=entries,
        fingerprint=None,
    )


def test_search_cached_library_returns_best_match_per_title(tmp_path):
    db_path = _db_path(tmp_path)
    _write_title(db_path, "guid-1", 1, "Monty Python", "Movies", ["Nobody expects the Spanish Inquisition!"])
    _write_title(db_path, "guid-2", 2, "Terminator", "Movies", ["I'll be back."])

    cached_titles = [
        CachedTitle(guid="guid-1", rating_key=1, title="Monty Python", library_name="Movies"),
        CachedTitle(guid="guid-2", rating_key=2, title="Terminator", library_name="Movies"),
    ]

    results = search_cached_library(
        db_path,
        cached_titles,
        "nobody expects the spanish inquisition",
        result_limit=8,
        min_score=50.0,
        max_window_gap_seconds=3.0,
        context_lines=1,
    )

    assert len(results) == 1
    assert results[0].rating_key == 1
    assert results[0].title == "Monty Python"


def test_search_cached_library_sorts_by_score_descending(tmp_path):
    # "Weak Match" shares real words with the quote (the/force/with/you) so
    # it's a genuine, if partial, match — not the zero-word-overlap case a
    # real bug (found via a full-library search) used to let sneak into
    # results purely off WRatio's character-level scoring. That class of
    # candidate is now correctly suppressed entirely rather than ranked low.
    db_path = _db_path(tmp_path)
    _write_title(
        db_path, "guid-1", 1, "Weak Match", "Movies",
        ["The Force is strong with this one, but not with you."],
    )
    _write_title(db_path, "guid-2", 2, "Strong Match", "Movies", ["May the Force be with you."])

    cached_titles = [
        CachedTitle(guid="guid-1", rating_key=1, title="Weak Match", library_name="Movies"),
        CachedTitle(guid="guid-2", rating_key=2, title="Strong Match", library_name="Movies"),
    ]

    results = search_cached_library(
        db_path,
        cached_titles,
        "May the Force be with you.",
        result_limit=8,
        min_score=1.0,
        max_window_gap_seconds=3.0,
        context_lines=1,
    )

    assert [r.title for r in results] == ["Strong Match", "Weak Match"]


def test_search_cached_library_respects_result_limit(tmp_path):
    db_path = _db_path(tmp_path)
    for i in range(5):
        _write_title(db_path, f"guid-{i}", i, f"Film {i}", "Movies", ["Same line of dialogue."])

    cached_titles = [
        CachedTitle(guid=f"guid-{i}", rating_key=i, title=f"Film {i}", library_name="Movies")
        for i in range(5)
    ]

    results = search_cached_library(
        db_path,
        cached_titles,
        "same line of dialogue",
        result_limit=2,
        min_score=1.0,
        max_window_gap_seconds=3.0,
        context_lines=1,
    )

    assert len(results) == 2


def test_search_cached_library_skips_missing_cache_entry(tmp_path):
    db_path = _db_path(tmp_path)
    # No titles ever written to the DB at all — search_entry_ids will find
    # nothing (empty DB), triggering the fallback path, which then has
    # nothing to iterate either.
    cached_titles = [
        CachedTitle(guid="ghost-guid", rating_key=1, title="Ghost", library_name="Movies"),
    ]

    results = search_cached_library(
        db_path,
        cached_titles,
        "anything",
        result_limit=8,
        min_score=1.0,
        max_window_gap_seconds=3.0,
        context_lines=1,
    )

    assert results == []


def test_search_cached_library_skips_guid_with_no_matching_cached_title(tmp_path):
    # A title exists in the DB (and its tokens would satisfy the FTS5
    # pre-filter) but the caller's cached_titles list doesn't know about it
    # (e.g. it belongs to a different library scope the caller filtered
    # out) — it must be skipped, not surfaced.
    db_path = _db_path(tmp_path)
    _write_title(db_path, "guid-known", 1, "Known", "Movies", ["a very particular set of skills"])
    _write_title(db_path, "guid-unknown", 2, "Unknown", "Movies", ["a very particular set of skills"])

    cached_titles = [
        CachedTitle(guid="guid-known", rating_key=1, title="Known", library_name="Movies"),
    ]

    results = search_cached_library(
        db_path,
        cached_titles,
        "a very particular set of skills",
        result_limit=8,
        min_score=1.0,
        max_window_gap_seconds=3.0,
        context_lines=1,
    )

    assert [r.title for r in results] == ["Known"]


def test_search_cached_library_fallback_finds_typo_query(tmp_path):
    # "inquisitoin" is a single-word typo that won't appear as an FTS5
    # token in the index (the indexed text has "inquisition"), so
    # search_entry_ids returns [] and the fallback full-scan path must run
    # instead — fuzzy matching (unlike FTS5's exact-token matching) can
    # still find this.
    db_path = _db_path(tmp_path)
    _write_title(db_path, "guid-1", 1, "Monty Python", "Movies", ["Nobody expects the Spanish Inquisition!"])
    _write_title(db_path, "guid-2", 2, "Terminator", "Movies", ["I'll be back."])

    cached_titles = [
        CachedTitle(guid="guid-1", rating_key=1, title="Monty Python", library_name="Movies"),
        CachedTitle(guid="guid-2", rating_key=2, title="Terminator", library_name="Movies"),
    ]

    # Sanity check: this query really does miss the FTS5 pre-filter.
    assert search_index.search_entry_ids(db_path, ["inquisitoin"]) == []

    results = search_cached_library(
        db_path,
        cached_titles,
        "nobody expects the spanish inquisitoin",
        result_limit=8,
        min_score=50.0,
        max_window_gap_seconds=3.0,
        context_lines=1,
    )

    assert len(results) == 1
    assert results[0].rating_key == 1
    assert results[0].title == "Monty Python"


def test_fast_path_and_fallback_path_agree_on_ordering(tmp_path, monkeypatch):
    # Build a corpus where the FTS5 pre-filter genuinely narrows the result
    # set (so the fast path and fallback path take visibly different code
    # routes), then force the fallback path to run over the SAME corpus/
    # query by monkeypatching search_entry_ids to return [] as if nothing
    # had matched. If _diversify_and_rank is truly shared code (not two
    # implementations that happen to agree today), both runs must produce
    # identically ordered results.
    db_path = _db_path(tmp_path)
    _write_title(
        db_path, "guid-1", 1, "Weak Match", "Movies",
        ["The Force is strong with this one, but not with you."],
    )
    _write_title(db_path, "guid-2", 2, "Strong Match", "Movies", ["May the Force be with you."])
    _write_title(db_path, "guid-3", 3, "No Match", "Movies", ["Nobody expects the Spanish Inquisition!"])

    cached_titles = [
        CachedTitle(guid="guid-1", rating_key=1, title="Weak Match", library_name="Movies"),
        CachedTitle(guid="guid-2", rating_key=2, title="Strong Match", library_name="Movies"),
        CachedTitle(guid="guid-3", rating_key=3, title="No Match", library_name="Movies"),
    ]

    kwargs = dict(
        result_limit=8,
        min_score=1.0,
        max_window_gap_seconds=3.0,
        context_lines=1,
    )
    query = "Force be with you"

    # Confirm the FTS5 pre-filter actually narrows the set to just the two
    # titles that share a word with the query (proves the fast path and
    # fallback path are genuinely different routes here, not the same code
    # by coincidence — "No Match" shares no token with the query at all).
    fast_title_ids = search_index.search_entry_ids(db_path, normalize_for_match(query).split())
    assert len(fast_title_ids) < len(cached_titles)

    fast_results = search_cached_library(db_path, cached_titles, query, **kwargs)

    monkeypatch.setattr(search_index, "search_entry_ids", lambda *a, **kw: [])
    fallback_results = search_cached_library(db_path, cached_titles, query, **kwargs)

    assert [(r.rating_key, r.match.score) for r in fast_results] == [
        (r.rating_key, r.match.score) for r in fallback_results
    ]
    assert len(fast_results) > 0


def test_merge_ranges_merges_overlapping_and_touching_but_not_disjoint():
    assert _merge_ranges([]) == []
    assert _merge_ranges([(0, 3)]) == [(0, 3)]
    # overlapping
    assert _merge_ranges([(0, 3), (2, 5)]) == [(0, 5)]
    # touching (lo == previous hi + 1) must still merge, to avoid
    # duplicate boundary entries when two windows are sliced independently
    assert _merge_ranges([(0, 3), (4, 6)]) == [(0, 6)]
    # disjoint (a real gap) must stay separate
    assert _merge_ranges([(0, 3), (10, 12)]) == [(0, 3), (10, 12)]
    # unordered input
    assert _merge_ranges([(10, 12), (0, 3)]) == [(0, 3), (10, 12)]


def test_search_cached_library_scores_a_windowed_slice_not_the_whole_title(tmp_path, monkeypatch):
    # A title with a long subtitle track where only one line, buried deep
    # inside it, contains the search term. Before the entry-level LIMIT fix,
    # the whole title's entries would get fuzzy-scored once the title
    # survived the FTS5 pre-filter at all; now only a small window around
    # the actual hit should ever reach find_quote_matches.
    db_path = _db_path(tmp_path)
    texts = ["irrelevant filler dialogue line"] * 200
    texts[100] = "the treasure is buried under the old oak tree"
    _write_title(db_path, "guid-1", 1, "Long Film", "Movies", texts)

    cached_titles = [
        CachedTitle(guid="guid-1", rating_key=1, title="Long Film", library_name="Movies"),
    ]

    import app.worker.library_search as library_search_module

    seen_slice_lengths = []
    real_find_quote_matches = find_quote_matches

    def spy(entries, *args, **kwargs):
        seen_slice_lengths.append(len(entries))
        return real_find_quote_matches(entries, *args, **kwargs)

    monkeypatch.setattr(library_search_module, "find_quote_matches", spy)

    results = search_cached_library(
        db_path,
        cached_titles,
        "the treasure is buried under the old oak tree",
        result_limit=8,
        min_score=50.0,
        max_window_gap_seconds=3.0,
        context_lines=1,
    )

    assert len(results) == 1
    assert results[0].title == "Long Film"
    # Only ever scored a small window around the hit — never anywhere close
    # to the title's full 200 entries.
    assert seen_slice_lengths
    assert all(n < 20 for n in seen_slice_lengths)


def test_search_cached_library_merges_multiple_hits_in_one_title_before_ranking(tmp_path, monkeypatch):
    # Two separate, far-apart lines in the SAME title both contain the
    # search term — two distinct FTS5 hit entries, two distinct windows.
    # per_title_limit must cap the title's own contribution to the overall
    # results (here to 1) using the merged/re-scored set, not let both
    # windows' matches leak through independently.
    db_path = _db_path(tmp_path)
    texts = ["irrelevant filler dialogue line"] * 200
    texts[10] = "a shared unique keyword phrase right here"
    texts[150] = "a shared unique keyword phrase right here too"
    _write_title(db_path, "guid-1", 1, "Long Film", "Movies", texts)

    cached_titles = [
        CachedTitle(guid="guid-1", rating_key=1, title="Long Film", library_name="Movies"),
    ]

    import app.worker.library_search as library_search_module

    seen_slice_lengths = []
    real_find_quote_matches = find_quote_matches

    def spy(entries, *args, **kwargs):
        seen_slice_lengths.append(len(entries))
        return real_find_quote_matches(entries, *args, **kwargs)

    monkeypatch.setattr(library_search_module, "find_quote_matches", spy)

    results = search_cached_library(
        db_path,
        cached_titles,
        "a shared unique keyword phrase right here",
        result_limit=8,
        min_score=50.0,
        max_window_gap_seconds=3.0,
        context_lines=1,
        per_title_limit=1,
    )

    # Two disjoint windows were scored (not one giant 200-entry scan)...
    assert len(seen_slice_lengths) == 2
    assert all(n < 20 for n in seen_slice_lengths)
    # ...but the title contributes only per_title_limit=1 result overall.
    assert len(results) == 1
    assert results[0].title == "Long Film"


def test_search_cached_library_scopes_fts_prefilter_to_cached_titles(tmp_path, monkeypatch):
    # Simulates /snip tv's whole-show search: cached_titles is deliberately
    # narrow (one "show's episodes"), while the DB also holds an unrelated
    # title outside that scope with a much better match for the same
    # query. The scoped search must never surface the out-of-scope title,
    # and the FTS5 pre-filter itself must be called with the scoped
    # title_ids (not None / unscoped).
    db_path = _db_path(tmp_path)
    _write_title(db_path, "guid-ep1", 1, "Show S01E01", "TV Shows", ["nothing quotable here"])
    _write_title(db_path, "guid-ep2", 2, "Show S01E02", "TV Shows", ["that's what she said"])
    # Outside the show's scope, but a stronger match for the same quote.
    _write_title(db_path, "guid-other", 99, "Unrelated Movie", "Movies", ["that's what she said, exactly"])

    cached_titles = [
        CachedTitle(guid="guid-ep1", rating_key=1, title="Show S01E01", library_name="TV Shows"),
        CachedTitle(guid="guid-ep2", rating_key=2, title="Show S01E02", library_name="TV Shows"),
    ]

    import app.worker.library_search as library_search_module

    real_search_entry_ids = search_index.search_entry_ids
    seen_title_ids_args = []

    def spy(db_path, tokens, **kwargs):
        seen_title_ids_args.append(kwargs.get("title_ids"))
        return real_search_entry_ids(db_path, tokens, **kwargs)

    monkeypatch.setattr(library_search_module.search_index, "search_entry_ids", spy)

    results = search_cached_library(
        db_path,
        cached_titles,
        "that's what she said",
        result_limit=8,
        min_score=50.0,
        max_window_gap_seconds=3.0,
        context_lines=1,
    )

    assert [r.title for r in results] == ["Show S01E02"]
    assert seen_title_ids_args
    scoped = seen_title_ids_args[0]
    assert scoped is not None
    assert set(scoped) != set()
    # The out-of-scope title's title_id must not be part of the scope.
    with search_index._connect(db_path) as conn:
        other_id = conn.execute(
            "SELECT title_id FROM titles WHERE guid = 'guid-other'"
        ).fetchone()[0]
    assert other_id not in scoped


def test_search_cached_library_fallback_scoped_to_cached_titles(tmp_path, monkeypatch):
    # Force the fallback path (typo'd query that misses the FTS5
    # pre-filter entirely) and confirm the full scan is scoped to just the
    # caller's cached_titles, not the whole DB — an out-of-scope title with
    # a fuzzy-matchable typo'd quote must not appear in results, and
    # iter_all_entries must have been called with the scoped title_ids.
    db_path = _db_path(tmp_path)
    _write_title(db_path, "guid-ep1", 1, "Show S01E01", "TV Shows", ["that's what she saidd"])
    _write_title(db_path, "guid-other", 99, "Unrelated Movie", "Movies", ["that's what she saidd"])

    cached_titles = [
        CachedTitle(guid="guid-ep1", rating_key=1, title="Show S01E01", library_name="TV Shows"),
    ]

    import app.worker.library_search as library_search_module

    real_iter_all_entries = search_index.iter_all_entries
    seen_title_ids_args = []

    def spy(db_path, **kwargs):
        seen_title_ids_args.append(kwargs.get("title_ids"))
        return real_iter_all_entries(db_path, **kwargs)

    monkeypatch.setattr(library_search_module.search_index, "iter_all_entries", spy)
    # Force the fallback: pretend the FTS5 pre-filter found nothing.
    monkeypatch.setattr(library_search_module.search_index, "search_entry_ids", lambda *a, **kw: [])

    results = search_cached_library(
        db_path,
        cached_titles,
        "that's what she said",
        result_limit=8,
        min_score=50.0,
        max_window_gap_seconds=3.0,
        context_lines=1,
    )

    assert [r.title for r in results] == ["Show S01E01"]
    assert seen_title_ids_args
    with search_index._connect(db_path) as conn:
        ep1_id, other_id = (
            conn.execute("SELECT title_id FROM titles WHERE guid = ?", (g,)).fetchone()[0]
            for g in ("guid-ep1", "guid-other")
        )
    scoped = seen_title_ids_args[0]
    assert scoped == [ep1_id]
    assert other_id not in scoped


def test_pick_random_quote_returns_none_for_no_cached_titles(tmp_path):
    db_path = _db_path(tmp_path)
    assert pick_random_quote(db_path, [], quote=None) is None


def test_pick_random_quote_without_quote_picks_a_cached_line(tmp_path):
    db_path = _db_path(tmp_path)
    _write_title(db_path, "guid-1", 1, "Monty Python", "Movies", ["Nobody expects the Spanish Inquisition!"])

    cached_titles = [CachedTitle(guid="guid-1", rating_key=1, title="Monty Python", library_name="Movies")]

    result = pick_random_quote(db_path, cached_titles, quote=None)

    assert result is not None
    assert result.rating_key == 1
    assert result.match.text == "Nobody expects the Spanish Inquisition!"


def test_pick_random_quote_with_quote_only_returns_whole_word_matches(tmp_path):
    # "cat" must match the literal-word entry, never the entry that merely
    # shares letters as a substring inside another word ("concatenated") —
    # mirrors find_quote_matches' word-boundary test
    # (test_literal_match_word_boundary_does_not_match_inside_another_word).
    db_path = _db_path(tmp_path)
    _write_title(db_path, "guid-1", 1, "Cat Film", "Movies", ["The cat sat on the mat."])
    _write_title(db_path, "guid-2", 2, "Unrelated Film", "Movies", ["The file was concatenated."])

    cached_titles = [
        CachedTitle(guid="guid-1", rating_key=1, title="Cat Film", library_name="Movies"),
        CachedTitle(guid="guid-2", rating_key=2, title="Unrelated Film", library_name="Movies"),
    ]

    for _ in range(10):
        result = pick_random_quote(db_path, cached_titles, quote="cat")
        assert result is not None
        assert result.rating_key == 1


def test_pick_random_quote_with_quote_returns_none_when_no_whole_word_match(tmp_path):
    db_path = _db_path(tmp_path)
    _write_title(db_path, "guid-1", 1, "Unrelated Film", "Movies", ["The file was concatenated."])

    cached_titles = [CachedTitle(guid="guid-1", rating_key=1, title="Unrelated Film", library_name="Movies")]

    assert pick_random_quote(db_path, cached_titles, quote="cat") is None
