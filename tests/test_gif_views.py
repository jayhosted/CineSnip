import asyncio
from unittest.mock import AsyncMock

import discord

from app.bot.cogs.gif import _PAGE_SIZE, QuoteMatchView, RandomResultView
from app.bot.worker_client import QuoteMatchResult, RandomQuoteResult


def _quote_match(i: int, score: float = 60.0) -> QuoteMatchResult:
    return QuoteMatchResult(
        start=float(i),
        end=float(i) + 2.0,
        timecode=f"0:{i:02d}",
        text=f"Line {i}",
        score=score,
        entry_indices=[i],
        context_before=[],
        context_after=[],
    )


def _fake_interaction() -> AsyncMock:
    interaction = AsyncMock()
    interaction.response.edit_message = AsyncMock()
    return interaction


def test_quote_match_view_timeout_gives_time_to_browse_multiple_pages():
    # Issue #7 follow-up: 120s was calibrated for scanning 8 results — with
    # up to 50 now possible across several pages, that timed out too fast
    # mid-browse.
    matches = [_quote_match(i) for i in range(20)]
    view = QuoteMatchView("Title", matches, min_score=50.0, confident_score=85.0)

    assert view.timeout == 600


def test_quote_match_view_below_confident_score_opens_first_page_select():
    matches = [_quote_match(i, score=60.0) for i in range(20)]
    view = QuoteMatchView("Title", matches, min_score=50.0, confident_score=85.0)

    assert view._select is not None
    assert [opt.value for opt in view._select.options] == [str(i) for i in range(_PAGE_SIZE)]
    assert view._prev_button.disabled is True
    assert view._next_button.disabled is False


def test_quote_match_view_confident_top_match_still_opens_select_immediately():
    # Issue #7 follow-up: with true pagination available, hiding the picker
    # behind a "Show other matches" button just adds a click — the picker
    # (and its Next/Previous paging) is shown immediately regardless of
    # the top match's score.
    matches = [_quote_match(0, score=95.0)] + [_quote_match(i, score=60.0) for i in range(1, 20)]
    view = QuoteMatchView("Title", matches, min_score=50.0, confident_score=85.0)

    assert view._select is not None
    assert len(view._select.options) == _PAGE_SIZE
    assert view._next_button.disabled is False
    labels = [item.label for item in view.children if hasattr(item, "label")]
    assert "Show other matches" not in labels


def test_quote_match_view_next_then_previous_round_trips():
    matches = [_quote_match(i, score=60.0) for i in range(20)]
    view = QuoteMatchView("Title", matches, min_score=50.0, confident_score=85.0)

    asyncio.run(view._on_next(_fake_interaction()))
    assert view._page == 1
    assert [opt.value for opt in view._select.options] == [str(i) for i in range(8, 16)]
    assert view._prev_button.disabled is False
    assert view._next_button.disabled is False

    asyncio.run(view._on_next(_fake_interaction()))
    assert view._page == 2
    assert [opt.value for opt in view._select.options] == [str(i) for i in range(16, 20)]
    assert view._next_button.disabled is True

    asyncio.run(view._on_previous(_fake_interaction()))
    assert view._page == 1
    assert view._next_button.disabled is False


def test_quote_match_view_select_on_second_page_resolves_absolute_index():
    matches = [_quote_match(i, score=60.0) for i in range(20)]
    view = QuoteMatchView("Title", matches, min_score=50.0, confident_score=85.0)

    asyncio.run(view._on_next(_fake_interaction()))
    view._select._values = ["10"]

    asyncio.run(view._on_select(_fake_interaction()))

    assert view.index == 10
    assert view.selected.text == "Line 10"


def test_quote_match_view_no_page_buttons_when_batch_fits_one_page():
    matches = [_quote_match(i, score=60.0) for i in range(8)]
    view = QuoteMatchView("Title", matches, min_score=50.0, confident_score=85.0)

    assert view._prev_button is None
    assert view._next_button is None


def test_quote_match_view_next_past_last_page_is_a_noop():
    matches = [_quote_match(i, score=60.0) for i in range(20)]  # 3 pages: 0,1,2
    view = QuoteMatchView("Title", matches, min_score=50.0, confident_score=85.0)

    asyncio.run(view._on_next(_fake_interaction()))
    asyncio.run(view._on_next(_fake_interaction()))
    assert view._page == 2
    # Simulated raced double-click: a third Next past the last page.
    asyncio.run(view._on_next(_fake_interaction()))
    assert view._page == 2
    assert len(view._select.options) > 0


def test_quote_match_view_previous_before_first_page_is_a_noop():
    matches = [_quote_match(i, score=60.0) for i in range(20)]
    view = QuoteMatchView("Title", matches, min_score=50.0, confident_score=85.0)

    asyncio.run(view._on_previous(_fake_interaction()))
    assert view._page == 0
    assert len(view._select.options) > 0


def test_quote_match_view_footer_shows_page_of_pages_not_raw_count():
    matches = [_quote_match(i, score=60.0) for i in range(20)]  # 3 pages
    view = QuoteMatchView("Title", matches, min_score=50.0, confident_score=85.0)

    assert view.embed().footer.text == "Page 1 of 3"

    asyncio.run(view._on_next(_fake_interaction()))
    assert view.embed().footer.text == "Page 2 of 3"


def test_quote_match_view_footer_omitted_when_batch_fits_one_page():
    matches = [_quote_match(i, score=60.0) for i in range(3)]
    view = QuoteMatchView("Title", matches, min_score=50.0, confident_score=85.0)

    assert view.embed().footer.text is None


def test_quote_match_view_truncated_note_only_on_last_page():
    matches = [_quote_match(i, score=60.0) for i in range(20)]  # 3 pages
    view = QuoteMatchView(
        "Title", matches, min_score=50.0, confident_score=85.0, truncated=True
    )

    assert view.embed().footer.text == "Page 1 of 3"

    asyncio.run(view._on_next(_fake_interaction()))
    assert view.embed().footer.text == "Page 2 of 3"

    asyncio.run(view._on_next(_fake_interaction()))
    assert "more results may exist" in view.embed().footer.text
    assert view.embed().footer.text.startswith("Page 3 of 3")


def test_quote_match_view_no_truncated_note_when_not_truncated():
    matches = [_quote_match(i, score=60.0) for i in range(20)]
    view = QuoteMatchView(
        "Title", matches, min_score=50.0, confident_score=85.0, truncated=False
    )

    asyncio.run(view._on_next(_fake_interaction()))
    asyncio.run(view._on_next(_fake_interaction()))
    assert view.embed().footer.text == "Page 3 of 3"


def test_quote_match_view_component_rows_keep_pagination_off_cancel_row():
    matches = [_quote_match(i, score=60.0) for i in range(20)]
    view = QuoteMatchView("Title", matches, min_score=50.0, confident_score=85.0)

    rows = {item.label: item.row for item in view.children if hasattr(item, "label")}
    assert rows["Confirm"] == 0
    assert rows["Cancel"] == 0
    assert rows["◀ Previous"] == 2
    assert rows["Next ▶"] == 2
    assert view._select.row == 1


from app.bot.cogs.gif import LibrarySearchView, _library_results_embed
from app.bot.worker_client import LibraryQuoteMatchResult


def _library_match(i: int, score: float = 60.0) -> LibraryQuoteMatchResult:
    return LibraryQuoteMatchResult(
        rating_key=i,
        title=f"Title {i}",
        library_name="Movies",
        start=float(i),
        end=float(i) + 2.0,
        timecode=f"0:{i:02d}",
        text=f"Line {i}",
        score=score,
        context_before=[],
        context_after=[],
    )


def test_quote_match_view_uses_fixed_title_when_given():
    matches = [_quote_match(i) for i in range(3)]
    view = QuoteMatchView("Fixed Title", matches, min_score=50.0, confident_score=85.0)

    assert view.embed().title == "Fixed Title"


def test_quote_match_view_uses_per_match_title_when_title_is_none():
    # /snip tv's whole-show search (issue #7 follow-up): candidates can come
    # from different episodes, so there's no single fixed title to show —
    # title=None means "read it off whichever match is currently selected".
    matches = [_library_match(i) for i in range(3)]
    view = QuoteMatchView(None, matches, min_score=50.0, confident_score=85.0)

    assert view.embed().title == matches[0].title


def test_quote_match_view_per_match_title_follows_selection():
    matches = [_library_match(i) for i in range(3)]
    view = QuoteMatchView(None, matches, min_score=50.0, confident_score=85.0)

    view._select._values = ["2"]
    asyncio.run(view._on_select(_fake_interaction()))

    assert view.embed().title == matches[2].title


class _FakeCog:
    async def _run_library_search(self, interaction, quote):
        raise AssertionError("not exercised by these tests")

    async def _generate(self, interaction, film, quote, timecode, end_timecode, format, preferred_start=None):
        self.generated = (film, preferred_start)


def test_library_search_view_timeout_gives_time_to_browse_multiple_pages():
    matches = [_library_match(i) for i in range(20)]
    view = LibrarySearchView(_FakeCog(), "quote", matches)

    assert view.timeout == 600


def test_library_search_view_first_page_has_eight_options_and_next_enabled():
    matches = [_library_match(i) for i in range(20)]
    view = LibrarySearchView(_FakeCog(), "quote", matches)

    assert len(view._select.options) == _PAGE_SIZE
    assert [opt.value for opt in view._select.options] == [str(i) for i in range(8)]
    assert view._prev_button.disabled is True
    assert view._next_button.disabled is False


def test_library_search_view_embed_shows_only_current_page_with_footer():
    matches = [_library_match(i) for i in range(20)]
    view = LibrarySearchView(_FakeCog(), "quote", matches)

    embed = view.embed()
    assert len(embed.fields) == _PAGE_SIZE
    assert embed.fields[0].name.startswith("1. ")
    assert embed.footer.text == "Page 1 of 3"


def test_library_search_view_next_shows_second_page():
    matches = [_library_match(i) for i in range(20)]
    view = LibrarySearchView(_FakeCog(), "quote", matches)

    asyncio.run(view._on_next(_fake_interaction()))

    assert [opt.value for opt in view._select.options] == [str(i) for i in range(8, 16)]
    embed = view.embed()
    assert embed.fields[0].name.startswith("9. ")
    assert embed.footer.text == "Page 2 of 3"
    assert view._next_button.disabled is False

    asyncio.run(view._on_next(_fake_interaction()))
    assert view._next_button.disabled is True


def test_library_search_view_no_page_buttons_or_footer_when_batch_fits_one_page():
    matches = [_library_match(i) for i in range(5)]
    view = LibrarySearchView(_FakeCog(), "quote", matches)

    assert view._prev_button is None
    assert view._next_button is None
    assert view.embed().footer.text is None


def test_library_search_view_select_on_second_page_resolves_absolute_match():
    matches = [_library_match(i) for i in range(20)]
    cog = _FakeCog()
    view = LibrarySearchView(cog, "quote", matches)

    asyncio.run(view._on_next(_fake_interaction()))
    view._select._values = ["10"]
    asyncio.run(view._on_select(_fake_interaction()))

    assert cog.generated == (str(10), 10.0)


def test_library_results_embed_footer_omitted_for_single_page():
    matches = [_library_match(i) for i in range(3)]
    embed = _library_results_embed("quote", matches)
    assert embed.footer.text is None


def test_library_search_view_truncated_note_only_on_last_page():
    matches = [_library_match(i) for i in range(20)]  # 3 pages
    view = LibrarySearchView(_FakeCog(), "quote", matches, truncated=True)

    assert view.embed().footer.text == "Page 1 of 3"

    asyncio.run(view._on_next(_fake_interaction()))
    assert view.embed().footer.text == "Page 2 of 3"

    asyncio.run(view._on_next(_fake_interaction()))
    assert "more results may exist" in view.embed().footer.text
    assert view.embed().footer.text.startswith("Page 3 of 3")


def test_library_search_view_no_truncated_note_when_not_truncated():
    matches = [_library_match(i) for i in range(20)]
    view = LibrarySearchView(_FakeCog(), "quote", matches, truncated=False)

    asyncio.run(view._on_next(_fake_interaction()))
    asyncio.run(view._on_next(_fake_interaction()))
    assert view.embed().footer.text == "Page 3 of 3"


def test_library_search_view_next_past_last_page_is_a_noop():
    matches = [_library_match(i) for i in range(20)]
    view = LibrarySearchView(_FakeCog(), "quote", matches)

    asyncio.run(view._on_next(_fake_interaction()))
    asyncio.run(view._on_next(_fake_interaction()))
    asyncio.run(view._on_next(_fake_interaction()))
    assert view._page == 2
    assert len(view._select.options) > 0


def test_library_search_view_previous_before_first_page_is_a_noop():
    matches = [_library_match(i) for i in range(20)]
    view = LibrarySearchView(_FakeCog(), "quote", matches)

    asyncio.run(view._on_previous(_fake_interaction()))
    assert view._page == 0
    assert len(view._select.options) > 0


def test_library_search_view_component_rows_stable_across_page_change():
    matches = [_library_match(i) for i in range(20)]
    view = LibrarySearchView(_FakeCog(), "quote", matches, remaining_uncached=5)

    def rows():
        return {
            item.label if hasattr(item, "label") else "select": item.row
            for item in view.children
        }

    before = rows()
    assert before["select"] == 0
    assert before["◀ Previous"] == 1
    assert before["Next ▶"] == 1
    assert before["🔍 Search 5 more"] == 2

    asyncio.run(view._on_next(_fake_interaction()))

    after = rows()
    # Finding 3: the "Search N more" button must keep its own row after the
    # prev/next buttons are torn down and rebuilt on a page change.
    assert after == before


def _random_pick(
    rating_key=1, title="Film", start=0.0, end=2.0, text="Line",
    entry_id=1, pool_size=5, exhausted=False,
) -> RandomQuoteResult:
    return RandomQuoteResult(
        rating_key=rating_key, title=title, library_name="Movies",
        start=start, end=end, timecode=f"0:{int(start):02d}", text=text,
        entry_id=entry_id, pool_size=pool_size, exhausted=exhausted,
    )


class _FakeRenderResult:
    def __init__(self, content: bytes = b"clip-bytes", format: str = "gif") -> None:
        self.content = content
        self.format = format


class _FakeWorker:
    def __init__(self) -> None:
        self.render = AsyncMock(return_value=_FakeRenderResult())


class _FakeFetch:
    """Records every call's (exclude, most_recent) args and returns picks
    from a preset queue, one per call."""

    def __init__(self, picks: list[RandomQuoteResult]) -> None:
        self._picks = list(picks)
        self.calls: list[tuple[frozenset, int | None]] = []

    async def __call__(self, exclude: frozenset[int], most_recent: int | None) -> RandomQuoteResult:
        self.calls.append((exclude, most_recent))
        return self._picks.pop(0)


def test_random_result_view_disables_shuffle_when_pool_size_is_one():
    view = RandomResultView(_FakeWorker(), _FakeFetch([]), _random_pick(pool_size=1), b"x", "clip.gif")

    assert view.shuffle.disabled is True


def test_random_result_view_enables_shuffle_when_pool_has_more_than_one():
    view = RandomResultView(_FakeWorker(), _FakeFetch([]), _random_pick(pool_size=5), b"x", "clip.gif")

    assert view.shuffle.disabled is False


def test_random_result_view_button_order_previous_shuffle_post():
    worker = _FakeWorker()
    fetch = _FakeFetch([_random_pick(entry_id=2, text="Second", pool_size=5)])
    view = RandomResultView(worker, fetch, _random_pick(entry_id=1, text="First", pool_size=5), b"x", "clip.gif")

    # Before the first shuffle, Previous doesn't exist yet — Shuffle then
    # Post, with Post last.
    assert [item.label for item in view.children] == ["🔀 Shuffle", "Post to channel"]

    asyncio.run(view.shuffle.callback(_fake_interaction()))

    # Previous belongs on the left, Post always stays last.
    assert [item.label for item in view.children] == ["◀ Previous", "🔀 Shuffle", "Post to channel"]


def test_random_result_view_shuffle_adds_previous_button_and_passes_exclusion():
    worker = _FakeWorker()
    fetch = _FakeFetch([_random_pick(entry_id=2, text="Second", pool_size=5)])
    view = RandomResultView(worker, fetch, _random_pick(entry_id=1, text="First", pool_size=5), b"x", "clip.gif")
    assert view._previous_button not in view.children

    asyncio.run(view.shuffle.callback(_fake_interaction()))

    assert fetch.calls == [(frozenset({1}), 1)]
    assert view._previous_button in view.children
    assert view._previous_button.disabled is False
    assert view._current.pick.text == "Second"


def test_random_result_view_previous_steps_back_without_refetching():
    worker = _FakeWorker()
    fetch = _FakeFetch([_random_pick(entry_id=2, text="Second", pool_size=5)])
    view = RandomResultView(worker, fetch, _random_pick(entry_id=1, text="First", pool_size=5), b"x", "clip.gif")
    asyncio.run(view.shuffle.callback(_fake_interaction()))

    asyncio.run(view._on_previous(_fake_interaction()))

    assert len(fetch.calls) == 1  # Previous must not call fetch again
    assert view._current.pick.text == "First"
    assert view._previous_button.disabled is True


def test_random_result_view_shuffle_after_previous_discards_stale_forward_history():
    worker = _FakeWorker()
    fetch = _FakeFetch([
        _random_pick(entry_id=2, text="Second", pool_size=5),
        _random_pick(entry_id=3, text="Third", pool_size=5),
    ])
    view = RandomResultView(worker, fetch, _random_pick(entry_id=1, text="First", pool_size=5), b"x", "clip.gif")
    asyncio.run(view.shuffle.callback(_fake_interaction()))  # -> Second
    asyncio.run(view._on_previous(_fake_interaction()))  # back to First

    asyncio.run(view.shuffle.callback(_fake_interaction()))  # branches to Third, not Second

    assert view._current.pick.text == "Third"
    assert len(view._history) == 2


def test_random_result_view_accumulates_seen_entry_ids_across_shuffles():
    worker = _FakeWorker()
    fetch = _FakeFetch([
        _random_pick(entry_id=2, text="Second", pool_size=5),
        _random_pick(entry_id=3, text="Third", pool_size=5),
    ])
    view = RandomResultView(worker, fetch, _random_pick(entry_id=1, text="First", pool_size=5), b"x", "clip.gif")

    asyncio.run(view.shuffle.callback(_fake_interaction()))
    asyncio.run(view.shuffle.callback(_fake_interaction()))

    assert fetch.calls[1] == (frozenset({1, 2}), 2)


def test_random_result_view_exhausted_pick_resets_seen_set_to_just_the_new_pick():
    worker = _FakeWorker()
    fetch = _FakeFetch([
        _random_pick(entry_id=2, text="Second", pool_size=2, exhausted=True),
        _random_pick(entry_id=1, text="First again", pool_size=2),
    ])
    view = RandomResultView(worker, fetch, _random_pick(entry_id=1, text="First", pool_size=2), b"x", "clip.gif")

    asyncio.run(view.shuffle.callback(_fake_interaction()))
    asyncio.run(view.shuffle.callback(_fake_interaction()))

    # Second call's exclude set must be reset to just {2} (the exhausted
    # pick), not the ever-growing {1, 2} — otherwise a 2-line pool would
    # force every subsequent pick to also come back exhausted.
    assert fetch.calls[1] == (frozenset({2}), 2)
