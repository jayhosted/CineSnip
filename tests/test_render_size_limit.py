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
        self.audio_languages: list[str] = []

    async def render_clip(self, *args, fps=None, width=None, audio_language="eng", **kwargs) -> bytes:
        fps = fps if fps is not None else self._default_fps
        width = width if width is not None else self._default_width
        self.calls.append((fps, width))
        self.audio_languages.append(audio_language)
        return b"x" * self._size_for(fps, width)


def _noop_optimize_gif(calls_log=None):
    """Default fake for the optimize_gif seam: returns the input
    unchanged, so pre-existing downscale-tier tests keep testing exactly
    what they tested before — no accidental dependency on gifsicle being
    installed on the machine running the tests."""

    async def optimize_gif(gif_bytes, max_bytes, timeout_seconds=60.0, full_parallel=False):
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
    audio_language="eng",
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
            audio_language=audio_language,
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

    async def shrinking_optimize_gif(gif_bytes, max_bytes, timeout_seconds=60.0, full_parallel=False):
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

    async def insufficient_optimize_gif(gif_bytes, max_bytes, timeout_seconds=60.0, full_parallel=False):
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

    async def broken_optimize_gif(gif_bytes, max_bytes, timeout_seconds=60.0, full_parallel=False):
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


def test_audio_formats_skip_the_downscale_tiers_entirely():
    # _DOWNSCALE_TIERS is fps/width — meaningless for mp3/ogg (issue #6). An
    # oversized audio render should be returned as-is rather than retried.
    renderer = _FakeRenderer(size_for=lambda fps, width: 1000)
    result = _run(renderer, max_bytes=500, clip_format="mp3")
    assert len(result) == 1000
    assert renderer.calls == [(15, 480)]


def test_gif_tier_tries_gifsicle_before_moving_to_the_next_smaller_tier():
    # Tier 1 (12, 480) renders oversized (1000 bytes) but a gifsicle pass
    # on *that* tier's own output would clear budget — the loop must
    # return that gifsicle result rather than stepping down to tier 2
    # (10, 400), preserving more resolution than necessary.
    renderer = _FakeRenderer(size_for=lambda fps, width: 1000)
    optimize_calls: list[int] = []

    async def optimize_gif(gif_bytes, max_bytes, timeout_seconds=60.0, full_parallel=False):
        optimize_calls.append(len(gif_bytes))
        if len(optimize_calls) == 1:
            return b"x" * 900  # pre-downscale attempt: gifsicle helps, but not enough
        return b"x" * 80  # tier 1's own gifsicle pass: fits

    result = _run(renderer, max_bytes=100, optimize_gif=optimize_gif)
    assert len(result) == 80
    # Pre-downscale gifsicle attempt (on the configured-settings render),
    # then one more on tier 1's (12, 480) output — never reaches tier 2.
    assert optimize_calls == [1000, 1000]
    assert renderer.calls == [(15, 480), (12, 480)]


def test_gif_tier_gifsicle_failure_still_falls_through_to_next_tier():
    # gifsicle failing on a downscaled tier's output must degrade to the
    # next tier, not blow up the whole render — same contract as the
    # pre-downscale gifsicle attempt already has.
    def size_for(fps, width):
        if (fps, width) == (10, 400):
            return 50
        return 1000

    renderer = _FakeRenderer(size_for=size_for)

    async def broken_optimize_gif(gif_bytes, max_bytes, timeout_seconds=60.0, full_parallel=False):
        raise GifOptimizeError("gifsicle not found")

    result = _run(renderer, max_bytes=500, optimize_gif=broken_optimize_gif)
    assert len(result) == 50
    assert renderer.calls == [(15, 480), (12, 480), (10, 400)]


def test_audio_language_is_forwarded_to_the_renderer():
    # Bug fix: the configured preferred audio track language must reach
    # ClipRenderer.render_clip, not get dropped between config and ffmpeg.
    renderer = _FakeRenderer(size_for=lambda fps, width: 100)
    _run(renderer, max_bytes=1000, clip_format="mp3", audio_language="fre")
    assert renderer.audio_languages == ["fre"]
