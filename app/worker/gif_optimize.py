from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

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


async def _run_gifsicle(args: list[str], timeout_seconds: float, error_prefix: str) -> None:
    await run_and_capture(["gifsicle", *args], timeout_seconds, error_prefix)


async def optimize_gif(
    gif_bytes: bytes, max_bytes: int, scratch_dir: Path, timeout_seconds: float = 60.0
) -> bytes:
    """Re-optimizes an already-rendered GIF with gifsicle, without ever
    touching resolution or frame rate. Tries -O3 (lossless) first; if the
    result still exceeds max_bytes, launches several --lossy=N --gamma=1
    passes (see _LOSSY_CANDIDATES) in parallel and keeps the best-fitting
    one via _pick_best. --gamma=1 is required on gifsicle >=1.96 — it
    restores the pre-1.96 linear-space --lossy math this candidate list
    was tuned against; the 1.96 default (sRGB-perceptual) needs much
    larger N values for equivalent compression. Never raises for "still
    too big" — only for an actual gifsicle failure on the lossless pass,
    which indicates a real tooling problem (missing binary, corrupt
    input) rather than an oversized-but-valid clip."""
    scratch_dir.mkdir(parents=True, exist_ok=True)
    src_path = scratch_dir / f"gifopt-src-{uuid.uuid4().hex}.gif"
    src_path.write_bytes(gif_bytes)

    try:
        lossless_path = scratch_dir / f"gifopt-o3-{uuid.uuid4().hex}.gif"
        try:
            await _run_gifsicle(
                ["-O3", str(src_path), "-o", str(lossless_path)],
                timeout_seconds,
                "gifsicle -O3",
            )
            baseline = lossless_path.read_bytes()
        except Exception as exc:
            raise GifOptimizeError(f"gifsicle -O3 lossless pass failed: {exc}") from exc
        finally:
            lossless_path.unlink(missing_ok=True)

        if len(baseline) <= max_bytes:
            return baseline

        async def _lossy_attempt(lossy: int) -> bytes | None:
            out_path = scratch_dir / f"gifopt-lossy{lossy}-{uuid.uuid4().hex}.gif"
            try:
                await _run_gifsicle(
                    ["-O3", f"--lossy={lossy}", "--gamma=1", str(src_path), "-o", str(out_path)],
                    timeout_seconds,
                    f"gifsicle --lossy={lossy}",
                )
                return out_path.read_bytes()
            except Exception:
                # One candidate failing (e.g. a timeout on an especially
                # large input) shouldn't sink the whole optimization —
                # _pick_best skips None entries and falls back to
                # whichever candidates did succeed.
                return None
            finally:
                out_path.unlink(missing_ok=True)

        lossy_results = await asyncio.gather(
            *(_lossy_attempt(n) for n in _LOSSY_CANDIDATES)
        )
        return _pick_best(baseline, list(lossy_results), max_bytes)
    finally:
        src_path.unlink(missing_ok=True)
