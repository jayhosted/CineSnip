from __future__ import annotations

import json
import re
import unicodedata
import uuid
from dataclasses import dataclass
from pathlib import Path

from rapidfuzz import fuzz, process

from app.worker.subtitles import SubtitleEntry, cache_path_for_guid

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


# --- Candidate cache --------------------------------------------------------
#
# Normalizing every subtitle line and building the single-line/adjacent-window
# candidate list is deterministic given a title's raw entries — it doesn't
# depend on the search query at all, yet find_quote_matches() used to redo it
# on every single search. Measured on the real library: this step was 53% of
# total search time (3.57s of 6.77s across 216 cached titles), by far the
# biggest piece — bigger than the disk read (7%) and even the actual
# per-query fuzzy scoring (26%), which is the one part that genuinely can't
# be cached. Persisting the built candidates to disk (not in memory) was a
# deliberate choice over an in-process cache: it adds no RAM growth and no
# new staleness class (an in-memory version would keep serving stale
# candidates after a title's subtitles get re-extracted, until the worker
# process restarts — this file-based version is invalidated by mtime,
# exactly like the raw subtitle cache's own fingerprint check).


def _candidates_cache_path(cache_dir: Path, guid: str) -> Path:
    # Deliberately co-located with the raw subtitle cache file (same digest,
    # different suffix) rather than a separate cache subdirectory — one
    # fewer thing to keep in sync, and easy to spot the pair on disk.
    subtitle_path = cache_path_for_guid(cache_dir, guid)
    return subtitle_path.with_suffix(".candidates.json")


def read_cached_candidates(
    cache_dir: Path, guid: str, max_window_gap_seconds: float
) -> PrecomputedCandidates | None:
    candidates_path = _candidates_cache_path(cache_dir, guid)
    if not candidates_path.exists():
        return None

    # The candidates cache is derived entirely from the raw subtitle cache —
    # if that's been rewritten more recently (a re-sync, an alass fix, a
    # fresh embedded extraction), this is stale by construction, regardless
    # of its own age. No raw cache at all means nothing to trust it against.
    subtitle_path = cache_path_for_guid(cache_dir, guid)
    try:
        if candidates_path.stat().st_mtime < subtitle_path.stat().st_mtime:
            return None
    except OSError:
        return None

    try:
        payload = json.loads(candidates_path.read_text(encoding="utf-8"))
        if payload.get("max_window_gap_seconds") != max_window_gap_seconds:
            return None
        return PrecomputedCandidates(
            displays=payload["displays"],
            candidates=[
                _Candidate(
                    indices=tuple(c["indices"]),
                    start=c["start"],
                    end=c["end"],
                    display_text=c["display_text"],
                    normalized_text=c["normalized_text"],
                )
                for c in payload["candidates"]
            ],
        )
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None


def write_cached_candidates(
    cache_dir: Path,
    guid: str,
    precomputed: PrecomputedCandidates,
    max_window_gap_seconds: float,
) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    final_path = _candidates_cache_path(cache_dir, guid)
    payload = {
        "max_window_gap_seconds": max_window_gap_seconds,
        "displays": precomputed.displays,
        "candidates": [
            {
                "indices": list(c.indices),
                "start": c.start,
                "end": c.end,
                "display_text": c.display_text,
                "normalized_text": c.normalized_text,
            }
            for c in precomputed.candidates
        ],
    }
    tmp_path = final_path.with_suffix(f".json.tmp-{uuid.uuid4().hex}")
    tmp_path.write_text(json.dumps(payload), encoding="utf-8")
    tmp_path.replace(final_path)


def get_or_build_candidates(
    cache_dir: Path,
    guid: str,
    entries: list[SubtitleEntry],
    max_window_gap_seconds: float,
) -> PrecomputedCandidates:
    cached = read_cached_candidates(cache_dir, guid, max_window_gap_seconds)
    if cached is not None:
        return cached

    displays = [strip_markup(e.text) for e in entries]
    normalized = [_normalize_stripped(d) for d in displays]
    candidates = _build_candidates(entries, displays, normalized, max_window_gap_seconds)
    precomputed = PrecomputedCandidates(displays=displays, candidates=candidates)
    write_cached_candidates(cache_dir, guid, precomputed, max_window_gap_seconds)
    return precomputed


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
    # rank it top: its length-normalized scoring can dilute a short quote
    # buried in a much longer line below an unrelated same-length line that
    # merely shares similar letters. Confirmed on the real library —
    # searching "Hitler" across Peep Show returned several lines that don't
    # contain the word ranked ahead of ones that do. Force every literal
    # substring hit to the top of the ranking (also picks up any candidate
    # WRatio scored below min_score despite containing the quote outright).
    literal_pattern = re.compile(r"\b" + re.escape(normalized_quote) + r"\b")
    quote_words = normalized_quote.split()

    for idx, candidate in enumerate(candidates):
        if literal_pattern.search(candidate.normalized_text):
            score_by_candidate_index[idx] = 100.0
            continue

        # Partial word-overlap bonus: catches a multi-word quote whose words
        # are all present in a candidate but out of order or interleaved
        # with other words — not a literal substring, so the check above
        # misses it, and WRatio's character-level scoring doesn't reward
        # word presence directly. Deliberately directional (what fraction of
        # the QUOTE's words appear in the candidate, not the reverse) —
        # rapidfuzz's token_set_ratio was tried and rejected here because
        # it's symmetric: it scores a short candidate that's a strict
        # word-subset of a much longer quote as a perfect 100 (confirmed:
        # token_set_ratio("i am", "i am your father") == 100.0), which would
        # rank an incomplete match as good as a real one — the exact class
        # of bug this whole scoring pass exists to avoid. A single-word
        # quote gets no partial credit (either the literal check above
        # caught it, or it's simply absent); only worth scoring below a
        # clear majority-present threshold to avoid one shared common word
        # inflating an otherwise-unrelated line.
        if len(quote_words) > 1:
            candidate_words = set(candidate.normalized_text.split())
            overlap = sum(1 for w in quote_words if w in candidate_words) / len(quote_words)
            if overlap >= 0.5:
                bonus = 60.0 + (overlap - 0.5) * 70.0  # 0.5 -> 60, 1.0 -> 95
                if bonus > score_by_candidate_index.get(idx, 0.0):
                    score_by_candidate_index[idx] = bonus

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
