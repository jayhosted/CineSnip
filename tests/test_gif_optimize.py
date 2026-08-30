from app.worker.gif_optimize import _LOSSY_CANDIDATES, _pick_best


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
