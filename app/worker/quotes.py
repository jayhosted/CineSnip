from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from rapidfuzz import fuzz, process

from app.worker.subtitles import SubtitleEntry

# --- Text normalization -------------------------------------------------

_TAG_PATTERN = re.compile(r"<[^>]+>")
_ASS_OVERRIDE_PATTERN = re.compile(r"\{[^}]*\}")
_LEADING_DASH_PATTERN = re.compile(r"^[-–—]\s*", re.MULTILINE)
_WHITESPACE_PATTERN = re.compile(r"\s+")

_QUOTE_MAP = str.maketrans(
    {
        "‘": "'",
        "’": "'",
        "‛": "'",
        "“": '"',
        "”": '"',
        "–": "-",
        "—": "-",
        "…": "...",
    }
)
_BRACKET_CUE_PATTERN = re.compile(r"\[[^\]]*\]|\([^)]*\)")
_MUSIC_GLYPH_PATTERN = re.compile(r"[♪♫♩♬]")
_SPEAKER_PREFIX_PATTERN = re.compile(r"^[A-Z][A-Z' .]{1,20}:\s*")
_APOSTROPHE_PATTERN = re.compile(r"'")
_NON_ALNUM_PATTERN = re.compile(r"[^a-z0-9]+")


def strip_markup(text: str) -> str:
    """Display-safe cleanup: strips formatting markup, preserves case/punctuation."""
    text = _TAG_PATTERN.sub("", text)
    text = _ASS_OVERRIDE_PATTERN.sub("", text)
    text = _LEADING_DASH_PATTERN.sub("", text)
    text = text.replace("\n", " ")
    return _WHITESPACE_PATTERN.sub(" ", text).strip()


def _normalize_stripped(text: str) -> str:
    """The normalize_for_match steps that run after strip_markup — split out
    so callers that already have stripped text (e.g. per-entry caching in
    find_quote_matches) don't pay for strip_markup twice."""
    text = unicodedata.normalize("NFKC", text)
    text = text.translate(_QUOTE_MAP)
    text = _BRACKET_CUE_PATTERN.sub(" ", text)
    text = _MUSIC_GLYPH_PATTERN.sub(" ", text)
    text = _SPEAKER_PREFIX_PATTERN.sub("", text)
    text = text.casefold()
    text = _APOSTROPHE_PATTERN.sub("", text)
    text = _NON_ALNUM_PATTERN.sub(" ", text)
    return _WHITESPACE_PATTERN.sub(" ", text).strip()


def normalize_for_match(text: str) -> str:
    """Reduce display text to a scoring key: case/punctuation/markup insensitive."""
    return _normalize_stripped(strip_markup(text))


# --- Matching -------------------------------------------------------------


@dataclass(frozen=True)
class QuoteMatch:
    start: float
    end: float
    text: str
    score: float
    entry_indices: tuple[int, ...]
    context_before: tuple[str, ...]
    context_after: tuple[str, ...]


@dataclass(frozen=True)
class _Candidate:
    indices: tuple[int, ...]
    start: float
    end: float
    display_text: str
    normalized_text: str


def _build_candidates(
    entries: list[SubtitleEntry],
    displays: list[str],
    normalized: list[str],
    max_window_gap_seconds: float,
) -> list[_Candidate]:
    candidates: list[_Candidate] = []

    for i, entry in enumerate(entries):
        if normalized[i]:
            candidates.append(
                _Candidate((i,), entry.start, entry.end, displays[i], normalized[i])
            )

    for i in range(len(entries) - 1):
        first, second = entries[i], entries[i + 1]
        if second.start - first.end > max_window_gap_seconds:
            continue
        display = f"{displays[i]} {displays[i + 1]}".strip()
        norm = f"{normalized[i]} {normalized[i + 1]}".strip()
        if norm:
            candidates.append(
                _Candidate((i, i + 1), first.start, second.end, display, norm)
            )

    return candidates


@dataclass(frozen=True)
class PrecomputedCandidates:
    displays: list[str]
    candidates: list[_Candidate]


def _context_for(
    displays: list[str], indices: tuple[int, ...], context_lines: int
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    first_index, last_index = indices[0], indices[-1]

    before_start = max(0, first_index - context_lines)
    before = tuple(displays[before_start:first_index])

    after_end = min(len(displays), last_index + 1 + context_lines)
    after = tuple(displays[last_index + 1 : after_end])

    return before, after


def find_quote_matches(
    entries: list[SubtitleEntry],
    quote: str,
    limit: int = 3,
    min_score: float = 50.0,
    max_window_gap_seconds: float = 3.0,
    context_lines: int = 1,
    precomputed: PrecomputedCandidates | None = None,
) -> list[QuoteMatch]:
    if not entries:
        return []

    normalized_quote = normalize_for_match(quote)
    if not normalized_quote:
        return []

    # precomputed skips the normalize+build step entirely — by far the most
    # expensive part of a search (see the Candidate cache section above) and
    # completely query-independent, so a caller with a title it expects to
    # search more than once (get_or_build_candidates()) should pass this in
    # rather than let it get rebuilt from scratch every time.
    if precomputed is not None:
        displays = precomputed.displays
        candidates = precomputed.candidates
    else:
        displays = [strip_markup(e.text) for e in entries]
        normalized = [_normalize_stripped(d) for d in displays]
        candidates = _build_candidates(entries, displays, normalized, max_window_gap_seconds)
    if not candidates:
        return []

    # score_cutoff=0 here (not min_score) because the literal/overlap boosts
    # below can rescue a candidate WRatio itself scored under min_score —
    # the single cutoff that actually matters is applied once, after all
    # three signals are merged, further down.
    scored = process.extract(
        normalized_quote,
        [c.normalized_text for c in candidates],
        scorer=fuzz.WRatio,
        processor=None,
        score_cutoff=0.0,
        limit=None,
    )

    score_by_candidate_index = {idx: score for _, score, idx in scored}

    # A literal (word-boundary) match of the quote inside a candidate is
    # unambiguously the best possible match, but WRatio doesn't reliably
    # rank it top — its length-normalized scoring can dilute a short quote
    # buried in a much longer line below an unrelated same-length line that
    # merely shares similar letters. Force every literal substring hit to
    # the top (also rescues any candidate WRatio scored below min_score
    # despite containing the quote outright). See docs/build-notes/subtitles-and-search.md.
    literal_pattern = re.compile(r"\b" + re.escape(normalized_quote) + r"\b")
    quote_words = normalized_quote.split()

    for idx, candidate in enumerate(candidates):
        if literal_pattern.search(candidate.normalized_text):
            score_by_candidate_index[idx] = 100.0
            continue

        # Partial word-overlap bonus: catches a multi-word quote whose words
        # are all present but out of order/interleaved — not a literal
        # substring, so the check above misses it. Deliberately directional
        # (fraction of the QUOTE's words present in the candidate, never
        # the reverse) — rapidfuzz's token_set_ratio was tried and rejected
        # here because it's symmetric: it scores a short candidate that's a
        # strict word-subset of a much longer quote as a perfect 100
        # (confirmed: token_set_ratio("i am", "i am your father") == 100.0),
        # ranking an incomplete match as good as a real one. A single-word
        # quote gets no partial credit; only scored past a clear
        # majority-present threshold to avoid one shared common word
        # inflating an unrelated line.
        if len(quote_words) > 1:
            candidate_words = set(candidate.normalized_text.split())
            overlap = sum(1 for w in quote_words if w in candidate_words) / len(quote_words)
            if overlap >= 0.5:
                bonus = 60.0 + (overlap - 0.5) * 70.0  # 0.5 -> 60, 1.0 -> 95
                if bonus > score_by_candidate_index.get(idx, 0.0):
                    score_by_candidate_index[idx] = bonus
            else:
                # WRatio itself (not just this bonus) can score a short
                # fragment sharing only a word or two with a much longer
                # quote surprisingly high via its internal partial-ratio
                # weighting for large length-ratio pairs (confirmed:
                # `WRatio("assistant to the regional manager", "to the")
                # == 90.0`). Invisible below full-library scale, but a real
                # correctness bug at scale — cap below this threshold
                # rather than trust WRatio's raw number for this shape of
                # candidate. See docs/build-notes/subtitles-and-search.md.
                cap = overlap * 60.0
                score_by_candidate_index[idx] = min(
                    score_by_candidate_index.get(idx, 0.0), cap
                )

    score_by_candidate_index = {
        idx: score for idx, score in score_by_candidate_index.items() if score >= min_score
    }
    ranked_with_scores = sorted(
        ((candidates[idx], score) for idx, score in score_by_candidate_index.items()),
        key=lambda pair: (-pair[1], len(pair[0].indices), pair[0].start),
    )

    accepted: list[QuoteMatch] = []
    used_indices: set[int] = set()

    for candidate, score in ranked_with_scores:
        if len(accepted) >= limit:
            break
        if used_indices & set(candidate.indices):
            continue

        context_before, context_after = _context_for(
            displays, candidate.indices, context_lines
        )
        accepted.append(
            QuoteMatch(
                start=max(0.0, candidate.start),
                end=candidate.end,
                text=candidate.display_text,
                score=score,
                entry_indices=candidate.indices,
                context_before=context_before,
                context_after=context_after,
            )
        )
        used_indices.update(candidate.indices)

    return accepted
