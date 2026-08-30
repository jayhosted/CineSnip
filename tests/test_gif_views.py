import asyncio
from unittest.mock import AsyncMock

import discord

from app.bot.cogs.gif import _PAGE_SIZE, QuoteMatchView
from app.bot.worker_client import QuoteMatchResult


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


def test_quote_match_view_below_confident_score_opens_first_page_select():
    matches = [_quote_match(i, score=60.0) for i in range(20)]
    view = QuoteMatchView("Title", matches, min_score=50.0, confident_score=85.0)

    assert view._select is not None
    assert [opt.value for opt in view._select.options] == [str(i) for i in range(_PAGE_SIZE)]
    assert view._prev_button.disabled is True
    assert view._next_button.disabled is False


def test_quote_match_view_confident_top_match_shows_button_not_select():
    matches = [_quote_match(0, score=95.0)] + [_quote_match(i, score=60.0) for i in range(1, 20)]
    view = QuoteMatchView("Title", matches, min_score=50.0, confident_score=85.0)

    assert view._select is None
    labels = [item.label for item in view.children if isinstance(item, discord.ui.Button)]
    assert "Show other matches" in labels


def test_quote_match_view_show_others_reveals_paged_select():
    matches = [_quote_match(0, score=95.0)] + [_quote_match(i, score=60.0) for i in range(1, 20)]
    view = QuoteMatchView("Title", matches, min_score=50.0, confident_score=85.0)

    asyncio.run(view._on_show_others(_fake_interaction()))

    assert view._select is not None
    assert len(view._select.options) == _PAGE_SIZE
    assert view._next_button.disabled is False


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
