import pytest

from app.bot.cogs.gif import _format_unit_timecode, _post_metadata_line, _slugify


@pytest.mark.parametrize(
    "title,expected",
    [
        ("The Matrix", "the-matrix"),
        ("Amélie", "am-lie"),
        ("Spider-Man: Into the Spider-Verse", "spider-man-into-the-spider-verse"),
        ("  Leading & Trailing  ", "leading-trailing"),
    ],
)
def test_slugify(title, expected):
    assert _slugify(title) == expected


@pytest.mark.parametrize(
    "seconds,expected",
    [
        (19, "19s"),
        (9 * 60 + 19, "9m19s"),
        (3600 + 2 * 60 + 10, "1h02m10s"),
        (0, "0s"),
    ],
)
def test_format_unit_timecode(seconds, expected):
    assert _format_unit_timecode(seconds) == expected


def test_post_metadata_line_movie():
    line = _post_metadata_line(
        "The Matrix", start=3730, end=3735, posted_by="Jay"
    )
    assert line == "-# the-matrix@1h02m10s-1h02m15s posted by Jay"


def test_post_metadata_line_tv_episode():
    line = _post_metadata_line(
        "Partridge — S02E03 — Bouncy", start=559, end=568, posted_by="Jay"
    )
    assert line == "-# partridge-s02e03@9m19s-9m28s posted by Jay"
