from types import SimpleNamespace

import pytest

from app.bot.cogs.gif import (
    _audio_filename,
    _error_detail_from_discord,
    _format_unit_timecode,
    _post_metadata_line,
    _slugify,
)


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


def test_audio_filename_uses_the_subtitle_text():
    assert _audio_filename("mp3", "I know you can be overwhelmed") == (
        "i-know-you-can-be-overwhelmed.mp3"
    )


def test_audio_filename_truncates_long_subtitle_text():
    long_text = "This is a very long line of dialogue that goes on for quite a while indeed"
    filename = _audio_filename("ogg", long_text)
    assert filename.endswith(".ogg")
    slug = filename[: -len(".ogg")]
    assert len(slug) <= 60
    # No truncation mid-word leaving a trailing separator.
    assert not slug.endswith("-")


def test_audio_filename_falls_back_to_generic_name_with_no_subtitle_text():
    assert _audio_filename("mp3", None) == "clip.mp3"


def test_audio_filename_falls_back_when_subtitle_text_slugifies_to_nothing():
    assert _audio_filename("mp3", "!!!") == "clip.mp3"


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


def test_error_detail_from_discord_gives_a_clear_message_for_payload_too_large():
    exc = SimpleNamespace(code=40005, text="Request entity too large")
    detail = _error_detail_from_discord(exc)
    assert "too large" in detail
    assert "shorter span" in detail


def test_error_detail_from_discord_falls_back_to_raw_text_for_other_errors():
    exc = SimpleNamespace(code=50013, text="Missing Permissions")
    assert _error_detail_from_discord(exc) == "Missing Permissions"
