import asyncio
from unittest.mock import AsyncMock

import discord

from app.bot.cogs.gif import (
    _PAGE_SIZE,
    AudioClipResultView,
    ClipEditView,
    QuoteMatchView,
    RandomResultView,
)
from app.bot.worker_client import QuoteMatchResult, RandomQuoteResult, SubtitleEntryResult


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
    view = QuoteMatchView(999, "Title", matches, min_score=50.0, confident_score=85.0)

    assert view.timeout == 600


def test_quote_match_view_below_confident_score_opens_first_page_select():
    matches = [_quote_match(i, score=60.0) for i in range(20)]
    view = QuoteMatchView(999, "Title", matches, min_score=50.0, confident_score=85.0)

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
    view = QuoteMatchView(999, "Title", matches, min_score=50.0, confident_score=85.0)

    assert view._select is not None
    assert len(view._select.options) == _PAGE_SIZE
    assert view._next_button.disabled is False
    labels = [item.label for item in view.children if hasattr(item, "label")]
    assert "Show other matches" not in labels


def test_quote_match_view_next_then_previous_round_trips():
    matches = [_quote_match(i, score=60.0) for i in range(20)]
    view = QuoteMatchView(999, "Title", matches, min_score=50.0, confident_score=85.0)

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
    view = QuoteMatchView(999, "Title", matches, min_score=50.0, confident_score=85.0)

    asyncio.run(view._on_next(_fake_interaction()))
    view._select._values = ["10"]

    asyncio.run(view._on_select(_fake_interaction()))

    assert view.index == 10
    assert view.selected.text == "Line 10"


def test_quote_match_view_no_page_buttons_when_batch_fits_one_page():
    matches = [_quote_match(i, score=60.0) for i in range(8)]
    view = QuoteMatchView(999, "Title", matches, min_score=50.0, confident_score=85.0)

    assert view._prev_button is None
    assert view._next_button is None


def test_quote_match_view_next_past_last_page_is_a_noop():
    matches = [_quote_match(i, score=60.0) for i in range(20)]  # 3 pages: 0,1,2
    view = QuoteMatchView(999, "Title", matches, min_score=50.0, confident_score=85.0)

    asyncio.run(view._on_next(_fake_interaction()))
    asyncio.run(view._on_next(_fake_interaction()))
    assert view._page == 2
    # Simulated raced double-click: a third Next past the last page.
    asyncio.run(view._on_next(_fake_interaction()))
    assert view._page == 2
    assert len(view._select.options) > 0


def test_quote_match_view_previous_before_first_page_is_a_noop():
    matches = [_quote_match(i, score=60.0) for i in range(20)]
    view = QuoteMatchView(999, "Title", matches, min_score=50.0, confident_score=85.0)

    asyncio.run(view._on_previous(_fake_interaction()))
    assert view._page == 0
    assert len(view._select.options) > 0


def test_quote_match_view_footer_shows_page_of_pages_not_raw_count():
    matches = [_quote_match(i, score=60.0) for i in range(20)]  # 3 pages
    view = QuoteMatchView(999, "Title", matches, min_score=50.0, confident_score=85.0)

    assert view.embed().footer.text == "Page 1 of 3"

    asyncio.run(view._on_next(_fake_interaction()))
    assert view.embed().footer.text == "Page 2 of 3"


def test_quote_match_view_footer_omitted_when_batch_fits_one_page():
    matches = [_quote_match(i, score=60.0) for i in range(3)]
    view = QuoteMatchView(999, "Title", matches, min_score=50.0, confident_score=85.0)

    assert view.embed().footer.text is None


def test_quote_match_view_truncated_note_only_on_last_page():
    matches = [_quote_match(i, score=60.0) for i in range(20)]  # 3 pages
    view = QuoteMatchView(
        999, "Title", matches, min_score=50.0, confident_score=85.0, truncated=True
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
        999, "Title", matches, min_score=50.0, confident_score=85.0, truncated=False
    )

    asyncio.run(view._on_next(_fake_interaction()))
    asyncio.run(view._on_next(_fake_interaction()))
    assert view.embed().footer.text == "Page 3 of 3"


def test_quote_match_view_component_rows_put_confirm_cancel_below_pagination():
    matches = [_quote_match(i, score=60.0) for i in range(20)]
    view = QuoteMatchView(999, "Title", matches, min_score=50.0, confident_score=85.0)

    rows = {item.label: item.row for item in view.children if hasattr(item, "label")}
    assert rows["◀ Previous"] == 2
    assert rows["Next ▶"] == 2
    assert view._select.row == 1
    # Confirm/Cancel sit at the bottom of the view, below whichever of the
    # select/pagination rows actually exist — row 3 once pagination is
    # present.
    assert rows["Confirm"] == 3
    assert rows["Cancel"] == 3


def test_quote_match_view_component_rows_no_pagination_puts_confirm_below_select():
    matches = [_quote_match(i, score=60.0) for i in range(3)]
    view = QuoteMatchView(999, "Title", matches, min_score=50.0, confident_score=85.0)

    rows = {item.label: item.row for item in view.children if hasattr(item, "label")}
    assert view._select.row == 1
    assert rows["Confirm"] == 2
    assert rows["Cancel"] == 2


def test_quote_match_view_component_rows_single_match_puts_confirm_on_row_one():
    matches = [_quote_match(0, score=60.0)]
    view = QuoteMatchView(999, "Title", matches, min_score=50.0, confident_score=85.0)

    rows = {item.label: item.row for item in view.children if hasattr(item, "label")}
    assert rows["Confirm"] == 1
    assert rows["Cancel"] == 1


from app.bot.cogs.gif import LibrarySearchView, _library_results_embed
from app.bot.worker_client import LibraryQuoteMatchResult


def _library_match(i: int, score: float = 60.0) -> LibraryQuoteMatchResult:
    return LibraryQuoteMatchResult(
        media_id=str(i),
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
    view = QuoteMatchView(999, "Fixed Title", matches, min_score=50.0, confident_score=85.0)

    assert view.embed().title == "Fixed Title"


def test_quote_match_view_uses_per_match_title_when_title_is_none():
    # /snip tv's whole-show search (issue #7 follow-up): candidates can come
    # from different episodes, so there's no single fixed title to show —
    # title=None means "read it off whichever match is currently selected".
    matches = [_library_match(i) for i in range(3)]
    view = QuoteMatchView(999, None, matches, min_score=50.0, confident_score=85.0)

    assert view.embed().title == matches[0].title


def test_quote_match_view_per_match_title_follows_selection():
    matches = [_library_match(i) for i in range(3)]
    view = QuoteMatchView(999, None, matches, min_score=50.0, confident_score=85.0)

    view._select._values = ["2"]
    asyncio.run(view._on_select(_fake_interaction()))

    assert view.embed().title == matches[2].title


class _FakeCog:
    def __init__(self):
        self.searched_again = None

    async def _run_library_search(self, interaction, quote, format=None, kind="clip"):
        self.searched_again = (quote, format, kind)

    async def _generate(
        self, interaction, film, quote, timecode, end_timecode, format,
        preferred_start=None, kind="clip",
    ):
        self.generated = (film, preferred_start, format, kind)


def test_library_search_view_timeout_gives_time_to_browse_multiple_pages():
    matches = [_library_match(i) for i in range(20)]
    view = LibrarySearchView(999, _FakeCog(), "quote", matches)

    assert view.timeout == 600


def test_library_search_view_first_page_has_eight_options_and_next_enabled():
    matches = [_library_match(i) for i in range(20)]
    view = LibrarySearchView(999, _FakeCog(), "quote", matches)

    assert len(view._select.options) == _PAGE_SIZE
    assert [opt.value for opt in view._select.options] == [str(i) for i in range(8)]
    assert view._prev_button.disabled is True
    assert view._next_button.disabled is False


def test_library_search_view_embed_shows_only_current_page_with_footer():
    matches = [_library_match(i) for i in range(20)]
    view = LibrarySearchView(999, _FakeCog(), "quote", matches)

    embed = view.embed()
    assert len(embed.fields) == _PAGE_SIZE
    assert embed.fields[0].name.startswith("1. ")
    assert embed.footer.text == "Page 1 of 3"


def test_library_search_view_next_shows_second_page():
    matches = [_library_match(i) for i in range(20)]
    view = LibrarySearchView(999, _FakeCog(), "quote", matches)

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
    view = LibrarySearchView(999, _FakeCog(), "quote", matches)

    assert view._prev_button is None
    assert view._next_button is None
    assert view.embed().footer.text is None


def test_library_search_view_select_on_second_page_resolves_absolute_match():
    matches = [_library_match(i) for i in range(20)]
    cog = _FakeCog()
    view = LibrarySearchView(999, cog, "quote", matches)

    asyncio.run(view._on_next(_fake_interaction()))
    view._select._values = ["10"]
    asyncio.run(view._on_select(_fake_interaction()))

    assert cog.generated == (str(10), 10.0, None, "clip")


def test_library_results_embed_footer_omitted_for_single_page():
    matches = [_library_match(i) for i in range(3)]
    embed = _library_results_embed("quote", matches)
    assert embed.footer.text is None


def test_library_search_view_truncated_note_only_on_last_page():
    matches = [_library_match(i) for i in range(20)]  # 3 pages
    view = LibrarySearchView(999, _FakeCog(), "quote", matches, truncated=True)

    assert view.embed().footer.text == "Page 1 of 3"

    asyncio.run(view._on_next(_fake_interaction()))
    assert view.embed().footer.text == "Page 2 of 3"

    asyncio.run(view._on_next(_fake_interaction()))
    assert "more results may exist" in view.embed().footer.text
    assert view.embed().footer.text.startswith("Page 3 of 3")


def test_library_search_view_no_truncated_note_when_not_truncated():
    matches = [_library_match(i) for i in range(20)]
    view = LibrarySearchView(999, _FakeCog(), "quote", matches, truncated=False)

    asyncio.run(view._on_next(_fake_interaction()))
    asyncio.run(view._on_next(_fake_interaction()))
    assert view.embed().footer.text == "Page 3 of 3"


def test_library_search_view_next_past_last_page_is_a_noop():
    matches = [_library_match(i) for i in range(20)]
    view = LibrarySearchView(999, _FakeCog(), "quote", matches)

    asyncio.run(view._on_next(_fake_interaction()))
    asyncio.run(view._on_next(_fake_interaction()))
    asyncio.run(view._on_next(_fake_interaction()))
    assert view._page == 2
    assert len(view._select.options) > 0


def test_library_search_view_previous_before_first_page_is_a_noop():
    matches = [_library_match(i) for i in range(20)]
    view = LibrarySearchView(999, _FakeCog(), "quote", matches)

    asyncio.run(view._on_previous(_fake_interaction()))
    assert view._page == 0
    assert len(view._select.options) > 0


def test_library_search_view_component_rows_stable_across_page_change():
    matches = [_library_match(i) for i in range(20)]
    view = LibrarySearchView(999, _FakeCog(), "quote", matches, remaining_uncached=5)

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


# LibrarySearchView's format/kind (issue #6 follow-up: /snip audio's
# title-less path reuses this same cross-title picker) — a selection or
# "Search N more" click must carry the audio format/kind through rather
# than silently reverting to the gif/"clip" defaults.


def test_library_search_view_select_forwards_format_and_kind():
    matches = [_library_match(i) for i in range(3)]
    cog = _FakeCog()
    view = LibrarySearchView(999, cog, "quote", matches, format="mp3", kind="audio")
    view._select._values = ["1"]

    asyncio.run(view._on_select(_fake_interaction()))

    assert cog.generated == (str(1), 1.0, "mp3", "audio")


def test_library_search_view_search_more_forwards_format_and_kind():
    matches = [_library_match(i) for i in range(3)]
    cog = _FakeCog()
    view = LibrarySearchView(999, cog, "quote", matches, format="mp3", kind="audio")

    asyncio.run(view._on_search_more(_fake_interaction()))

    assert cog.searched_again == ("quote", "mp3", "audio")


def test_library_search_view_defaults_to_gif_format_and_clip_kind():
    matches = [_library_match(i) for i in range(3)]
    cog = _FakeCog()
    view = LibrarySearchView(999, cog, "quote", matches)
    view._select._values = ["0"]

    asyncio.run(view._on_select(_fake_interaction()))

    assert cog.generated == (str(0), 0.0, None, "clip")


def _random_pick(
    media_id="1", title="Film", start=0.0, end=2.0, text="Line",
    entry_id=1, pool_size=5, exhausted=False,
) -> RandomQuoteResult:
    return RandomQuoteResult(
        media_id=media_id, title=title, library_name="Movies",
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
    view = RandomResultView(999, _FakeWorker(), _FakeFetch([]), _random_pick(pool_size=1), b"x", "clip.gif")

    assert view.shuffle.disabled is True


def test_random_result_view_enables_shuffle_when_pool_has_more_than_one():
    view = RandomResultView(999, _FakeWorker(), _FakeFetch([]), _random_pick(pool_size=5), b"x", "clip.gif")

    assert view.shuffle.disabled is False


def test_random_result_view_button_order_previous_shuffle_post():
    worker = _FakeWorker()
    fetch = _FakeFetch([_random_pick(entry_id=2, text="Second", pool_size=5)])
    view = RandomResultView(999, worker, fetch, _random_pick(entry_id=1, text="First", pool_size=5), b"x", "clip.gif")

    # Before the first shuffle, Previous doesn't exist yet — Shuffle then
    # Post, with Post last.
    assert [item.label for item in view.children] == ["🔀 Shuffle", "Post to channel"]

    asyncio.run(view.shuffle.callback(_fake_interaction()))

    # Previous belongs on the left, Post always stays last.
    assert [item.label for item in view.children] == ["◀ Previous", "🔀 Shuffle", "Post to channel"]


def test_random_result_view_shuffle_adds_previous_button_and_passes_exclusion():
    worker = _FakeWorker()
    fetch = _FakeFetch([_random_pick(entry_id=2, text="Second", pool_size=5)])
    view = RandomResultView(999, worker, fetch, _random_pick(entry_id=1, text="First", pool_size=5), b"x", "clip.gif")
    assert view._previous_button not in view.children

    asyncio.run(view.shuffle.callback(_fake_interaction()))

    assert fetch.calls == [(frozenset({1}), 1)]
    assert view._previous_button in view.children
    assert view._previous_button.disabled is False
    assert view._current.pick.text == "Second"


def test_random_result_view_previous_steps_back_without_refetching():
    worker = _FakeWorker()
    fetch = _FakeFetch([_random_pick(entry_id=2, text="Second", pool_size=5)])
    view = RandomResultView(999, worker, fetch, _random_pick(entry_id=1, text="First", pool_size=5), b"x", "clip.gif")
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
    view = RandomResultView(999, worker, fetch, _random_pick(entry_id=1, text="First", pool_size=5), b"x", "clip.gif")
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
    view = RandomResultView(999, worker, fetch, _random_pick(entry_id=1, text="First", pool_size=5), b"x", "clip.gif")

    asyncio.run(view.shuffle.callback(_fake_interaction()))
    asyncio.run(view.shuffle.callback(_fake_interaction()))

    assert fetch.calls[1] == (frozenset({1, 2}), 2)


def test_random_result_view_exhausted_pick_resets_seen_set_to_just_the_new_pick():
    worker = _FakeWorker()
    fetch = _FakeFetch([
        _random_pick(entry_id=2, text="Second", pool_size=2, exhausted=True),
        _random_pick(entry_id=1, text="First again", pool_size=2),
    ])
    view = RandomResultView(999, worker, fetch, _random_pick(entry_id=1, text="First", pool_size=2), b"x", "clip.gif")

    asyncio.run(view.shuffle.callback(_fake_interaction()))
    asyncio.run(view.shuffle.callback(_fake_interaction()))

    # Second call's exclude set must be reset to just {2} (the exhausted
    # pick), not the ever-growing {1, 2} — otherwise a 2-line pool would
    # force every subsequent pick to also come back exhausted.
    assert fetch.calls[1] == (frozenset({2}), 2)


class _FakeSubtitleStatus:
    def __init__(self, likely_slow: bool = False) -> None:
        self.likely_slow = likely_slow


class _FakeRenderResult2:
    def __init__(
        self,
        content: bytes = b"clip-bytes",
        format: str = "gif",
        style: str = "classic",
        start: float = 10.0,
        duration: float = 4.0,
    ) -> None:
        self.content = content
        self.format = format
        self.style = style
        self.start = start
        self.duration = duration


class _FakeEditWorker:
    def __init__(self, entries: list[SubtitleEntryResult] | None = None, likely_slow: bool = False) -> None:
        self.render = AsyncMock(return_value=_FakeRenderResult2())
        self.subtitles = AsyncMock(return_value=entries or [])
        self.subtitle_status = AsyncMock(return_value=_FakeSubtitleStatus(likely_slow=likely_slow))


def _make_clip_edit_view(worker) -> ClipEditView:
    # Must be called from inside a running event loop — __init__ kicks off
    # a background entries-prefetch task (asyncio.create_task), which
    # requires one, and that task stays bound to whichever loop was running
    # when it was created (a second, later asyncio.run() call spins up a
    # *different* loop and can't await a task from the first one). Every
    # test below therefore does all of its asyncio.run()-driven work inside
    # a single wrapping coroutine, not one asyncio.run() per action.
    return ClipEditView(
        999,
        worker,
        media_id="1",
        title="Title",
        timecode="10",
        duration=4.0,
        end_timecode=None,
        format=None,
        style="classic",
        content=b"clip-bytes",
        filename="clip.gif",
        clip_start=10.0,
        clip_duration=4.0,
    )


def _entry(index: int, start: float, end: float, text: str) -> SubtitleEntryResult:
    return SubtitleEntryResult(index=index, start=start, end=end, text=text)


def test_clip_edit_view_duration_row_layout_defaults_to_middle_step():
    async def run():
        view = _make_clip_edit_view(_FakeEditWorker())
        await view._open_duration()

        row2 = [b for b in view._category_buttons if b.row == 2]
        row3 = [b for b in view._category_buttons if b.row == 3]
        assert [b.label for b in row2] == [
            "Start ← 1s", "Start → 1s", "End ← 1s", "End → 1s", "Custom",
        ]
        assert [b.label for b in row3] == ["Shift ← 1s", "Shift → 1s", "0.5s", "1s", "3s"]

        active = next(b for b in row3 if b.label == "1s")
        assert active.style == discord.ButtonStyle.success
        assert active.disabled is True
        inactive = next(b for b in row3 if b.label == "3s")
        assert inactive.style == discord.ButtonStyle.secondary
        assert inactive.disabled is False

    asyncio.run(run())


def test_clip_edit_view_step_button_switches_step_and_relabels_arrows():
    async def run():
        view = _make_clip_edit_view(_FakeEditWorker())
        await view._open_duration()
        step_3s = next(b for b in view._category_buttons if b.label == "3s")

        await step_3s.callback(_fake_interaction())

        assert view._duration_step == 3.0
        labels = [b.label for b in view._category_buttons]
        assert "Start ← 3s" in labels
        assert "Shift ← 3s" in labels
        active = next(b for b in view._category_buttons if b.label == "3s")
        assert active.disabled is True

    asyncio.run(run())


def test_clip_edit_view_shift_moves_both_boundaries_by_the_current_step():
    async def run():
        view = _make_clip_edit_view(_FakeEditWorker())
        view._re_render = AsyncMock()
        await view._open_duration()
        shift_left = next(b for b in view._category_buttons if b.label == "Shift ← 1s")
        start_before, end_before = view._clip_start, view._clip_end

        await shift_left.callback(_fake_interaction())

        view._re_render.assert_awaited_once()
        _, new_start, new_end = view._re_render.await_args.args
        assert new_start == start_before - 1.0
        assert new_end == end_before - 1.0

    asyncio.run(run())


def test_clip_edit_view_shift_right_at_3s_step_moves_start_and_end_together():
    async def run():
        view = _make_clip_edit_view(_FakeEditWorker())
        view._re_render = AsyncMock()
        view._duration_step = 3.0
        await view._open_duration()
        shift_right = next(b for b in view._category_buttons if b.label == "Shift → 3s")
        start_before, end_before = view._clip_start, view._clip_end

        await shift_right.callback(_fake_interaction())

        view._re_render.assert_awaited_once()
        _, new_start, new_end = view._re_render.await_args.args
        assert new_start == start_before + 3.0
        assert new_end == end_before + 3.0

    asyncio.run(run())


def test_clip_edit_view_start_nudge_only_moves_start():
    async def run():
        view = _make_clip_edit_view(_FakeEditWorker())
        view._re_render = AsyncMock()
        await view._open_duration()
        start_left = next(b for b in view._category_buttons if b.label == "Start ← 1s")
        start_before, end_before = view._clip_start, view._clip_end

        await start_left.callback(_fake_interaction())

        _, new_start, new_end = view._re_render.await_args.args
        assert new_start == start_before - 1.0
        assert new_end == end_before

    asyncio.run(run())


def test_clip_edit_view_edit_subs_opens_modal_directly_when_entries_cached():
    async def run():
        view = _make_clip_edit_view(_FakeEditWorker())
        view._all_entries = [_entry(0, 9.0, 13.0, "cached line")]
        interaction = _fake_interaction()
        interaction.response.send_modal = AsyncMock()

        await view._on_edit_subs_entry(interaction)

        interaction.response.send_modal.assert_awaited_once()
        interaction.response.defer.assert_not_called()
        assert view._open_category is None

    asyncio.run(run())


def test_clip_edit_view_edit_subs_no_window_sends_ephemeral_notice_when_cached():
    async def run():
        view = _make_clip_edit_view(_FakeEditWorker())
        view._all_entries = [_entry(0, 100.0, 103.0, "far away line")]
        interaction = _fake_interaction()
        interaction.response.send_modal = AsyncMock()
        interaction.response.send_message = AsyncMock()

        await view._on_edit_subs_entry(interaction)

        interaction.response.send_modal.assert_not_called()
        interaction.response.send_message.assert_awaited_once()
        assert interaction.response.send_message.call_args.kwargs.get("ephemeral") is True

    asyncio.run(run())


def test_clip_edit_view_edit_subs_cold_falls_back_to_open_button_then_modal():
    async def run():
        # likely_slow=False here (unlike the dedicated slow-warning test
        # below) so the prefetch task this view kicks off on construction
        # resolves fast — the interesting "cold" case under test is a
        # click landing *before* that background task has had a chance to
        # run at all, not a slow extraction.
        entries = [_entry(0, 9.0, 13.0, "cold line")]
        worker = _FakeEditWorker(entries=entries)
        view = _make_clip_edit_view(worker)
        assert view._all_entries is None

        first_click = _fake_interaction()
        first_click.response.send_modal = AsyncMock()
        await view._on_edit_subs_entry(first_click)

        # Cold path must defer (a modal can't be the response after an
        # async fetch) and never itself opens the modal.
        first_click.response.defer.assert_awaited_once()
        first_click.response.send_modal.assert_not_called()
        assert view._open_category == "subtitles"
        fallback = next(b for b in view._category_buttons if b.label == "Open Edit Subs")
        assert fallback.disabled is False

        second_click = _fake_interaction()
        second_click.response.send_modal = AsyncMock()
        await view._on_open_edit_subs_fallback(second_click)

        second_click.response.send_modal.assert_awaited_once()
        assert view._open_category is None

    asyncio.run(run())


def test_clip_edit_view_prefetches_entries_so_first_ever_click_opens_modal_instantly():
    # This is the whole point of _start_entries_prefetch: on a
    # warm/cached title, the background fetch kicked off in __init__
    # finishes before the user has had time to click anything, so even the
    # very first Edit Subs click of the session takes the fast path.
    async def run():
        entries = [_entry(0, 9.0, 13.0, "prefetched line")]
        worker = _FakeEditWorker(entries=entries)
        view = _make_clip_edit_view(worker)

        await view._entries_task  # let the background prefetch finish

        assert view._all_entries == entries
        interaction = _fake_interaction()
        interaction.response.send_modal = AsyncMock()

        await view._on_edit_subs_entry(interaction)

        interaction.response.send_modal.assert_awaited_once()
        interaction.response.defer.assert_not_called()
        worker.subtitles.assert_awaited_once()

    asyncio.run(run())


def test_clip_edit_view_click_during_in_flight_prefetch_awaits_it_without_refetching():
    # A click landing while the background prefetch is still running must
    # await that same task rather than starting a second worker call.
    async def run():
        entries = [_entry(0, 9.0, 13.0, "slow line")]
        call_count = 0
        release = asyncio.Event()

        async def slow_subtitles(media_id: str) -> list[SubtitleEntryResult]:
            nonlocal call_count
            call_count += 1
            await release.wait()
            return entries

        worker = _FakeEditWorker(likely_slow=True)
        worker.subtitles = slow_subtitles
        view = _make_clip_edit_view(worker)

        interaction = _fake_interaction()
        interaction.response.send_modal = AsyncMock()
        click_task = asyncio.ensure_future(view._on_edit_subs_entry(interaction))
        await asyncio.sleep(0)  # let both the prefetch task and the click reach their awaits

        assert call_count == 1  # the click reused the in-flight prefetch, not a second call
        assert not click_task.done()

        release.set()
        await click_task

        interaction.response.send_modal.assert_not_called()  # cold click, so fallback button
        fallback = next(b for b in view._category_buttons if b.label == "Open Edit Subs")
        assert fallback.disabled is False

    asyncio.run(run())


# RandomResultView's format/style params (issue #6): let /snip audio's
# random-pick path reuse Shuffle/Previous/Post rather than forking a new
# view, while every pre-existing caller (/snip movie, /snip tv, /snip
# random) keeps the original gif/"classic" defaults untouched.


def test_random_result_view_shuffle_defaults_to_classic_style_and_no_format():
    worker = _FakeWorker()
    fetch = _FakeFetch([_random_pick(entry_id=2, pool_size=5)])
    view = RandomResultView(999, worker, fetch, _random_pick(entry_id=1, pool_size=5), b"x", "clip.gif")

    asyncio.run(view.shuffle.callback(_fake_interaction()))

    worker.render.assert_awaited_once()
    _, kwargs = worker.render.await_args
    assert kwargs["format"] is None
    assert kwargs["style"] == "classic"


def test_random_result_view_shuffle_uses_the_given_format_and_style():
    worker = _FakeWorker()
    fetch = _FakeFetch([_random_pick(entry_id=2, pool_size=5)])
    view = RandomResultView(
        999, worker, fetch, _random_pick(entry_id=1, pool_size=5), b"x", "clip.mp3",
        format="mp3", style="none",
    )

    asyncio.run(view.shuffle.callback(_fake_interaction()))

    worker.render.assert_awaited_once()
    _, kwargs = worker.render.await_args
    assert kwargs["format"] == "mp3"
    assert kwargs["style"] == "none"


# AudioClipResultView (issue #6): the /snip audio result view. No
# style-select or ✏️ Edit Subs (no burn-in to edit), but issue #21 gives it
# the same ⏱ Duration / 🔀 Merge Subs span-editing controls as ClipEditView,
# via the shared _DurationMergeMixin.


def _make_audio_view(worker, format=None) -> AudioClipResultView:
    # See _make_clip_edit_view's note: must run inside the same event loop
    # as any asyncio.run() driving further actions on this view, since
    # __init__ kicks off a background entries-prefetch task bound to
    # whichever loop is current when it's created.
    return AudioClipResultView(
        999,
        worker,
        media_id="1",
        title="The Matrix",
        content=b"audio-bytes",
        filename="clip.mp3",
        clip_start=10.0,
        clip_duration=4.0,
        format=format,
    )


def test_audio_clip_result_view_has_duration_merge_toggles_and_post():
    async def run():
        view = _make_audio_view(_FakeEditWorker())
        return {item.label: item.row for item in view.children}

    rows_by_label = asyncio.run(run())
    assert rows_by_label == {
        "⏱ Duration": 1,
        "🔀 Merge Subs": 1,
        "Post to channel": 4,
        "Add to Soundboard": 4,
    }


def test_audio_clip_result_view_post_sends_file_and_disables_button():
    async def run():
        view = _make_audio_view(_FakeEditWorker())
        interaction = _fake_interaction()
        interaction.user.display_name = "Neo"
        await view.post.callback(interaction)
        return interaction, view

    interaction, view = asyncio.run(run())

    interaction.channel.send.assert_awaited_once()
    _, kwargs = interaction.channel.send.await_args
    assert kwargs["file"].filename == "clip.mp3"
    assert "the-matrix" in kwargs["content"]
    assert view.post.disabled is True


def test_audio_clip_result_view_duration_row_layout_defaults_to_middle_step():
    async def run():
        view = _make_audio_view(_FakeEditWorker())
        await view._open_duration()

        row2 = [b for b in view._category_buttons if b.row == 2]
        row3 = [b for b in view._category_buttons if b.row == 3]
        assert [b.label for b in row2] == [
            "Start ← 1s", "Start → 1s", "End ← 1s", "End → 1s", "Custom",
        ]
        assert [b.label for b in row3] == ["Shift ← 1s", "Shift → 1s", "0.5s", "1s", "3s"]

    asyncio.run(run())


def test_audio_clip_result_view_start_nudge_calls_render_with_style_none_no_overrides():
    async def run():
        view = _make_audio_view(_FakeEditWorker(), format="mp3")
        await view._open_duration()
        start_left = next(b for b in view._category_buttons if b.label == "Start ← 1s")

        await start_left.callback(_fake_interaction())
        return view

    view = asyncio.run(run())

    view._worker.render.assert_awaited_once()
    _, kwargs = view._worker.render.await_args
    assert kwargs["start"] == 9.0
    assert kwargs["end"] == 14.0
    assert kwargs["format"] == "mp3"
    assert kwargs["style"] == "none"
    assert "subtitle_overrides" not in kwargs


def test_audio_clip_result_view_merge_previous_extends_start_to_adjacent_entry():
    async def run():
        entries = [_entry(0, 5.0, 8.0, "previous line")]
        view = _make_audio_view(_FakeEditWorker(entries=entries))

        await view._on_merge_previous(_fake_interaction())
        return view

    view = asyncio.run(run())

    view._worker.render.assert_awaited_once()
    _, kwargs = view._worker.render.await_args
    assert kwargs["start"] == 5.0
    assert kwargs["end"] == 14.0


def test_audio_clip_result_view_merge_open_uses_embed_footer_like_clip_edit_view():
    # Styling consistency with ClipEditView's Merge Subs screen (issue #5):
    # the surrounding-lines readout goes in an embed footer, not plain
    # content — audio just has no gif to set as the embed's image.
    async def run():
        entries = [_entry(0, 5.0, 8.0, "previous line")]
        view = _make_audio_view(_FakeEditWorker(entries=entries))
        interaction = _fake_interaction()

        await view._on_toggle_merge(interaction)
        return interaction

    interaction = asyncio.run(run())

    _, kwargs = interaction.edit_original_response.await_args
    assert kwargs["content"] is None
    assert "previous line" in kwargs["embed"].footer.text
    assert not kwargs["embed"].image.url


def test_audio_clip_result_view_edit_with_no_cached_entries_falls_back_to_generic_filename():
    async def run():
        worker = _FakeEditWorker()
        worker.render.return_value = _FakeRenderResult2(format="mp3", start=9.0, duration=5.0)
        view = _make_audio_view(worker)
        await view._open_duration()
        start_left = next(b for b in view._category_buttons if b.label == "Start ← 1s")

        await start_left.callback(_fake_interaction())
        return view

    view = asyncio.run(run())

    assert view._filename == "clip.mp3"
    assert view._clip_start == 9.0
    assert view._clip_duration == 5.0


def test_audio_clip_result_view_edit_uses_first_line_in_the_new_window_as_filename():
    # A merge/nudge that pulls a multi-line window into the clip must not
    # revert to the generic "clip.mp3" — it should keep reflecting a
    # subtitle line, specifically the first (earliest-start) one now in
    # the window, the same way the initial render's filename does.
    async def run():
        entries = [
            _entry(0, 9.0, 9.5, "Whoa"),
            _entry(1, 10.0, 13.0, "Neo wakes up"),
        ]
        worker = _FakeEditWorker(entries=entries)
        worker.render.return_value = _FakeRenderResult2(format="mp3", start=9.0, duration=5.0)
        view = _make_audio_view(worker)
        await view._ensure_entries()
        await view._open_duration()
        start_left = next(b for b in view._category_buttons if b.label == "Start ← 1s")

        await start_left.callback(_fake_interaction())
        return view

    view = asyncio.run(run())

    assert view._filename == "whoa.mp3"


# _render_and_respond's audio filename (issue #6 follow-up): reflects the
# subtitle line actually used, not a generic "clip.mp3" — _generate/
# _run_tv_generate thread the matched line's text through as subtitle_text.


class _FakeRenderResultWithSpan:
    def __init__(self, format="mp3", start=10.0, duration=4.0, style="none"):
        self.content = b"audio-bytes"
        self.format = format
        self.start = start
        self.duration = duration
        self.style = style


def test_render_and_respond_audio_filename_uses_subtitle_text():
    from types import SimpleNamespace

    from app.bot.cogs.gif import GifCog

    worker = SimpleNamespace(
        render=AsyncMock(return_value=_FakeRenderResultWithSpan()),
        subtitles=AsyncMock(return_value=[]),
    )
    cog = GifCog(bot=SimpleNamespace(worker=worker))
    interaction = _fake_interaction()

    async def run():
        await cog._render_and_respond(
            interaction, 1, "The Matrix", "10", 4.0, None, "mp3", "none",
            kind="audio", subtitle_text="I know kung fu",
        )
        # Let AudioClipResultView's background entries-prefetch task (kicked
        # off during construction) finish inside this same loop — see
        # test_render_and_respond_clip_filename_ignores_subtitle_text.
        await asyncio.sleep(0)

    asyncio.run(run())

    _, kwargs = interaction.edit_original_response.await_args
    assert kwargs["attachments"][0].filename == "i-know-kung-fu.mp3"


def test_render_and_respond_audio_over_5_2s_shows_soundboard_disabled_note():
    from types import SimpleNamespace

    from app.bot.cogs.gif import GifCog

    worker = SimpleNamespace(
        render=AsyncMock(return_value=_FakeRenderResultWithSpan(duration=6.0)),
        subtitles=AsyncMock(return_value=[]),
    )
    cog = GifCog(bot=SimpleNamespace(worker=worker))
    interaction = _fake_interaction()

    async def run():
        await cog._render_and_respond(
            interaction, 1, "The Matrix", "10", 6.0, None, "mp3", "none",
            kind="audio", subtitle_text=None,
        )
        await asyncio.sleep(0)

    asyncio.run(run())

    _, kwargs = interaction.edit_original_response.await_args
    assert "Add to Soundboard is disabled" in kwargs["content"]
    assert "end_timecode" in kwargs["content"]


def test_render_and_respond_audio_filename_falls_back_without_subtitle_text():
    from types import SimpleNamespace

    from app.bot.cogs.gif import GifCog

    worker = SimpleNamespace(
        render=AsyncMock(return_value=_FakeRenderResultWithSpan()),
        subtitles=AsyncMock(return_value=[]),
    )
    cog = GifCog(bot=SimpleNamespace(worker=worker))
    interaction = _fake_interaction()

    async def run():
        await cog._render_and_respond(
            interaction, 1, "The Matrix", "10", None, None, "mp3", "none", kind="audio",
        )
        await asyncio.sleep(0)

    asyncio.run(run())

    _, kwargs = interaction.edit_original_response.await_args
    assert kwargs["attachments"][0].filename == "clip.mp3"


def test_render_and_respond_clip_filename_ignores_subtitle_text():
    # kind="clip" (the default) must never be affected by this — every
    # existing gif/mp4/webm caller keeps the plain "clip.<ext>" name.
    from types import SimpleNamespace

    from app.bot.cogs.gif import GifCog

    cog = GifCog(bot=SimpleNamespace(worker=_FakeEditWorker()))
    interaction = _fake_interaction()

    async def run():
        await cog._render_and_respond(
            interaction, 1, "The Matrix", "10", 4.0, None, None, "classic",
            subtitle_text="I know kung fu",
        )
        # Let ClipEditView's background entries-prefetch task (kicked off
        # during construction) finish inside this same loop, matching
        # _make_clip_edit_view's note above about asyncio.run() boundaries.
        await asyncio.sleep(0)

    asyncio.run(run())

    _, kwargs = interaction.edit_original_response.await_args
    assert kwargs["attachments"][0].filename == "clip.gif"


def test_random_result_view_audio_shuffle_filename_uses_picked_text():
    worker = _FakeWorker()
    worker.render = AsyncMock(return_value=_FakeRenderResult(format="mp3"))
    fetch = _FakeFetch([_random_pick(entry_id=2, text="I know kung fu", pool_size=5)])
    view = RandomResultView(
        999, worker, fetch, _random_pick(entry_id=1, pool_size=5), b"x", "clip.mp3",
        format="mp3", style="none", kind="audio",
    )

    asyncio.run(view.shuffle.callback(_fake_interaction()))

    assert view._current.filename == "i-know-kung-fu.mp3"
