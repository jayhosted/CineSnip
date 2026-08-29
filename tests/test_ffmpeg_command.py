import asyncio

import pytest

from app.worker.ffmpeg import ClipRenderer, _three_d_plan, build_seek_args, parse_timecode
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
    assert renderer._scale_and_subtitle_filter(None, None) == "scale=480:-2:flags=lanczos"


def test_scale_filter_puts_three_d_prefix_before_scale_and_subtitles():
    from pathlib import Path

    renderer = ClipRenderer(fps=15, width=480)
    filt = renderer._scale_and_subtitle_filter(Path("/tmp/subs.ass"), "crop=iw:ih/2:0:0,setsar=1")
    assert filt == "crop=iw:ih/2:0:0,setsar=1,scale=480:-2:flags=lanczos,subtitles='/tmp/subs.ass'"


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
