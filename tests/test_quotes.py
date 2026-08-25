import pytest

from app.worker.quotes import find_quote_matches, normalize_for_match, strip_markup
from app.worker.subtitles import SubtitleEntry


def _entries(*specs):
    return [
        SubtitleEntry(index=i + 1, start=start, end=end, text=text)
        for i, (start, end, text) in enumerate(specs)
    ]


# --- normalize_for_match / strip_markup -----------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("<i>Hello</i> world", "hello world"),
        ("{\\an8}Top of screen", "top of screen"),
        ("Line one\nLine two", "line one line two"),
        ("He said ‘hi’", "he said hi"),
        ("don’t", "dont"),
        ("don't", "dont"),
        ("Wait—no", "wait no"),
        ("[DOOR SLAMS] Get out!", "get out"),
        ("(sighs) Fine.", "fine"),
        ("♪♪ Some song ♪♪", "some song"),
        ("- Are you coming?", "are you coming"),
        ("JOHN: I'm here.", "im here"),
        ("MiXeD CaSe TeXt", "mixed case text"),
        ("Wait...!", "wait"),
        ("café", "caf"),
    ],
)
def test_normalize_for_match(text, expected):
    assert normalize_for_match(text) == expected


def test_strip_markup_preserves_case_and_punctuation():
    text = strip_markup("<i>Hello, world!</i>")
    assert text == "Hello, world!"


def test_strip_markup_collapses_multiline_cue():
    assert strip_markup("Line one\nLine two") == "Line one Line two"


# --- find_quote_matches: single-cue matching -------------------------------


def test_exact_single_cue_match_scores_100():
    entries = _entries((10.0, 12.0, "Here's Johnny!"))
    matches = find_quote_matches(entries, "Here's Johnny!")
    assert len(matches) == 1
    assert matches[0].score == 100.0
    assert matches[0].start == 10.0
    assert matches[0].entry_indices == (0,)


def test_fuzzy_single_cue_match_with_typo_still_ranks_first():
    entries = _entries(
        (10.0, 12.0, "I'm gonna make him an offer he can't refuse."),
        (20.0, 22.0, "Completely unrelated line about breakfast."),
    )
    matches = find_quote_matches(entries, "make him an offer he cant refuze")
    assert matches[0].entry_indices == (0,)
    assert matches[0].score > 50.0


def test_short_fragment_of_long_cue_still_ranks_first():
    entries = _entries(
        (10.0, 14.0, "Frankly, my dear, I don't give a damn about any of this."),
        (20.0, 22.0, "Something else entirely."),
    )
    matches = find_quote_matches(entries, "I don't give a damn")
    assert matches[0].entry_indices == (0,)


# --- find_quote_matches: adjacent-window matching --------------------------


def test_quote_split_across_adjacent_cues_is_matched_as_a_window():
    """CLAUDE.md Section 5 names lines split across subtitle entries as a
    known hard case. Single-entry matching alone cannot reach a quote like
    "I am your father" when a subtitle file splits it as "I am" / "your
    father" across two consecutive cues — neither cue alone contains the
    full phrase. This asserts the adjacent-window candidate wins, spans
    both entries' start/end, and outscores either constituent cue matched
    alone.
    """
    entries = _entries(
        (0.0, 1.0, "Something before."),
        (2.0, 3.0, "Something after."),
        (4.0, 4.0, "filler"),
        (5.0, 6.0, "filler"),
        (10.0, 11.0, "I am"),
        (11.5, 13.0, "your father."),
    )
    matches = find_quote_matches(entries, "I am your father")

    assert matches[0].entry_indices == (4, 5)
    assert matches[0].start == 10.0
    assert matches[0].end == 13.0

    solo_first = find_quote_matches([entries[4]], "I am your father")
    solo_second = find_quote_matches([entries[5]], "I am your father")
    best_solo = max(
        [m.score for m in solo_first] + [m.score for m in solo_second], default=0.0
    )
    assert matches[0].score > best_solo


def test_adjacent_window_suppressed_across_a_scene_gap():
    entries = _entries(
        (10.0, 11.0, "I am"),
        (20.0, 21.0, "your father."),
    )
    matches = find_quote_matches(
        entries, "I am your father", max_window_gap_seconds=3.0
    )
    assert all(len(m.entry_indices) == 1 for m in matches)


# --- find_quote_matches: ranking, dedup, limits ----------------------------


def test_results_are_sorted_descending_by_score():
    entries = _entries(
        (0.0, 1.0, "Completely different sentence about weather."),
        (10.0, 12.0, "Here's Johnny!"),
        (20.0, 22.0, "Here's Johnny"),
    )
    matches = find_quote_matches(entries, "Here's Johnny!", limit=3)
    scores = [m.score for m in matches]
    assert scores == sorted(scores, reverse=True)


def test_exact_match_suppresses_its_containing_window():
    entries = _entries(
        (10.0, 11.0, "Here's Johnny!"),
        (11.5, 12.0, "unrelated filler"),
    )
    matches = find_quote_matches(entries, "Here's Johnny!", limit=3)
    assert matches[0].entry_indices == (0,)
    all_indices = set()
    for m in matches:
        assert not (all_indices & set(m.entry_indices))
        all_indices.update(m.entry_indices)


def test_limit_caps_result_count_and_all_results_have_disjoint_indices():
    entries = _entries(
        *[(float(i * 5), float(i * 5 + 2), f"Here's Johnny number {i}") for i in range(10)]
    )
    matches = find_quote_matches(entries, "Here's Johnny", limit=3)
    assert len(matches) == 3
    seen = set()
    for m in matches:
        assert not (seen & set(m.entry_indices))
        seen.update(m.entry_indices)


def test_garbage_quote_below_min_score_returns_empty():
    entries = _entries((10.0, 12.0, "Here's Johnny!"))
    matches = find_quote_matches(entries, "zzzz qqqq xyzzy", min_score=50.0)
    assert matches == []


@pytest.mark.parametrize(
    "entries_specs,quote",
    [
        ([], "anything"),
        ([(10.0, 12.0, "Here's Johnny!")], ""),
        ([(10.0, 12.0, "Here's Johnny!")], "..."),
    ],
)
def test_empty_inputs_return_no_matches(entries_specs, quote):
    entries = _entries(*entries_specs)
    assert find_quote_matches(entries, quote) == []


def test_cues_that_normalize_to_empty_are_never_matched_but_appear_as_context():
    entries = _entries(
        (0.0, 1.0, "♪♪"),
        (2.0, 4.0, "Here's Johnny!"),
        (5.0, 6.0, "[MUSIC PLAYING]"),
    )
    matches = find_quote_matches(entries, "Here's Johnny!")
    assert matches[0].entry_indices == (1,)
    assert matches[0].context_before == ("♪♪",)
    assert matches[0].context_after == ("[MUSIC PLAYING]",)


def test_out_of_order_srt_indices_do_not_affect_pairing_or_context():
    """Mirrors tests/test_subtitles.py's
    test_parse_srt_preserves_out_of_order_entries_without_reordering:
    SubtitleEntry.index reflects the raw SRT file order, which can be out
    of order relative to chronological start time. Window pairing and
    context must key off list position, not .index, or an out-of-order
    file would silently mis-pair cues.
    """
    entries = [
        SubtitleEntry(index=99, start=10.0, end=11.0, text="I am"),
        SubtitleEntry(index=1, start=11.5, end=13.0, text="your father."),
    ]
    matches = find_quote_matches(entries, "I am your father")
    assert matches[0].entry_indices == (0, 1)


def test_first_and_last_entries_have_empty_context_on_open_side():
    entries = _entries(
        (0.0, 1.0, "First line."),
        (2.0, 3.0, "Second line."),
        (4.0, 5.0, "Third line."),
    )
    matches = find_quote_matches(entries, "First line.")
    assert matches[0].context_before == ()

    matches = find_quote_matches(entries, "Third line.")
    assert matches[0].context_after == ()
