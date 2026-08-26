from app.worker.library_search import search_cached_library
from app.worker.quote_index import CachedTitle
from app.worker.subtitles import (
    SubtitleEntry,
    SubtitleResult,
    SubtitleSource,
    write_cached_subtitles,
)


def _write_title(cache_dir, guid, texts):
    entries = [
        SubtitleEntry(index=i + 1, start=float(i * 5), end=float(i * 5 + 2), text=text)
        for i, text in enumerate(texts)
    ]
    write_cached_subtitles(
        cache_dir,
        SubtitleResult(guid=guid, source=SubtitleSource.SIDECAR, entries=entries),
    )


def test_search_cached_library_returns_best_match_per_title(tmp_path):
    _write_title(tmp_path, "guid-1", ["Nobody expects the Spanish Inquisition!"])
    _write_title(tmp_path, "guid-2", ["I'll be back."])

    cached_titles = [
        CachedTitle(guid="guid-1", rating_key=1, title="Monty Python", library_name="Movies"),
        CachedTitle(guid="guid-2", rating_key=2, title="Terminator", library_name="Movies"),
    ]

    results = search_cached_library(
        tmp_path,
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
    _write_title(tmp_path, "guid-1", ["Something else entirely, roughly similar in length"])
    _write_title(tmp_path, "guid-2", ["May the Force be with you."])

    cached_titles = [
        CachedTitle(guid="guid-1", rating_key=1, title="Weak Match", library_name="Movies"),
        CachedTitle(guid="guid-2", rating_key=2, title="Strong Match", library_name="Movies"),
    ]

    results = search_cached_library(
        tmp_path,
        cached_titles,
        "May the Force be with you.",
        result_limit=8,
        min_score=1.0,
        max_window_gap_seconds=3.0,
        context_lines=1,
    )

    assert [r.title for r in results] == ["Strong Match", "Weak Match"]


def test_search_cached_library_respects_result_limit(tmp_path):
    for i in range(5):
        _write_title(tmp_path, f"guid-{i}", ["Same line of dialogue."])

    cached_titles = [
        CachedTitle(guid=f"guid-{i}", rating_key=i, title=f"Film {i}", library_name="Movies")
        for i in range(5)
    ]

    results = search_cached_library(
        tmp_path,
        cached_titles,
        "same line of dialogue",
        result_limit=2,
        min_score=1.0,
        max_window_gap_seconds=3.0,
        context_lines=1,
    )

    assert len(results) == 2


def test_search_cached_library_skips_missing_cache_entry(tmp_path):
    cached_titles = [
        CachedTitle(guid="ghost-guid", rating_key=1, title="Ghost", library_name="Movies"),
    ]

    results = search_cached_library(
        tmp_path,
        cached_titles,
        "anything",
        result_limit=8,
        min_score=1.0,
        max_window_gap_seconds=3.0,
        context_lines=1,
    )

    assert results == []
