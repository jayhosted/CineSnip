from app.bot.cogs.gif import (
    _CustomDurationModal,
    _EditSubsModal,
    _MergeCountModal,
    _entries_in_window,
    _find_merge_next,
    _find_merge_previous,
    _find_unmerge_next,
    _find_unmerge_previous,
    _format_edit_blocks,
    _merge_context_block,
    _merge_next_n,
    _merge_previous_n,
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


# --- merge boundary must actually include the merged-in entry ---------------
#
# _entries_in_window's overlap check is strict (e.end > clip_start and
# e.start < clip_end) — a new clip boundary that lands exactly on the
# merged-in entry's OTHER edge (e.g. new clip_start == entry.end, rather
# than entry.start) gets excluded by that check. The video would extend
# but the merged line's text would never actually render. These pin down
# which boundary field the caller (ClipEditView._on_merge_previous/_next)
# must use — not just which entry gets found.


def test_merge_previous_boundary_keeps_the_entry_in_the_new_window():
    entries = [_entry(1, 5.0, 8.0, "previous line"), _entry(2, 10.0, 12.0, "current")]
    entry = _find_merge_previous(entries, clip_start=10.0)
    new_window = _entries_in_window(entries, clip_start=entry.start, clip_end=12.0)
    assert entry in new_window


def test_merge_next_boundary_keeps_the_entry_in_the_new_window():
    entries = [_entry(1, 5.0, 8.0, "current"), _entry(2, 10.0, 12.0, "next line")]
    entry = _find_merge_next(entries, clip_end=8.0)
    new_window = _entries_in_window(entries, clip_start=5.0, clip_end=entry.end)
    assert entry in new_window


# --- _find_unmerge_previous / _find_unmerge_next -----------------------------


def test_find_unmerge_previous_returns_the_second_earliest_entry():
    entries = [
        _entry(1, 0.0, 2.0, "first"),
        _entry(2, 5.0, 8.0, "second"),
        _entry(3, 10.0, 12.0, "third"),
    ]
    entry = _find_unmerge_previous(entries, clip_start=0.0, clip_end=12.0)
    assert entry.text == "second"


def test_find_unmerge_previous_returns_none_with_only_one_entry_in_window():
    entries = [_entry(1, 0.0, 2.0, "only")]
    assert _find_unmerge_previous(entries, clip_start=0.0, clip_end=2.0) is None


def test_find_unmerge_next_returns_the_second_latest_entry():
    entries = [
        _entry(1, 0.0, 2.0, "first"),
        _entry(2, 5.0, 8.0, "second"),
        _entry(3, 10.0, 12.0, "third"),
    ]
    entry = _find_unmerge_next(entries, clip_start=0.0, clip_end=12.0)
    assert entry.text == "second"


def test_find_unmerge_next_returns_none_with_only_one_entry_in_window():
    entries = [_entry(1, 0.0, 2.0, "only")]
    assert _find_unmerge_next(entries, clip_start=0.0, clip_end=2.0) is None


def test_unmerge_previous_boundary_drops_the_first_entry_but_keeps_the_second():
    entries = [_entry(1, 0.0, 2.0, "dropped"), _entry(2, 5.0, 8.0, "kept")]
    entry = _find_unmerge_previous(entries, clip_start=0.0, clip_end=8.0)
    new_window = _entries_in_window(entries, clip_start=entry.start, clip_end=8.0)
    assert [e.text for e in new_window] == ["kept"]


def test_unmerge_next_boundary_drops_the_last_entry_but_keeps_the_second_to_last():
    entries = [_entry(1, 0.0, 2.0, "kept"), _entry(2, 5.0, 8.0, "dropped")]
    entry = _find_unmerge_next(entries, clip_start=0.0, clip_end=8.0)
    new_window = _entries_in_window(entries, clip_start=0.0, clip_end=entry.end)
    assert [e.text for e in new_window] == ["kept"]


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


def test_merge_count_modal_labels_and_title_fit_discord_limits():
    assert len(_MergeCountModal.title) <= _MODAL_TITLE_MAX
    for name, item in _MergeCountModal.__modal_children_items__.items():
        assert item.label is not None
        assert len(item.label) <= _MODAL_LABEL_MAX, (name, item.label)


# --- _merge_previous_n / _merge_next_n ---------------------------------------


def test_merge_previous_n_walks_back_the_requested_number_of_lines():
    entries = [
        _entry(1, 0.0, 2.0, "first"),
        _entry(2, 3.0, 5.0, "second"),
        _entry(3, 6.0, 8.0, "third"),
    ]
    # From clip_start=10.0, two steps back: third (6-8), then second (3-5).
    result = _merge_previous_n(entries, clip_start=10.0, count=2)
    assert result == 3.0


def test_merge_previous_n_stops_early_when_fewer_lines_exist_than_requested():
    entries = [_entry(1, 6.0, 8.0, "only")]
    result = _merge_previous_n(entries, clip_start=10.0, count=5)
    assert result == 6.0


def test_merge_previous_n_returns_none_with_no_lines_available():
    assert _merge_previous_n([], clip_start=10.0, count=2) is None


def test_merge_next_n_walks_forward_the_requested_number_of_lines():
    entries = [
        _entry(1, 2.0, 4.0, "first"),
        _entry(2, 5.0, 7.0, "second"),
        _entry(3, 8.0, 10.0, "third"),
    ]
    # From clip_end=0.0, two steps forward: first (2-4), then second (5-7).
    result = _merge_next_n(entries, clip_end=0.0, count=2)
    assert result == 7.0


def test_merge_next_n_stops_early_when_fewer_lines_exist_than_requested():
    entries = [_entry(1, 2.0, 4.0, "only")]
    result = _merge_next_n(entries, clip_end=0.0, count=5)
    assert result == 4.0


def test_merge_next_n_returns_none_with_no_lines_available():
    assert _merge_next_n([], clip_end=0.0, count=2) is None


# --- _merge_context_block -----------------------------------------------------


def test_merge_context_block_shows_current_and_surrounding_lines():
    entries = [
        _entry(1, 0.0, 1.5, "before"),
        _entry(2, 5.0, 7.0, "current"),
        _entry(3, 10.0, 11.0, "after"),
    ]
    block = _merge_context_block(entries, clip_start=5.0, clip_end=7.0)
    lines = block.splitlines()
    assert lines == [
        "> before (1.5s)",
        "» current (2.0s)",
        "> after (1.0s)",
    ]


def test_merge_context_block_limits_to_the_context_count_on_each_side():
    entries = [
        _entry(1, 0.0, 1.0, "far before"),
        _entry(2, 2.0, 3.0, "near before"),
        _entry(3, 10.0, 11.0, "near after"),
        _entry(4, 12.0, 13.0, "far after"),
    ]
    block = _merge_context_block(entries, clip_start=5.0, clip_end=6.0, context=1)
    lines = block.splitlines()
    assert lines == ["> near before (1.0s)", "> near after (1.0s)"]


def test_merge_context_block_empty_with_no_entries():
    assert _merge_context_block([], clip_start=5.0, clip_end=6.0) == ""


def test_merge_context_block_collapses_embedded_newlines_in_subtitle_text():
    # A two-speaker subtitle card ("- Yes.\n- Many questions remain.") is
    # collapsed to one line — this now renders in the embed footer, which
    # Discord never markdown-parses, but keeping each entry on one line is
    # still clearer for a compact readout regardless.
    entries = [_entry(1, 5.0, 7.0, "- Yes.\n- Many questions remain.")]
    block = _merge_context_block(entries, clip_start=5.0, clip_end=7.0)
    assert "\n- " not in block
    assert "» - Yes. - Many questions remain. (2.0s)" in block
