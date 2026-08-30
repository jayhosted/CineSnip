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
    """Picks the best result among the lossless baseline and the parallel
    --lossy candidates. lossy_results is ordered the same as
    _LOSSY_CANDIDATES (ascending N, i.e. best-quality-first) — a None
    entry means that candidate's gifsicle pass failed and is skipped.

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


async def _optimize_gif_unsafe(
    gif_bytes: bytes, max_bytes: int, timeout_seconds: float
) -> bytes:
    """The real optimization logic, allowed to raise anything — every
    exception is normalized to GifOptimizeError by optimize_gif() below."""
    baseline = await _run_gifsicle_to_bytes(
        ["-O3"], gif_bytes, timeout_seconds, "gifsicle -O3"
    )
    if len(baseline) <= max_bytes:
        return baseline

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

    lossy_results = await asyncio.gather(
        *(_lossy_attempt(n) for n in _LOSSY_CANDIDATES)
    )
    return _pick_best(baseline, list(lossy_results), max_bytes)


async def optimize_gif(
    gif_bytes: bytes, max_bytes: int, timeout_seconds: float = 60.0
) -> bytes:
    """Re-optimizes an already-rendered GIF with gifsicle, without ever
    touching resolution or frame rate. Tries -O3 (lossless) first; if the
    result still exceeds max_bytes, launches several --lossy=N --gamma=1
    passes (see _LOSSY_CANDIDATES) in parallel and keeps the best-fitting
    one via _pick_best. --gamma=1 is required on gifsicle >=1.96 — it
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
        return await _optimize_gif_unsafe(gif_bytes, max_bytes, timeout_seconds)
    except Exception as exc:
        raise GifOptimizeError(f"gifsicle optimization failed: {exc}") from exc
