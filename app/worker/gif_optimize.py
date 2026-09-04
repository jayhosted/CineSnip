from __future__ import annotations

import asyncio

from app.worker.subprocess_utils import run_and_capture

# Candidate --lossy values, ascending (lowest N == least quality loss).
# Tuned against gifsicle 1.96 with --gamma=1 (see this plan's header for
# the full rationale) — --lossy=5 alone cleared the 9.5 MiB target on
# every "normal" clip tested during development; the higher values exist
# for the rare long/high-motion clip. Deliberately excludes anything past
# 30: diminishing returns are steep past that point on every clip tested,
# and if the fallback loop below ever needs to reach past this whole set,
# _DOWNSCALE_TIERS in api.py is the intended next step, not a bigger N.
_LOSSY_CANDIDATES: tuple[int, ...] = (2, 5, 10, 20, 30)


def _pick_best(baseline: bytes, lossy_results: list[bytes | None], max_bytes: int) -> bytes:
    """Picks the best result among a baseline and a set of --lossy
    candidates run concurrently (originally all 5 at once; now one
    _GROUP_SIZE-sized group at a time — see _optimize_gif_unsafe).
    lossy_results is ordered the same as the candidates it was called
    with (ascending N, i.e. best-quality-first) — a None entry means that
    candidate's gifsicle pass failed and is skipped.

    Prefers the *first* (lowest-N, highest-quality) candidate that fits
    max_bytes over any smaller-but-lower-quality one — we only need to
    clear budget, not minimize size once budget is already met. If
    nothing fits (baseline included), returns whichever attempt was
    smallest, matching api.py's _render_within_size_limit fallback
    philosophy of never erroring out."""
    fitting_candidates = [
        result
        for result in lossy_results
        if result is not None and len(result) <= max_bytes
    ]
    if fitting_candidates:
        return fitting_candidates[0]
    if len(baseline) <= max_bytes:
        return baseline

    all_attempts = [baseline] + [r for r in lossy_results if r is not None]
    return min(all_attempts, key=len)


class GifOptimizeError(RuntimeError):
    pass


async def _run_gifsicle_to_bytes(
    args: list[str], gif_bytes: bytes, timeout_seconds: float, error_prefix: str
) -> bytes:
    """Runs gifsicle over stdin/stdout (`-` for both input and output) so
    no scratch-directory temp file is ever created for this pass — avoids
    the whole read/write/cleanup lifecycle repeating per candidate."""
    result = await run_and_capture(
        ["gifsicle", *args, "-", "-o", "-"],
        timeout_seconds,
        error_prefix,
        capture_stdout=True,
        stdin_data=gif_bytes,
    )
    assert result is not None  # capture_stdout=True guarantees bytes, not None
    return result


# Candidates run in fixed-size ascending groups rather than all at once —
# issue #17's concurrency benchmarking found launching all 5 in parallel
# repeatedly wasted the majority of the CPU (real oversized clips only
# ever needed the first 1-2 candidates to find a fit; the rest were
# thrown away by _pick_best) while also being the single biggest driver
# of contention when several users render at once. A bounded group still
# gets some parallelism — most clips resolve within one group, so
# solo-request latency stays close to the fully-parallel case — but caps
# how many gifsicle processes ever run at once, which is what actually
# matters once multiple renders overlap. Group size of 2 was the smallest
# grouping that never lost to full parallelism on solo latency in that
# benchmarking, on either of the two real oversized clips tested.
_GROUP_SIZE = 2


async def _optimize_gif_unsafe(
    gif_bytes: bytes, max_bytes: int, timeout_seconds: float, full_parallel: bool = False
) -> bytes:
    """The real optimization logic, allowed to raise anything — every
    exception is normalized to GifOptimizeError by optimize_gif() below."""

    async def _lossy_attempt(lossy: int) -> bytes | None:
        try:
            return await _run_gifsicle_to_bytes(
                ["-O3", f"--lossy={lossy}", "--gamma=1"],
                gif_bytes,
                timeout_seconds,
                f"gifsicle --lossy={lossy}",
            )
        except Exception:
            # One candidate failing (e.g. a timeout on an especially
            # large input) shouldn't sink the whole optimization —
            # _pick_best skips None entries and falls back to
            # whichever candidates did succeed.
            return None

    # full_parallel (set by api.py only when this is the sole active render
    # right now — see app.state.active_renders) skips the group cap
    # entirely: _GROUP_SIZE exists solely to bound gifsicle/CPU contention
    # across *concurrent* renders (issue #17's benchmark), which doesn't
    # apply when nothing else is competing for the same cores. Group size
    # never affects which candidate wins (ascending-index selection is
    # unchanged either way) — only how many run at once before that
    # selection happens.
    group_size = len(_LOSSY_CANDIDATES) if full_parallel else _GROUP_SIZE

    smallest: bytes | None = None
    for group_start in range(0, len(_LOSSY_CANDIDATES), group_size):
        group = _LOSSY_CANDIDATES[group_start : group_start + group_size]
        if group_start == 0:
            # The lossless -O3 baseline races alongside the *first* lossy
            # group instead of running to completion on its own first — on
            # a genuinely oversized clip, -O3 alone rarely closes the gap
            # (a normal clip needs at least --lossy=5 to clear budget per
            # _LOSSY_CANDIDATES' own tuning notes), so waiting on it
            # serially before starting lossy work that's very likely
            # needed anyway was pure latency with nothing bought for it.
            # Reusing group_size (rather than a separately-tuned ratio
            # threshold) for how many lossy candidates race alongside it
            # keeps the extra contention this adds bounded exactly the
            # same way _GROUP_SIZE already bounds it: 2 extra gifsicle
            # processes under real contention, or effectively free extra
            # parallelism on otherwise-idle cores when full_parallel.
            # Baseline still wins outright if it fits — nothing here
            # trades away the lossless result when it's actually good
            # enough, it just no longer blocks starting the lossy work.
            baseline, *group_results = await asyncio.gather(
                _run_gifsicle_to_bytes(["-O3"], gif_bytes, timeout_seconds, "gifsicle -O3"),
                *(_lossy_attempt(n) for n in group),
            )
            if len(baseline) <= max_bytes:
                return baseline
            smallest = baseline
        else:
            # gather() preserves input order in its result list regardless
            # of completion order, so _pick_best always picks by ascending
            # N (lowest loss) — never by whichever candidate happens to
            # finish first. Only once this whole group has completed does
            # the next group (if needed) get launched.
            group_results = await asyncio.gather(*(_lossy_attempt(n) for n in group))
        # `smallest` threads forward as _pick_best's "baseline" argument on
        # each iteration — since it's already established not to fit (we
        # only keep looping while that's true), _pick_best's own
        # fitting-check against it is a no-op, and its "nothing fit"
        # fallback naturally folds this group's attempts into the running
        # smallest-so-far, carrying it into the next group unchanged.
        smallest = _pick_best(smallest, group_results, max_bytes)
        if len(smallest) <= max_bytes:
            return smallest
    return smallest


async def optimize_gif(
    gif_bytes: bytes, max_bytes: int, timeout_seconds: float = 60.0, full_parallel: bool = False
) -> bytes:
    """Re-optimizes an already-rendered GIF with gifsicle, without ever
    touching resolution or frame rate. Races the -O3 (lossless) baseline
    against the first --lossy=N --gamma=1 group rather than waiting on it
    alone first (see _optimize_gif_unsafe for why); if neither the
    baseline nor that group fits max_bytes, tries the rest of
    _LOSSY_CANDIDATES, _GROUP_SIZE at a time (or all at once if
    full_parallel), stopping as soon as a group produces a fit (see
    _optimize_gif_unsafe/_pick_best for the selection rules). The
    lossless baseline still always wins if it fits, regardless of what
    the lossy candidates racing alongside it produced. --gamma=1 is
    required on gifsicle >=1.96 — it
    restores the pre-1.96 linear-space --lossy math this candidate list
    was tuned against; the 1.96 default (sRGB-perceptual) needs much
    larger N values for equivalent compression. Runs entirely over
    stdin/stdout (`-`/`-o -`) — no scratch-directory temp file is ever
    written for this tier.

    Only ever raises GifOptimizeError, never a bare OSError/RuntimeError —
    the whole function body runs under one try/except so *any* failure
    (missing binary, corrupt input, or anything else) degrades to
    _render_within_size_limit's downscale-tier fallback in api.py, rather
    than surfacing as an unhandled 500."""
    try:
        return await _optimize_gif_unsafe(gif_bytes, max_bytes, timeout_seconds, full_parallel)
    except Exception as exc:
        raise GifOptimizeError(f"gifsicle optimization failed: {exc}") from exc
