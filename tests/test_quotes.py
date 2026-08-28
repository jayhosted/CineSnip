import pytest

from app.worker.quotes import (
    find_quote_matches,
    normalize_for_match,
    strip_markup,
)
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


def test_literal_substring_match_ranks_above_similar_but_non_matching_lines():
    """Real bug found on the live library: searching "Hitler" in Peep Show
    returned several lines that don't contain the word ranked ahead of ones
    that do — WRatio's length-normalized scoring diluted a short quote
    buried in a longer line below unrelated same-length lines that merely
    share similar letters. A literal occurrence of the quote must always
    outrank a same-score-or-higher fuzzy-only match.
    """
    entries = _entries(
        (0.0, 1.0, "Little Hitler, that's what she called him."),
        (10.0, 11.0, "He's a right little bother, isn't he."),
        (20.0, 21.0, "Hither and thither, all over the place."),
        (30.0, 31.0, "We watched a documentary about Hitler last night."),
    )
    matches = find_quote_matches(entries, "Hitler", limit=4)

    literal_indices = {0, 3}
    literal_scores = [m.score for m in matches if m.entry_indices[0] in literal_indices]
    non_literal_scores = [
        m.score for m in matches if m.entry_indices[0] not in literal_indices
    ]
    assert literal_scores and all(s == 100.0 for s in literal_scores)
    assert not non_literal_scores or min(literal_scores) > max(non_literal_scores)
    # Both literal matches must appear ahead of every non-literal one.
    literal_positions = [
        i for i, m in enumerate(matches) if m.entry_indices[0] in literal_indices
    ]
    assert literal_positions == [0, 1]


def test_literal_match_word_boundary_does_not_match_inside_another_word():
    entries = _entries((10.0, 12.0, "The situation was completely concatenated today."))
    matches = find_quote_matches(entries, "cat", limit=1)
    # "concatenated" contains "cat" as a raw substring but not as a whole
    # word — must not be force-scored to 100 off that alone.
    assert matches[0].score < 100.0


def test_partial_word_overlap_ranks_above_unrelated_lines():
    """A multi-word quote whose words are all present in a candidate, just
    out of order/interleaved with other words, isn't a literal substring
    match — this is what the directional word-overlap bonus is for. The
    truly unrelated line shares no real words with the quote, so it's
    correctly suppressed entirely (see the low-overlap score cap below),
    leaving only the genuine match.
    """
    entries = _entries(
        (0.0, 1.0, "Your father, I am, whether you like it or not."),
        (10.0, 11.0, "Completely unrelated line about the weather."),
    )
    matches = find_quote_matches(entries, "I am your father", limit=2)
    assert len(matches) == 1
    assert matches[0].entry_indices == (0,)


def test_low_word_overlap_candidate_is_suppressed_even_when_wratio_scores_it_high():
    """Real bug found via a full-library search (11,463 titles): WRatio
    itself — not the overlap bonus — can score a short fragment sharing
    only a word or two with a much longer quote surprisingly high, via its
    internal partial-ratio weighting for large length-ratio pairs.
    Confirmed: WRatio("assistant to the regional manager", "to the") == 90,
    with only "to"/"the" (2 of 5 query words) actually present. Invisible
    at small scale — not enough short coincidental-overlap candidates
    existed to surface it — but at real-library scale this flooded results
    with noise ahead of the genuine match. A candidate missing most of a
    multi-word quote's actual words must never survive regardless of what
    WRatio's raw score says.
    """
    entries = _entries(
        (0.0, 1.0, "To the moon and back, my friend."),
        (10.0, 12.0, "She was named Assistant to the Regional Manager today."),
    )
    matches = find_quote_matches(entries, "Assistant to the Regional Manager", limit=5)
    assert [m.entry_indices for m in matches] == [(1,)]


def test_short_candidate_subset_of_long_quote_is_not_inflated():
    """Guards against ever reintroducing the exact bug found while designing
    this: rapidfuzz's token_set_ratio scores a short candidate that's a
    strict word-subset of a much longer quote as a perfect 100 (confirmed:
    token_set_ratio("i am", "i am your father") == 100.0), which would tie
    or beat a genuine full match. The directional overlap bonus must never
    push a truncated candidate ("I am", missing most of "I am your father")
    to equal or exceed a real one — WRatio alone already scores this
    particular pair at 90 (a separate, pre-existing property of WRatio
    itself, not something this bonus should ever add to), so the invariant
    that actually matters is the ordering, not an absolute number.
    """
    entries = _entries(
        (0.0, 1.0, "I am."),
        (10.0, 11.0, "Look, I am your father, this changes everything."),
    )
    matches = find_quote_matches(entries, "I am your father", limit=2)
    assert matches[0].entry_indices == (1,)
    assert matches[0].score == 100.0
    truncated = next(m for m in matches if m.entry_indices == (0,))
    assert truncated.score < matches[0].score


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
