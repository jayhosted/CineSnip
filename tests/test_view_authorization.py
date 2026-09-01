from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

from app.bot.cogs.gif import (
    AudioClipResultView,
    ClipEditView,
    LibrarySearchView,
    QuoteMatchView,
    RandomResultView,
    _SoundboardReplacePickerView,
)
from app.bot.worker_client import LibraryQuoteMatchResult, QuoteMatchResult, RandomQuoteResult

# Regression coverage for the audit finding: none of these views restricted
# their buttons/selects to the Discord user who ran the command, so any
# server member who could see the message could operate someone else's
# in-progress render (confirmed via discord.py's interaction_check hook
# never being overridden). Every view here now derives from
# _InvokerOnlyView, which rejects any interaction whose .user.id doesn't
# match the invoker_id it was constructed with.

_INVOKER_ID = 111
_OTHER_USER_ID = 222


def _interaction(user_id: int) -> AsyncMock:
    interaction = AsyncMock()
    interaction.user.id = user_id
    return interaction


def _quote_match(i: int = 0) -> QuoteMatchResult:
    return QuoteMatchResult(
        start=float(i), end=float(i) + 2.0, timecode=f"0:{i:02d}", text="Line", score=60.0,
        entry_indices=[i], context_before=[], context_after=[],
    )


def _library_match() -> LibraryQuoteMatchResult:
    return LibraryQuoteMatchResult(
        media_id="1", title="A Film", library_name="Movies", start=0.0, end=2.0,
        timecode="0:00", text="Line", score=60.0, context_before=[], context_after=[],
    )


def _random_pick() -> RandomQuoteResult:
    return RandomQuoteResult(
        media_id="1", title="A Film", library_name="Movies", start=0.0, end=2.0,
        timecode="0:00", text="Line", entry_id=1, pool_size=5, exhausted=False,
    )


def _assert_rejects_other_user_but_allows_invoker(view) -> None:
    other = _interaction(_OTHER_USER_ID)
    allowed = asyncio.run(view.interaction_check(other))
    assert allowed is False
    other.response.send_message.assert_awaited_once()
    assert other.response.send_message.await_args.kwargs.get("ephemeral") is True

    invoker = _interaction(_INVOKER_ID)
    allowed = asyncio.run(view.interaction_check(invoker))
    assert allowed is True


def test_quote_match_view_rejects_non_invoker():
    view = QuoteMatchView(_INVOKER_ID, "Title", [_quote_match()], min_score=50.0, confident_score=85.0)
    _assert_rejects_other_user_but_allows_invoker(view)


def test_clip_edit_view_rejects_non_invoker():
    async def build():
        return ClipEditView(
            _INVOKER_ID,
            worker=AsyncMock(),
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

    view = asyncio.run(build())
    _assert_rejects_other_user_but_allows_invoker(view)


def test_audio_clip_result_view_rejects_non_invoker():
    async def build():
        return AudioClipResultView(
            _INVOKER_ID,
            AsyncMock(),
            media_id="1",
            title="Title",
            content=b"audio-bytes",
            filename="clip.mp3",
            clip_start=10.0,
            clip_duration=4.0,
        )

    view = asyncio.run(build())
    _assert_rejects_other_user_but_allows_invoker(view)


def test_random_result_view_rejects_non_invoker():
    view = RandomResultView(_INVOKER_ID, AsyncMock(), AsyncMock(), _random_pick(), b"x", "clip.gif")
    _assert_rejects_other_user_but_allows_invoker(view)


def test_library_search_view_rejects_non_invoker():
    view = LibrarySearchView(_INVOKER_ID, AsyncMock(), "quote", [_library_match()])
    _assert_rejects_other_user_but_allows_invoker(view)


def test_soundboard_replace_picker_view_inherits_parents_invoker():
    async def build():
        parent = AudioClipResultView(
            _INVOKER_ID,
            AsyncMock(),
            media_id="1",
            title="Title",
            content=b"audio-bytes",
            filename="clip.mp3",
            clip_start=10.0,
            clip_duration=4.0,
        )
        return _SoundboardReplacePickerView(parent, candidates=[], bot_user_id=999)

    picker = asyncio.run(build())
    assert picker.invoker_id == _INVOKER_ID
    _assert_rejects_other_user_but_allows_invoker(picker)


def test_post_to_channel_button_actually_goes_through_interaction_check():
    # Not just a unit-tested helper method in isolation — discord.py's own
    # dispatch path (View._scheduled_task) calls interaction_check before
    # routing a component interaction to its callback. This exercises that
    # real dispatch path end to end for one representative view+button,
    # rather than trusting that interaction_check alone is enough to
    # conclude the button is actually covered.
    view = QuoteMatchView(_INVOKER_ID, "Title", [_quote_match()], min_score=50.0, confident_score=85.0)
    confirm_button = next(item for item in view.children if getattr(item, "label", None) == "Confirm")

    other = _interaction(_OTHER_USER_ID)
    asyncio.run(view._scheduled_task(confirm_button, other))
    assert view.value is None  # the confirm callback never ran
    other.response.send_message.assert_awaited_once()
