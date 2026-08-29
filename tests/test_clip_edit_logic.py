from app.bot.cogs.gif import (
    _CustomDurationModal,
    _EditSubsModal,
    _entries_in_window,
    _find_merge_next,
    _find_merge_previous,
    _format_edit_blocks,
    _parse_edit_blocks,
)
from app.bot.worker_client import SubtitleEntryResult


def _entry(index, start, end, text):
    return SubtitleEntryResult(index=index, start=start, end=end, text=text)


# --- _entries_in_window ------------------------------------------------------


def test_entries_in_window_keeps_only_overlapping_entries_in_time_order():
    entries = [
        _entry(1, 20.0, 22.0, "third"),
        _entry(2, 0.0, 2.0, "before"),
        _entry(3, 10.0, 12.0, "during"),
    ]
    window = _entries_in_window(entries, clip_start=9.0, clip_end=21.0)
    assert [e.text for e in window] == ["during", "third"]


def test_entries_in_window_excludes_touching_but_not_overlapping_entries():
    entries = [_entry(1, 5.0, 10.0, "touches start")]
    assert _entries_in_window(entries, clip_start=10.0, clip_end=20.0) == []


# --- _format_edit_blocks / _parse_edit_blocks --------------------------------


def test_format_edit_blocks_joins_entry_text_with_separator():
    entries = [_entry(1, 0.0, 2.0, "hello"), _entry(2, 2.0, 4.0, "world")]
    assert _format_edit_blocks(entries, {}) == "hello\n---\nworld"


def test_format_edit_blocks_shows_override_text_when_present():
    entries = [_entry(1, 0.0, 2.0, "hello")]
    assert _format_edit_blocks(entries, {1: "goodbye"}) == "goodbye"


def test_format_edit_blocks_shows_blank_for_a_suppressed_entry():
    entries = [_entry(1, 0.0, 2.0, "hello"), _entry(2, 2.0, 4.0, "world")]
    assert _format_edit_blocks(entries, {1: None}) == "\n---\nworld"


def test_parse_edit_blocks_sets_override_for_changed_text():
    entries = [_entry(1, 0.0, 2.0, "hello")]
    result = _parse_edit_blocks("goodbye", entries, {})
    assert result == {1: "goodbye"}


def test_parse_edit_blocks_suppresses_a_blanked_block():
    entries = [_entry(1, 0.0, 2.0, "hello"), _entry(2, 2.0, 4.0, "world")]
    result = _parse_edit_blocks("hello\n---\n", entries, {})
    assert result == {2: None}


def test_parse_edit_blocks_clears_an_existing_override_when_reverted():
    entries = [_entry(1, 0.0, 2.0, "hello")]
    result = _parse_edit_blocks("hello", entries, {1: "goodbye"})
    assert result == {}


def test_parse_edit_blocks_is_position_based_not_keyed_by_content():
    # Two entries with identical original text: editing only the second
    # block must not affect the first, proving the mapping is positional.
    entries = [_entry(1, 0.0, 2.0, "hi"), _entry(2, 2.0, 4.0, "hi")]
    result = _parse_edit_blocks("hi\n---\nbye", entries, {})
    assert result == {2: "bye"}


# --- _find_merge_previous / _find_merge_next ---------------------------------


def test_find_merge_previous_returns_the_closest_entry_before_clip_start():
    entries = [_entry(1, 0.0, 2.0, "far"), _entry(2, 5.0, 8.0, "closer")]
    result = _find_merge_previous(entries, clip_start=10.0)
    assert result.text == "closer"


def test_find_merge_previous_returns_none_when_nothing_precedes():
    entries = [_entry(1, 20.0, 22.0, "after")]
    assert _find_merge_previous(entries, clip_start=10.0) is None


def test_find_merge_next_returns_the_closest_entry_after_clip_end():
    entries = [_entry(1, 30.0, 32.0, "far"), _entry(2, 20.0, 22.0, "closer")]
    result = _find_merge_next(entries, clip_end=15.0)
    assert result.text == "closer"


def test_find_merge_next_returns_none_when_nothing_follows():
    entries = [_entry(1, 0.0, 2.0, "before")]
    assert _find_merge_next(entries, clip_end=10.0) is None


# --- Discord modal component limits -------------------------------------------
#
# discord.py does no client-side validation of Discord's own API limits, so
# an over-length label/title silently 400s at runtime the first time the
# modal is opened ("This interaction failed", no useful error) rather than
# failing at import/construction time. _EditSubsModal's label once exceeded
# this and broke every Edit Subs click — guard both modals here so a future
# label/title edit can't silently reintroduce that.

_MODAL_LABEL_MAX = 45
_MODAL_TITLE_MAX = 45


def test_custom_duration_modal_labels_and_title_fit_discord_limits():
    assert len(_CustomDurationModal.title) <= _MODAL_TITLE_MAX
    for name, item in _CustomDurationModal.__modal_children_items__.items():
        assert item.label is not None
        assert len(item.label) <= _MODAL_LABEL_MAX, (name, item.label)


def test_edit_subs_modal_labels_and_title_fit_discord_limits():
    assert len(_EditSubsModal.title) <= _MODAL_TITLE_MAX
    for name, item in _EditSubsModal.__modal_children_items__.items():
        assert item.label is not None
        assert len(item.label) <= _MODAL_LABEL_MAX, (name, item.label)
