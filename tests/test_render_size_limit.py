"""Covers the /render size-downscale retry loop (_render_within_size_limit,
_DOWNSCALE_TIERS) — CLAUDE.md documents auto-downscaling on an oversized
render as existing behavior, but it was never actually built until now.
Also covers the gifsicle compression tier (app/worker/gif_optimize.py)
that now runs before the downscale tiers for GIF renders.
"""

import asyncio

from app.worker.api import _render_within_size_limit
from app.worker.gif_optimize import GifOptimizeError


class _FakeRenderer:
    """Returns bytes whose length is determined by the fps/width passed to
    render_clip — smaller fps/width means a smaller "render", mimicking
    real downscale behavior without touching ffmpeg. default_fps/width
    mirror how a real ClipRenderer is constructed with the configured
    values, so the first (unparameterized) call in the retry loop uses
    the same numbers the test passes as configured_fps/configured_width."""

    def __init__(self, size_for, default_fps=15, default_width=480):
        self._size_for = size_for
        self._default_fps = default_fps
        self._default_width = default_width
        self.calls: list[tuple[int, int]] = []

    async def render_clip(self, *args, fps=None, width=None, **kwargs) -> bytes:
        fps = fps if fps is not None else self._default_fps
        width = width if width is not None else self._default_width
        self.calls.append((fps, width))
        return b"x" * self._size_for(fps, width)


def _noop_optimize_gif(calls_log=None):
    """Default fake for the optimize_gif seam: returns the input
    unchanged, so pre-existing downscale-tier tests keep testing exactly
    what they tested before — no accidental dependency on gifsicle being
    installed on the machine running the tests."""

    async def optimize_gif(gif_bytes, max_bytes, scratch_dir, timeout_seconds=60.0):
        if calls_log is not None:
            calls_log.append((len(gif_bytes), max_bytes))
        return gif_bytes

    return optimize_gif


def _run(
    renderer,
    max_bytes,
    configured_fps=15,
    configured_width=480,
    clip_format="gif",
    optimize_gif=None,
):
    return asyncio.run(
        _render_within_size_limit(
            renderer,
            "input.mkv",
            0.0,
            5.0,
            None,
            clip_format,
            None,
            None,
            "none",
            None,
            configured_fps,
            configured_width,
            max_bytes,
            optimize_gif=optimize_gif or _noop_optimize_gif(),
        )
    )


def test_returns_first_attempt_when_already_under_the_limit():
    renderer = _FakeRenderer(size_for=lambda fps, width: 100)
    result = _run(renderer, max_bytes=1000)
    assert len(result) == 100
    assert renderer.calls == [(15, 480)]


def test_retries_with_tiers_until_one_fits():
    # Only the third tier (8, 320) produces something small enough.
    def size_for(fps, width):
        if (fps, width) == (8, 320):
            return 50
        return 1000

    renderer = _FakeRenderer(size_for=size_for)
    result = _run(renderer, max_bytes=500)
    assert len(result) == 50
    # Configured attempt, then tier 1 (12,480), tier 2 (10,400), tier 3 (8,320) — stops there.
    assert renderer.calls == [(15, 480), (12, 480), (10, 400), (8, 320)]


def test_tier_values_are_capped_by_configured_settings_never_upscaled():
    # Configured fps/width are already below every tier's values.
    renderer = _FakeRenderer(size_for=lambda fps, width: 1000, default_fps=6, default_width=200)
    _run(renderer, max_bytes=1, configured_fps=6, configured_width=200)
    assert all(fps <= 6 and width <= 200 for fps, width in renderer.calls)


def test_returns_smallest_attempt_when_every_tier_is_still_too_large():
    sizes = {
        (15, 480): 900,
        (12, 480): 800,
        (10, 400): 700,
        (8, 320): 650,
        (8, 240): 600,
    }
    renderer = _FakeRenderer(size_for=lambda fps, width: sizes[(fps, width)])
    result = _run(renderer, max_bytes=100)
    assert len(result) == 600


def test_gif_format_tries_gifsicle_before_downscale_tiers():
    # The oversized 900-byte render should go through optimize_gif first;
    # if optimize_gif's (faked) result already fits, the downscale tiers
    # must never be reached at all.
    renderer = _FakeRenderer(size_for=lambda fps, width: 900)

    async def shrinking_optimize_gif(gif_bytes, max_bytes, scratch_dir, timeout_seconds=60.0):
        return b"x" * 80

    result = _run(renderer, max_bytes=100, optimize_gif=shrinking_optimize_gif)
    assert len(result) == 80
    # Only the initial configured-settings render happened — no downscale
    # tier attempts, because gifsicle alone was enough.
    assert renderer.calls == [(15, 480)]


def test_gif_format_falls_through_to_downscale_tiers_when_gifsicle_is_not_enough():
    def size_for(fps, width):
        if (fps, width) == (10, 400):
            return 50
        return 1000

    renderer = _FakeRenderer(size_for=size_for)

    async def insufficient_optimize_gif(gif_bytes, max_bytes, scratch_dir, timeout_seconds=60.0):
        # gifsicle helps, but not enough to clear budget on its own.
        return b"x" * 600

    result = _run(renderer, max_bytes=500, optimize_gif=insufficient_optimize_gif)
    assert len(result) == 50
    assert renderer.calls == [(15, 480), (12, 480), (10, 400)]


def test_gif_optimize_error_falls_through_to_downscale_tiers():
    # gifsicle itself failing (missing binary, timeout, unusual GIF) must
    # degrade to the downscale tiers, not propagate out of
    # _render_within_size_limit and become an HTTP 500 (CLAUDE.md: "a
    # still-oversized clip is more useful than none").
    def size_for(fps, width):
        if (fps, width) == (10, 400):
            return 50
        return 1000

    renderer = _FakeRenderer(size_for=size_for)

    async def broken_optimize_gif(gif_bytes, max_bytes, scratch_dir, timeout_seconds=60.0):
        raise GifOptimizeError("gifsicle not found")

    result = _run(renderer, max_bytes=500, optimize_gif=broken_optimize_gif)
    assert len(result) == 50
    assert renderer.calls == [(15, 480), (12, 480), (10, 400)]


def test_non_gif_formats_skip_gifsicle_entirely():
    calls_log: list[tuple[int, int]] = []
    renderer = _FakeRenderer(size_for=lambda fps, width: 1000)
    result = _run(
        renderer,
        max_bytes=500,
        clip_format="mp4",
        optimize_gif=_noop_optimize_gif(calls_log),
    )
    # optimize_gif must never be called for a non-gif format — it falls
    # straight through to the downscale tiers, whatever they produce.
    assert calls_log == []
