import asyncio

import pytest

from app.worker import gif_optimize
from app.worker.gif_optimize import _LOSSY_CANDIDATES, GifOptimizeError, _pick_best, optimize_gif


def test_picks_smallest_n_that_fits_not_the_smallest_overall():
    # Candidate order mirrors _LOSSY_CANDIDATES (ascending N == descending
    # quality loss). The N=20 result is smaller, but N=10 already fits and
    # is higher quality — higher quality must win once budget is met.
    baseline = b"x" * 2000
    lossy_results = [
        b"x" * 1500,  # N=2  (first candidate) - still too big
        b"x" * 1200,  # N=5  - still too big
        b"x" * 900,   # N=10 - fits!
        b"x" * 700,   # N=20 - also fits, but N=10 already won
        b"x" * 600,   # N=30 - also fits, but N=10 already won
    ]
    result = _pick_best(baseline, lossy_results, max_bytes=1000)
    assert result == b"x" * 900


def test_returns_baseline_when_it_already_fits():
    baseline = b"x" * 500
    lossy_results = [None] * len(_LOSSY_CANDIDATES)
    result = _pick_best(baseline, lossy_results, max_bytes=1000)
    assert result == baseline


def test_returns_smallest_available_when_nothing_fits():
    baseline = b"x" * 5000
    lossy_results = [b"x" * 4000, b"x" * 3000, None, b"x" * 3500, b"x" * 2900]
    result = _pick_best(baseline, lossy_results, max_bytes=100)
    assert result == b"x" * 2900


def test_skips_failed_candidates_marked_none():
    baseline = b"x" * 2000
    lossy_results = [None, None, b"x" * 900, None, b"x" * 600]
    result = _pick_best(baseline, lossy_results, max_bytes=1000)
    # N=10 (index 2) is the first fitting non-None candidate.
    assert result == b"x" * 900


def test_lossy_candidates_are_ascending():
    assert list(_LOSSY_CANDIDATES) == sorted(_LOSSY_CANDIDATES)
    assert len(_LOSSY_CANDIDATES) >= 1


# The tests below exercise optimize_gif()'s real control flow (not just the
# pure _pick_best helper above) by faking run_and_capture — the actual
# gifsicle binary isn't assumed to be on the machine running the tests, but
# everything around the subprocess call (stdin/stdout wiring, exception
# normalization to GifOptimizeError, the lossy fan-out) is real.


def test_optimize_gif_returns_baseline_when_lossless_pass_already_fits(monkeypatch):
    async def fake_run_and_capture(args, timeout_seconds, error_prefix, **kwargs):
        assert kwargs["stdin_data"] == b"source-gif-bytes"
        assert "-O3" in args and "--lossy" not in " ".join(args)
        return b"o3-result"

    monkeypatch.setattr(gif_optimize, "run_and_capture", fake_run_and_capture)
    result = asyncio.run(optimize_gif(b"source-gif-bytes", max_bytes=100))
    assert result == b"o3-result"


def test_optimize_gif_falls_through_to_lossy_passes_when_baseline_too_big(monkeypatch):
    async def fake_run_and_capture(args, timeout_seconds, error_prefix, **kwargs):
        if any(arg.startswith("--lossy=10") for arg in args):
            return b"x" * 50
        if any(arg.startswith("--lossy=") for arg in args):
            return b"x" * 200
        return b"x" * 1000  # -O3 baseline, still too big

    monkeypatch.setattr(gif_optimize, "run_and_capture", fake_run_and_capture)
    result = asyncio.run(optimize_gif(b"source-gif-bytes", max_bytes=100))
    # N=10 is the first (lowest-N) candidate that fits max_bytes=100.
    assert result == b"x" * 50


def test_optimize_gif_wraps_any_failure_as_gif_optimize_error(monkeypatch):
    # Any exception at all from the gifsicle plumbing — not just a
    # subprocess RuntimeError — must come out as GifOptimizeError so
    # api.py's narrow `except GifOptimizeError` reliably catches it and
    # falls back to the downscale tiers instead of a raw 500.
    async def broken_run_and_capture(args, timeout_seconds, error_prefix, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(gif_optimize, "run_and_capture", broken_run_and_capture)
    with pytest.raises(GifOptimizeError):
        asyncio.run(optimize_gif(b"source-gif-bytes", max_bytes=100))


def test_optimize_gif_survives_some_lossy_passes_failing(monkeypatch):
    async def flaky_run_and_capture(args, timeout_seconds, error_prefix, **kwargs):
        if any(arg.startswith("--lossy=") for arg in args):
            if any(arg.startswith("--lossy=30") for arg in args):
                return b"x" * 40  # the one that survives
            raise RuntimeError("gifsicle crashed on this candidate")
        return b"x" * 1000  # -O3 baseline, still too big

    monkeypatch.setattr(gif_optimize, "run_and_capture", flaky_run_and_capture)
    result = asyncio.run(optimize_gif(b"source-gif-bytes", max_bytes=100))
    assert result == b"x" * 40
