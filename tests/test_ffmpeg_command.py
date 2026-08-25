import pytest

from app.worker.ffmpeg import build_seek_args, parse_timecode


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


def test_parse_timecode_rejects_garbage():
    with pytest.raises(ValueError):
        parse_timecode("not-a-timecode")


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
