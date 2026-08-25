import pytest

from app.worker.ffmpeg import build_duration_args, build_seek_args, parse_timecode


def test_build_seek_args_formats_as_timecode():
    assert build_seek_args(83.5) == ["-ss", "00:01:23.500"]


def test_build_duration_args_formats_as_timecode():
    assert build_duration_args(4) == ["-t", "00:00:04.000"]


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


def test_two_input_command_places_duration_after_all_inputs():
    """Regression test: -t must come after BOTH -i flags in the two-input
    (video + palette) encode pass. ffmpeg binds a trailing option to
    whichever -i follows it, so -t placed between the two inputs would
    silently limit the palette image instead of the output duration.
    """
    video_path = "/media/movies-d/film.mkv"
    palette_path = "/tmp/palette.png"

    args = [
        *build_seek_args(10.0),
        "-i",
        video_path,
        "-i",
        palette_path,
        *build_duration_args(4.0),
    ]

    first_i_index = args.index("-i")
    second_i_index = args.index("-i", first_i_index + 1)
    duration_index = args.index("-t")

    assert duration_index > second_i_index > first_i_index
