import asyncio

import pytest

from app.worker import gif_optimize
from app.worker.gif_optimize import (
    _GROUP_SIZE,
    _LOSSY_CANDIDATES,
    GifOptimizeError,
    _pick_best,
    optimize_gif,
)


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
    # The first lossy group now races alongside the -O3 baseline (see
    # _optimize_gif_unsafe) rather than waiting for it to fail first, so
    # the fake must handle both call shapes — but the baseline must still
    # win outright once it fits, discarding whatever the racing lossy
    # candidates produced.
    async def fake_run_and_capture(args, timeout_seconds, error_prefix, **kwargs):
        assert kwargs["stdin_data"] == b"source-gif-bytes"
        if "--lossy" not in " ".join(args):
            return b"o3-result"
        return b"x" * 999  # lossy candidates racing alongside baseline; must lose to it

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


# Bounded-parallelism regression coverage (issue #17): candidates now run
# in fixed-size ascending groups (_GROUP_SIZE at a time) instead of all 5
# concurrently, to cap gifsicle process/CPU contention under multiple
# simultaneous renders. The selection rule must stay exactly what it was
# before grouping — lowest-N-that-fits wins, decided by ascending index
# within each completed group, never by which candidate happens to finish
# first — and grouping must not launch candidates beyond what's needed to
# find a fit.


def _lossy_n_from_args(args: list[str]) -> int | None:
    for arg in args:
        if arg.startswith("--lossy="):
            return int(arg.split("=", 1)[1])
    return None


def test_bounded_group_picks_lower_loss_candidate_when_both_in_group_fit(monkeypatch):
    # _GROUP_SIZE=2 groups (2, 5) together first. Both fit; N=2 (lower
    # loss, earlier index) must win over N=5, even though N=5 is smaller.
    async def fake_run_and_capture(args, timeout_seconds, error_prefix, **kwargs):
        n = _lossy_n_from_args(args)
        if n == 2:
            return b"x" * 90
        if n == 5:
            return b"x" * 50
        return b"x" * 1000  # -O3 baseline, still too big; other lossy levels unused

    monkeypatch.setattr(gif_optimize, "run_and_capture", fake_run_and_capture)
    result = asyncio.run(optimize_gif(b"source-gif-bytes", max_bytes=100))
    assert result == b"x" * 90


def test_bounded_group_picks_second_candidate_when_only_it_fits(monkeypatch):
    # Within the first group (2, 5), only N=5 fits — it must win, since
    # N=2 doesn't satisfy the size constraint at all.
    async def fake_run_and_capture(args, timeout_seconds, error_prefix, **kwargs):
        n = _lossy_n_from_args(args)
        if n == 2:
            return b"x" * 500  # too big
        if n == 5:
            return b"x" * 90  # fits
        return b"x" * 1000  # -O3 baseline, still too big

    monkeypatch.setattr(gif_optimize, "run_and_capture", fake_run_and_capture)
    result = asyncio.run(optimize_gif(b"source-gif-bytes", max_bytes=100))
    assert result == b"x" * 90


def test_bounded_advances_to_next_group_when_first_group_has_no_fit(monkeypatch):
    # Group 1 (2, 5) both too big; group 2 (10, 20) — N=10 fits. Confirms
    # evaluation actually moves on to the next group rather than settling
    # for group 1's smallest-but-still-oversized result.
    call_log = []

    async def fake_run_and_capture(args, timeout_seconds, error_prefix, **kwargs):
        n = _lossy_n_from_args(args)
        call_log.append(n)
        if n in (2, 5, 20):
            return b"x" * 500  # too big
        if n == 10:
            return b"x" * 90  # fits — first index within group 2 that does
        return b"x" * 1000  # -O3 baseline / group 3 (N=30), if ever called

    monkeypatch.setattr(gif_optimize, "run_and_capture", fake_run_and_capture)
    result = asyncio.run(optimize_gif(b"source-gif-bytes", max_bytes=100))
    assert result == b"x" * 90
    # Group 2 (10, 20) is launched together, so N=20 does run — but N=10
    # (earlier index in that group) wins, so group 3 (N=30) is never needed.
    assert 30 not in call_log


def test_bounded_does_not_select_a_faster_finishing_higher_loss_candidate(monkeypatch):
    # N=5 (higher loss, later index) is made to finish first by sleeping
    # less than N=2. Both fit. The lower-loss N=2 must still win — the
    # group is fully awaited (via gather) before ascending-index selection
    # runs, so finish order can't influence the outcome.
    async def fake_run_and_capture(args, timeout_seconds, error_prefix, **kwargs):
        n = _lossy_n_from_args(args)
        if n == 2:
            await asyncio.sleep(0.02)
            return b"x" * 90
        if n == 5:
            await asyncio.sleep(0.0)
            return b"x" * 50
        return b"x" * 1000  # -O3 baseline, still too big

    monkeypatch.setattr(gif_optimize, "run_and_capture", fake_run_and_capture)
    result = asyncio.run(optimize_gif(b"source-gif-bytes", max_bytes=100))
    assert result == b"x" * 90


def test_bounded_stops_after_first_fitting_group_no_extra_candidates_launched(monkeypatch):
    # N=2 alone (first candidate in the first group) already fits — N=5
    # (rest of that group) still runs since the whole group is launched
    # together, but nothing from any later group should ever be invoked.
    call_log = []

    async def fake_run_and_capture(args, timeout_seconds, error_prefix, **kwargs):
        n = _lossy_n_from_args(args)
        call_log.append(n)
        if n == 2:
            return b"x" * 90  # fits immediately
        if n == 5:
            return b"x" * 80
        return b"x" * 1000  # -O3 baseline / group-2+ candidates, if ever called

    monkeypatch.setattr(gif_optimize, "run_and_capture", fake_run_and_capture)
    result = asyncio.run(optimize_gif(b"source-gif-bytes", max_bytes=100))
    assert result == b"x" * 90
    lossy_calls = [n for n in call_log if n is not None]
    assert set(lossy_calls) == {2, 5}  # only the first group ever ran
    assert len(lossy_calls) == _GROUP_SIZE


def test_baseline_races_with_first_group_not_after_it(monkeypatch):
    # The -O3 baseline and the first lossy group must be in flight at the
    # same time, not baseline-then-group sequentially — proven by making
    # the baseline call block until a lossy candidate has already started.
    lossy_started = asyncio.Event()
    baseline_called = asyncio.Event()

    async def fake_run_and_capture(args, timeout_seconds, error_prefix, **kwargs):
        n = _lossy_n_from_args(args)
        if n is None:
            baseline_called.set()
            # Would deadlock (timeout the test) if the first lossy group
            # were only launched after this baseline call returns.
            await asyncio.wait_for(lossy_started.wait(), timeout=2)
            return b"x" * 1000  # too big
        lossy_started.set()
        if n == 5:
            return b"x" * 90  # fits
        return b"x" * 1000

    monkeypatch.setattr(gif_optimize, "run_and_capture", fake_run_and_capture)
    result = asyncio.run(optimize_gif(b"source-gif-bytes", max_bytes=100))
    assert result == b"x" * 90
    assert baseline_called.is_set()


def test_baseline_wins_over_a_fitting_lossy_candidate_from_the_same_group(monkeypatch):
    # Both the baseline and N=5 fit — the lossless baseline must win even
    # though it's racing concurrently with the lossy group, not just when
    # it runs alone.
    async def fake_run_and_capture(args, timeout_seconds, error_prefix, **kwargs):
        n = _lossy_n_from_args(args)
        if n is None:
            return b"o3-result"  # fits
        return b"x" * 90  # also fits, but must lose to the lossless baseline

    monkeypatch.setattr(gif_optimize, "run_and_capture", fake_run_and_capture)
    result = asyncio.run(optimize_gif(b"source-gif-bytes", max_bytes=100))
    assert result == b"o3-result"


def test_full_parallel_launches_every_candidate_in_one_group(monkeypatch):
    # full_parallel=True is set by api.py only when this render is the
    # sole active one (app.state.active_renders) — the contention _GROUP_SIZE
    # protects against doesn't exist in that case, so all candidates should
    # launch together instead of in bounded groups.
    call_log = []

    async def fake_run_and_capture(args, timeout_seconds, error_prefix, **kwargs):
        n = _lossy_n_from_args(args)
        call_log.append(n)
        if n == 30:
            return b"x" * 90  # only the last, most-aggressive candidate fits
        return b"x" * 500  # covers every other candidate and the -O3 baseline

    monkeypatch.setattr(gif_optimize, "run_and_capture", fake_run_and_capture)
    result = asyncio.run(optimize_gif(b"source-gif-bytes", max_bytes=100, full_parallel=True))
    assert result == b"x" * 90
    lossy_calls = [n for n in call_log if n is not None]
    # All 5 candidates ran in a single group, not split across _GROUP_SIZE-sized ones.
    assert set(lossy_calls) == set(_LOSSY_CANDIDATES)


def test_default_full_parallel_is_false_and_preserves_grouped_behavior(monkeypatch):
    call_log = []

    async def fake_run_and_capture(args, timeout_seconds, error_prefix, **kwargs):
        n = _lossy_n_from_args(args)
        call_log.append(n)
        if n == 2:
            return b"x" * 90
        return b"x" * 500

    monkeypatch.setattr(gif_optimize, "run_and_capture", fake_run_and_capture)
    result = asyncio.run(optimize_gif(b"source-gif-bytes", max_bytes=100))
    assert result == b"x" * 90
    lossy_calls = [n for n in call_log if n is not None]
    assert set(lossy_calls) == {2, 5}  # unchanged default: only first group ran
