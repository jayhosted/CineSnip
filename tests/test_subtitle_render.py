import pytest

from app.worker.subtitle_render import (
    STYLE_PRESETS,
    apply_overrides,
    build_ass_document,
    entries_in_window,
)
from app.worker.subtitles import SubtitleEntry


def _entry(index, start, end, text):
    return SubtitleEntry(index=index, start=start, end=end, text=text)


# --- entries_in_window ----------------------------------------------------


def test_entries_in_window_rebases_to_clip_relative_time():
    entries = [_entry(1, 100.0, 102.0, "hello")]
    window = entries_in_window(entries, clip_start=98.0, clip_end=104.0)
    assert window == [_entry(1, 2.0, 4.0, "hello")]


def test_entries_in_window_excludes_entries_outside_the_clip():
    entries = [
        _entry(1, 0.0, 2.0, "before"),
        _entry(2, 10.0, 12.0, "during"),
        _entry(3, 20.0, 22.0, "after"),
    ]
    window = entries_in_window(entries, clip_start=9.0, clip_end=13.0)
    assert [e.text for e in window] == ["during"]


def test_entries_in_window_trims_a_partially_overlapping_entry():
    # An entry that starts before the clip and ends inside it (or vice
    # versa) must be clamped to the clip's own span, not dropped or left
    # pointing outside [0, clip_duration].
    entries = [_entry(1, 8.0, 12.0, "spans the cut")]
    window = entries_in_window(entries, clip_start=10.0, clip_end=20.0)
    assert window == [_entry(1, 0.0, 2.0, "spans the cut")]


def test_entries_in_window_clamps_end_to_clip_duration():
    entries = [_entry(1, 18.0, 25.0, "runs past the end")]
    window = entries_in_window(entries, clip_start=10.0, clip_end=20.0)
    assert window == [_entry(1, 8.0, 10.0, "runs past the end")]


def test_entries_in_window_returns_nothing_for_no_overlap():
    entries = [_entry(1, 0.0, 2.0, "nowhere near")]
    assert entries_in_window(entries, clip_start=100.0, clip_end=104.0) == []


# --- build_ass_document -----------------------------------------------------


def test_build_ass_document_includes_play_res_and_style_fields():
    style = STYLE_PRESETS["classic"]
    doc = build_ass_document([], style, play_res_x=480, play_res_y=270)
    assert "PlayResX: 480" in doc
    assert "PlayResY: 270" in doc
    assert style.font in doc
    assert style.primary_color in doc


def test_build_ass_document_formats_dialogue_timestamps():
    entries = [_entry(1, 1.5, 63.25, "hi")]
    doc = build_ass_document(entries, STYLE_PRESETS["classic"], 480, 270)
    assert "Dialogue: 0,0:00:01.50,0:01:03.25,Default,,0,0,0,,hi" in doc


def test_build_ass_document_strips_markup_from_dialogue_text():
    entries = [_entry(1, 0.0, 1.0, "<i>- Hello</i>\nthere")]
    doc = build_ass_document(entries, STYLE_PRESETS["classic"], 480, 270)
    assert "Hello there" in doc
    assert "<i>" not in doc


def test_build_ass_document_uppercases_text_for_meme_style():
    entries = [_entry(1, 0.0, 1.0, "shout this")]
    doc = build_ass_document(entries, STYLE_PRESETS["meme"], 480, 270)
    assert "SHOUT THIS" in doc


def test_build_ass_document_escapes_literal_braces_and_backslashes():
    # strip_markup already removes *matched* {..} pairs as ASS override
    # syntax, so an unmatched brace (or a literal backslash) is what can
    # actually survive to reach the escaping step — and libass would parse
    # either unescaped as the start of an override/escape sequence instead
    # of a literal character.
    entries = [_entry(1, 0.0, 1.0, "wait } now\\then")]
    doc = build_ass_document(entries, STYLE_PRESETS["classic"], 480, 270)
    assert "wait \\} now\\\\then" in doc


def test_build_ass_document_preserves_bracket_cues_like_real_subtitles_do():
    # [MUSIC]/[door slams]-style cues are display content a real sidecar
    # subtitle would show — strip_markup deliberately preserves them
    # (unlike normalize_for_match's matching-only stripping), so burn-in
    # should render them as-is, not silently drop them.
    entries = [_entry(1, 0.0, 1.0, "[MUSIC PLAYING]")]
    doc = build_ass_document(entries, STYLE_PRESETS["classic"], 480, 270)
    assert "[MUSIC PLAYING]" in doc


def test_build_ass_document_skips_entries_that_strip_to_empty():
    entries = [_entry(1, 0.0, 1.0, "<i></i>")]
    doc = build_ass_document(entries, STYLE_PRESETS["classic"], 480, 270)
    assert "Dialogue:" not in doc


@pytest.mark.parametrize("preset_name", list(STYLE_PRESETS.keys()))
def test_all_presets_produce_a_parseable_style_line(preset_name):
    doc = build_ass_document([], STYLE_PRESETS[preset_name], 480, 270)
    style_line = next(line for line in doc.splitlines() if line.startswith("Style: Default,"))
    fields = style_line.removeprefix("Style: ").split(",")
    assert len(fields) == 23  # matches the declared Format: field count


# --- apply_overrides --------------------------------------------------------


def test_apply_overrides_passes_through_entries_with_no_override():
    entries = [_entry(1, 0.0, 2.0, "hello")]
    assert apply_overrides(entries, {}) == entries


def test_apply_overrides_replaces_text_for_a_matched_index():
    entries = [_entry(1, 0.0, 2.0, "hello")]
    result = apply_overrides(entries, {1: "goodbye"})
    assert result == [_entry(1, 0.0, 2.0, "goodbye")]


def test_apply_overrides_suppresses_an_entry_mapped_to_none():
    entries = [_entry(1, 0.0, 2.0, "hello"), _entry(2, 2.0, 4.0, "world")]
    result = apply_overrides(entries, {1: None})
    assert result == [_entry(2, 2.0, 4.0, "world")]


def test_apply_overrides_ignores_indices_not_present_in_the_window():
    entries = [_entry(1, 0.0, 2.0, "hello")]
    result = apply_overrides(entries, {99: "unused"})
    assert result == entries


def test_apply_overrides_preserves_entry_order():
    entries = [_entry(1, 0.0, 2.0, "a"), _entry(2, 2.0, 4.0, "b"), _entry(3, 4.0, 6.0, "c")]
    result = apply_overrides(entries, {2: "B"})
    assert [e.text for e in result] == ["a", "B", "c"]
