import pytest

from app.worker.ffmpeg import ClipRenderer, build_seek_args, parse_timecode


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


def test_scale_filter_has_no_crop_for_flat_video():
    renderer = ClipRenderer(fps=15, width=480)
    assert renderer._scale_and_subtitle_filter(None, "none") == "scale=480:-2:flags=lanczos"


def test_scale_filter_crops_left_eye_for_side_by_side():
    renderer = ClipRenderer(fps=15, width=480)
    filt = renderer._scale_and_subtitle_filter(None, "side_by_side")
    assert filt == "crop=iw/2:ih:0:0,scale=480:-2:flags=lanczos"


def test_scale_filter_crops_top_eye_for_over_under():
    renderer = ClipRenderer(fps=15, width=480)
    filt = renderer._scale_and_subtitle_filter(None, "over_under")
    assert filt == "crop=iw:ih/2:0:0,scale=480:-2:flags=lanczos"


def test_scale_filter_puts_crop_before_scale_and_subtitles():
    from pathlib import Path

    renderer = ClipRenderer(fps=15, width=480)
    filt = renderer._scale_and_subtitle_filter(Path("/tmp/subs.ass"), "over_under")
    assert filt.startswith("crop=iw:ih/2:0:0,scale=480:-2:flags=lanczos,subtitles=")
