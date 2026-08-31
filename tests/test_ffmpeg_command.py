import asyncio
import json

import pytest

from app.worker import ffmpeg as ffmpeg_module
from app.worker.ffmpeg import (
    _AUDIO_CODEC_ARGS,
    AudioStreamInfo,
    ClipRenderer,
    _crop_adjusted_dims,
    _crop_box_or_none,
    _crop_probe_window,
    _three_d_plan,
    build_seek_args,
    choose_audio_stream,
    is_hdr_transfer,
    parse_cropdetect_output,
    parse_timecode,
    probe_audio_streams,
)
from app.worker.subtitle_render import STYLE_PRESETS
from app.worker.subtitles import SubtitleEntry


def test_build_seek_args_formats_start_and_duration_as_timecodes():
    assert build_seek_args(83.5, 4) == [
        "-ss",
        "00:01:23.500",
        "-t",
        "00:00:04.000",
    ]


@pytest.mark.parametrize(
    "text,expected_seconds",
    [
        ("45", 45.0),
        ("1:23", 83.0),
        ("1:23:45", 5025.0),
        ("0:05", 5.0),
    ],
)
def test_parse_timecode(text, expected_seconds):
    assert parse_timecode(text) == expected_seconds


@pytest.mark.parametrize(
    "text,expected_seconds",
    [
        ("22m12s", 1332.0),
        ("22min12sec", 1332.0),
        ("1hr22min2sec", 4922.0),
        ("1h22m12s", 4932.0),
        ("22 min 12 sec", 1332.0),
        ("90s", 90.0),
        ("1h", 3600.0),
        ("2minutes", 120.0),
        ("1hour30minutes", 5400.0),
        ("45sec", 45.0),
        # Abbreviated and fully-spelled-out forms must agree exactly —
        # users won't reliably pick one convention over the other.
        ("42m19s", 2539.0),
        ("42min19sec", 2539.0),
        ("42minutes19seconds", 2539.0),
        ("42 minutes 19 seconds", 2539.0),
        ("1hr2min3sec", 3723.0),
        ("1h2m3s", 3723.0),
        ("1hour2minutes3seconds", 3723.0),
    ],
)
def test_parse_timecode_accepts_unit_suffixed_forms(text, expected_seconds):
    assert parse_timecode(text) == expected_seconds


def test_parse_timecode_rejects_garbage():
    with pytest.raises(ValueError):
        parse_timecode("not-a-timecode")


def test_parse_timecode_rejects_out_of_order_units():
    with pytest.raises(ValueError):
        parse_timecode("12s22m")


def test_seek_and_duration_are_input_options_before_the_video_input():
    """Regression test for a real bug: -t must precede the video's -i (as
    an INPUT option), not follow it. Filters like palettegen/paletteuse
    only emit a single output frame at the very end of the graph, so -t
    placed as an OUTPUT option (after -i) has no rolling output PTS to cut
    off against — ffmpeg keeps decoding and feeding frames into the filter
    for the rest of the file. As an input option, -t instead bounds how
    much is actually read from the file, which is what stops it.
    """
    video_path = "/media/movies-d/film.mkv"
    palette_path = "/tmp/palette.png"

    args = [
        *build_seek_args(10.0, 4.0),
        "-i",
        video_path,
        "-i",
        palette_path,
    ]

    first_i_index = args.index("-i")
    seek_index = args.index("-ss")
    duration_index = args.index("-t")

    assert seek_index < first_i_index
    assert duration_index < first_i_index


def test_scale_filter_has_no_prefix_for_flat_video():
    renderer = ClipRenderer(fps=15, width=480)
    assert renderer._scale_and_subtitle_filter(480, None, None) == "scale=480:-2:flags=lanczos"


def test_scale_filter_puts_three_d_prefix_before_scale_and_subtitles():
    from pathlib import Path

    renderer = ClipRenderer(fps=15, width=480)
    filt = renderer._scale_and_subtitle_filter(
        480, Path("/tmp/subs.ass"), "crop=iw:ih/2:0:0,setsar=1"
    )
    assert filt == "crop=iw:ih/2:0:0,setsar=1,scale=480:-2:flags=lanczos,subtitles='/tmp/subs.ass'"


def test_scale_filter_uses_the_given_width_not_the_renderer_default():
    renderer = ClipRenderer(fps=15, width=480)
    assert renderer._scale_and_subtitle_filter(240, None, None) == "scale=240:-2:flags=lanczos"


# HDR tonemap: ffmpeg's plain `scale` filter doesn't tonemap, so an
# HDR-tagged source (PQ/smpte2084, HLG/arib-std-b67) must get a tonemap
# filter chain before scale/subtitles, or the output is washed-out/wrong
# (issue #12 — confirmed against real HDR10 remuxes in this library).


@pytest.mark.parametrize(
    "color_transfer,expected",
    [
        ("smpte2084", True),
        ("arib-std-b67", True),
        ("bt709", False),
        (None, False),
        ("", False),
    ],
)
def test_is_hdr_transfer(color_transfer, expected):
    assert is_hdr_transfer(color_transfer) is expected


def test_scale_filter_has_no_tonemap_for_sdr_video():
    renderer = ClipRenderer(fps=15, width=480)
    filt = renderer._scale_and_subtitle_filter(480, None, None, is_hdr=False)
    assert "tonemap" not in filt
    assert filt == "scale=480:-2:flags=lanczos"


def test_scale_filter_inserts_tonemap_before_scale_for_hdr_video():
    renderer = ClipRenderer(fps=15, width=480)
    filt = renderer._scale_and_subtitle_filter(480, None, None, is_hdr=True)
    assert filt.index("tonemap") < filt.index("scale=480")


def test_scale_filter_puts_tonemap_after_three_d_prefix_and_before_subtitles():
    from pathlib import Path

    renderer = ClipRenderer(fps=15, width=480)
    filt = renderer._scale_and_subtitle_filter(
        480, Path("/tmp/subs.ass"), "crop=iw:ih/2:0:0,setsar=1", is_hdr=True
    )
    crop_idx = filt.index("crop=")
    tonemap_idx = filt.index("tonemap")
    scale_idx = filt.index("scale=480")
    subs_idx = filt.index("subtitles=")
    assert crop_idx < tonemap_idx < scale_idx < subs_idx


# _three_d_plan: distinguishes "full" 3D packs (each eye at native
# resolution, crop alone suffices) from "half"/squeezed packs (each eye
# compressed to fit the original frame, needing an unsqueeze stretch back
# to native size after cropping) purely from the packed frame's own raw
# pixel aspect ratio. Regression coverage for a real bug: the initial 3D
# fix only cropped, which fixed a real Full-SBS file (Ready Player One,
# 3840x1080) but left a real squeezed over/under file (Dune, 1920x1080)
# still squished after cropping.


def test_three_d_plan_is_a_noop_for_flat_video():
    assert _three_d_plan("none", 1920, 1080) == (None, 1920, 1080)


def test_three_d_plan_full_side_by_side_just_crops():
    # Confirmed on Ready Player One's real Full-SBS file: 3840x1080, ratio
    # 3.56, well past the full-pack threshold.
    prefix, eye_w, eye_h = _three_d_plan("side_by_side", 3840, 1080)
    assert prefix == "crop=iw/2:ih:0:0,setsar=1"
    assert (eye_w, eye_h) == (1920, 1080)


def test_three_d_plan_squeezed_side_by_side_also_unsqueezes():
    prefix, eye_w, eye_h = _three_d_plan("side_by_side", 1920, 1080)
    assert prefix == "crop=iw/2:ih:0:0,scale=iw*2:ih,setsar=1"
    assert (eye_w, eye_h) == (1920, 1080)


def test_three_d_plan_full_over_under_just_crops():
    prefix, eye_w, eye_h = _three_d_plan("over_under", 1920, 2160)
    assert prefix == "crop=iw:ih/2:0:0,setsar=1"
    assert (eye_w, eye_h) == (1920, 1080)


def test_three_d_plan_squeezed_over_under_also_unsqueezes():
    # Confirmed on Dune's real squeezed over/under file: 1920x1080 packed
    # frame, cropdetect showed a 1920x402 content box within the top half
    # (ratio 4.78, implausible for any real film) that becomes 2.39:1
    # (Cinemascope) once doubled back to its true height.
    prefix, eye_w, eye_h = _three_d_plan("over_under", 1920, 1080)
    assert prefix == "crop=iw:ih/2:0:0,scale=iw:ih*2,setsar=1"
    assert (eye_w, eye_h) == (1920, 1080)


# Auto-crop (issue #14): baked-in letterbox/pillarbox bars in a source's
# own pixel data (e.g. Dune (2021)'s 4K remux, mastered 16:9 with real
# black bars around a 2.39:1 image) are detected via a cropdetect probe and
# cropped out before scale/subtitles, the same filter-chain-insertion
# pattern as the existing 3D eye-crop and HDR tonemap.


@pytest.mark.parametrize(
    "stderr_text,expected",
    [
        ("random ffmpeg banter\ncrop=3840:1604:0:278\nmore lines", (3840, 1604, 0, 278)),
        # cropdetect converges over several frames and logs one line per
        # frame — only the LAST reported value is the converged result.
        ("crop=3840:2160:0:0\ncrop=3840:1700:0:230\ncrop=3840:1604:0:278", (3840, 1604, 0, 278)),
        ("no crop lines here at all", None),
        ("", None),
    ],
)
def test_parse_cropdetect_output(stderr_text, expected):
    assert parse_cropdetect_output(stderr_text) == expected


@pytest.mark.parametrize(
    "duration,expected",
    [
        # A typical feature: skip the first couple of minutes (studio
        # logos/black intro cards) and sample a short window well inside
        # the runtime — baked-in bars are a mastering-wide constant, not
        # scene-dependent, so any non-edge sample is representative.
        (7200.0, (1800.0, 5.0)),
        # Duration unknown/short enough that the default window would run
        # past the end of the file — never probe past the file's own end.
        (100.0, (95.0, 5.0)),
        # Shorter than the default window entirely — probe the whole thing.
        (3.0, (0.0, 3.0)),
        # No/zero duration (ffprobe failed) — fall back to probing from the
        # start with the default window rather than crashing.
        (0.0, (0.0, 5.0)),
    ],
)
def test_crop_probe_window(duration, expected):
    assert _crop_probe_window(duration) == expected


@pytest.mark.parametrize(
    "box,width,height,expected",
    [
        # A real detected crop, well inside the margin of error.
        ((3840, 1604, 0, 278), 3840, 2160, (3840, 1604, 0, 278)),
        # cropdetect reporting essentially the full frame (no bars) must be
        # treated as "no crop needed", not an inert crop=iw:ih filter.
        ((3840, 2160, 0, 0), 3840, 2160, None),
        # Within rounding/margin of the full frame on all sides.
        ((3838, 2158, 1, 1), 3840, 2160, None),
    ],
)
def test_crop_box_or_none(box, width, height, expected):
    assert _crop_box_or_none(box, width, height) == expected


def test_scale_filter_has_no_crop_by_default():
    renderer = ClipRenderer(fps=15, width=480)
    filt = renderer._scale_and_subtitle_filter(480, None, None, crop_box=None)
    assert "crop=" not in filt


def test_scale_filter_inserts_content_crop_before_scale():
    renderer = ClipRenderer(fps=15, width=480)
    filt = renderer._scale_and_subtitle_filter(480, None, None, crop_box=(3840, 1604, 0, 278))
    assert filt == "crop=3840:1604:0:278,scale=480:-2:flags=lanczos"


def test_scale_filter_puts_content_crop_after_three_d_prefix_and_before_tonemap():
    renderer = ClipRenderer(fps=15, width=480)
    filt = renderer._scale_and_subtitle_filter(
        480, None, "crop=iw/2:ih:0:0,setsar=1", is_hdr=True, crop_box=(1920, 800, 0, 140)
    )
    three_d_idx = filt.index("crop=iw/2")
    content_crop_idx = filt.index("crop=1920:800:0:140")
    tonemap_idx = filt.index("tonemap")
    scale_idx = filt.index("scale=480")
    assert three_d_idx < content_crop_idx < tonemap_idx < scale_idx


# libass sizes burned-in text relative to PlayResX/PlayResY, which must
# match the actual frame scale/subtitles will draw against — a content
# crop shrinks that frame just like the 3D eye-crop already does, so it
# must feed into the same dimensions _write_ass_file uses to compute
# PlayResY (see the 3D squeeze bug this project already hit for why this
# class of mismatch matters).


def test_crop_adjusted_dims_is_a_noop_without_a_crop_box():
    assert _crop_adjusted_dims(None, 1920, 1080) == (1920, 1080)


def test_crop_adjusted_dims_uses_the_cropped_size():
    assert _crop_adjusted_dims((1920, 800, 0, 140), 1920, 1080) == (1920, 800)


def test_write_ass_file_applies_subtitle_overrides(tmp_path):
    renderer = ClipRenderer(fps=15, width=480)
    entries = [
        SubtitleEntry(index=1, start=1.0, end=3.0, text="original one"),
        SubtitleEntry(index=2, start=3.0, end=5.0, text="original two"),
    ]

    ass_path = asyncio.run(
        renderer._write_ass_file(
            "unused-input-path.mkv",
            start=0.0,
            duration=6.0,
            entries=entries,
            style=STYLE_PRESETS["classic"],
            scratch_dir=tmp_path,
            width=480,
            eye_width=480,
            eye_height=270,
            subtitle_overrides={1: "edited one", 2: None},
        )
    )

    doc = ass_path.read_text(encoding="utf-8")
    assert "edited one" in doc
    assert "original one" not in doc
    assert "original two" not in doc
    assert "two" not in doc


# Audio-only clips (issue #6): no frame exists to crop/tonemap/scale or burn
# subtitles into, so render_clip must branch to the audio path before any of
# that video-specific probing runs, not just skip using its results.


def test_audio_codec_args_cover_both_supported_formats():
    assert _AUDIO_CODEC_ARGS["mp3"] == ["-c:a", "libmp3lame", "-q:a", "2"]
    assert _AUDIO_CODEC_ARGS["ogg"] == ["-c:a", "libvorbis", "-q:a", "4"]


@pytest.mark.parametrize("fmt", ["mp3", "ogg"])
def test_render_clip_skips_video_probing_for_audio_formats(monkeypatch, tmp_path, fmt):
    def _boom(*args, **kwargs):
        raise AssertionError("video-only probe must not run for an audio render")

    monkeypatch.setattr(ffmpeg_module, "probe_stereo_format", _boom)
    monkeypatch.setattr(ffmpeg_module, "probe_video_dimensions", _boom)
    monkeypatch.setattr(ffmpeg_module, "probe_color_transfer", _boom)
    monkeypatch.setattr(ffmpeg_module, "probe_crop", _boom)

    async def fake_render_audio(self, input_path, start, duration, scratch_dir, fmt_arg, audio_language="eng"):
        return b"audio-bytes"

    monkeypatch.setattr(ClipRenderer, "_render_audio", fake_render_audio)

    renderer = ClipRenderer(fps=15, width=480, crop_cache_db_path=tmp_path / "crop.db")
    result = asyncio.run(
        renderer.render_clip(
            "input.mkv", 0.0, 4.0, tmp_path, fmt=fmt, three_d_format="side_by_side",
        )
    )
    assert result == b"audio-bytes"


@pytest.mark.parametrize("fmt", ["mp3", "ogg"])
def test_render_audio_builds_expected_ffmpeg_args(monkeypatch, tmp_path, fmt):
    captured = {}

    async def fake_run(self, args, error_prefix, capture_stdout=False):
        captured["args"] = args
        # Real ffmpeg writes to the output path given as the last arg;
        # _render_audio then reads it back from disk.
        from pathlib import Path

        Path(args[-1]).write_bytes(b"encoded-audio")
        return None

    async def fake_probe_audio_streams(input_path, timeout_seconds=30.0):
        return []  # single/untagged track — falls back to stream 0.

    monkeypatch.setattr(ClipRenderer, "_run", fake_run)
    monkeypatch.setattr(ffmpeg_module, "probe_audio_streams", fake_probe_audio_streams)

    renderer = ClipRenderer(fps=15, width=480)
    result = asyncio.run(renderer._render_audio("input.mkv", 10.0, 4.0, tmp_path, fmt))

    assert result == b"encoded-audio"
    args = captured["args"]
    assert "-map" in args and args[args.index("-map") + 1] == "0:a:0"
    assert "-map_chapters" in args and args[args.index("-map_chapters") + 1] == "-1"
    assert "-vn" in args
    for arg in _AUDIO_CODEC_ARGS[fmt]:
        assert arg in args
    assert "-vf" not in args


# Bug: a multi-track source doesn't reliably order its audio streams
# original-language-first — a real file in this library (Pulp Fiction's
# Blu-ray rip) has German as stream 0, English as stream 1. `-map 0:a:0`
# alone (the old hardcoded behavior) silently picked the wrong track.


def test_render_audio_picks_the_stream_matching_configured_language(monkeypatch, tmp_path):
    captured = {}

    async def fake_run(self, args, error_prefix, capture_stdout=False):
        captured["args"] = args
        from pathlib import Path

        Path(args[-1]).write_bytes(b"encoded-audio")
        return None

    async def fake_probe_audio_streams(input_path, timeout_seconds=30.0):
        return [
            AudioStreamInfo(relative_index=0, codec_name="dts", language="ger", title="German"),
            AudioStreamInfo(relative_index=1, codec_name="dts", language="eng", title="English"),
        ]

    monkeypatch.setattr(ClipRenderer, "_run", fake_run)
    monkeypatch.setattr(ffmpeg_module, "probe_audio_streams", fake_probe_audio_streams)

    renderer = ClipRenderer(fps=15, width=480)
    asyncio.run(
        renderer._render_audio("input.mkv", 10.0, 4.0, tmp_path, "mp3", audio_language="eng")
    )

    args = captured["args"]
    assert args[args.index("-map") + 1] == "0:a:1"


def test_render_audio_falls_back_to_stream_zero_when_language_not_present(monkeypatch, tmp_path):
    captured = {}

    async def fake_run(self, args, error_prefix, capture_stdout=False):
        captured["args"] = args
        from pathlib import Path

        Path(args[-1]).write_bytes(b"encoded-audio")
        return None

    async def fake_probe_audio_streams(input_path, timeout_seconds=30.0):
        return [
            AudioStreamInfo(relative_index=0, codec_name="dts", language="ger", title="German"),
            AudioStreamInfo(relative_index=1, codec_name="dts", language="fre", title="French"),
        ]

    monkeypatch.setattr(ClipRenderer, "_run", fake_run)
    monkeypatch.setattr(ffmpeg_module, "probe_audio_streams", fake_probe_audio_streams)

    renderer = ClipRenderer(fps=15, width=480)
    asyncio.run(
        renderer._render_audio("input.mkv", 10.0, 4.0, tmp_path, "mp3", audio_language="eng")
    )

    args = captured["args"]
    assert args[args.index("-map") + 1] == "0:a:0"


# choose_audio_stream (the pure decision function above) in isolation.


def _audio_stream(**overrides):
    defaults = dict(relative_index=0, codec_name="dts", language="eng", title=None)
    defaults.update(overrides)
    return AudioStreamInfo(**defaults)


def test_choose_audio_stream_prefers_the_configured_language():
    streams = [
        _audio_stream(relative_index=0, language="ger"),
        _audio_stream(relative_index=1, language="eng"),
    ]
    assert choose_audio_stream(streams, "eng") == 1


def test_choose_audio_stream_is_case_insensitive():
    streams = [_audio_stream(relative_index=0, language="ENG")]
    assert choose_audio_stream(streams, "eng") == 0


def test_choose_audio_stream_falls_back_to_first_when_language_absent():
    streams = [_audio_stream(relative_index=0, language="ger"), _audio_stream(relative_index=1, language="fre")]
    assert choose_audio_stream(streams, "eng") == 0


def test_choose_audio_stream_falls_back_to_first_when_untagged():
    streams = [_audio_stream(relative_index=0, language=None)]
    assert choose_audio_stream(streams, "eng") == 0


def test_choose_audio_stream_falls_back_to_zero_with_no_streams_at_all():
    assert choose_audio_stream([], "eng") == 0


def test_probe_audio_streams_parses_language_and_title_tags(monkeypatch):
    payload = json.dumps(
        {
            "streams": [
                {"codec_name": "dts", "tags": {"language": "ger", "title": "German 5.1 DTS"}},
                {"codec_name": "dts", "tags": {"language": "eng", "title": "English 5.1 DTS"}},
            ]
        }
    ).encode()

    async def fake_run_and_capture(args, timeout_seconds, error_prefix, capture_stdout=False):
        return payload

    monkeypatch.setattr(ffmpeg_module, "run_and_capture", fake_run_and_capture)

    streams = asyncio.run(probe_audio_streams("input.mkv"))

    assert [s.language for s in streams] == ["ger", "eng"]
    assert [s.relative_index for s in streams] == [0, 1]
    assert streams[1].title == "English 5.1 DTS"
