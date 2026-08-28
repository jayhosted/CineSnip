from app.worker import search_index
from app.worker.library_search import search_cached_library
from app.worker.quote_index import CachedTitle
from app.worker.quotes import normalize_for_match
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
    # No titles ever written to the DB at all — search_title_ids will find
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
    # search_title_ids returns [] and the fallback full-scan path must run
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
    assert search_index.search_title_ids(db_path, ["inquisitoin"]) == []

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
    # query by monkeypatching search_title_ids to return [] as if nothing
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
    fast_title_ids = search_index.search_title_ids(db_path, normalize_for_match(query).split())
    assert len(fast_title_ids) < len(cached_titles)

    fast_results = search_cached_library(db_path, cached_titles, query, **kwargs)

    monkeypatch.setattr(search_index, "search_title_ids", lambda *a, **kw: [])
    fallback_results = search_cached_library(db_path, cached_titles, query, **kwargs)

    assert [(r.rating_key, r.match.score) for r in fast_results] == [
        (r.rating_key, r.match.score) for r in fallback_results
    ]
    assert len(fast_results) > 0
