from types import SimpleNamespace

import pytest

from app.bot import soundboard


def _guild(create_expressions=True, manage_expressions=False, premium_tier=0, sound_count=0):
    perms = SimpleNamespace(create_expressions=create_expressions, manage_expressions=manage_expressions)
    me = SimpleNamespace(guild_permissions=perms)
    return SimpleNamespace(
        me=me, premium_tier=premium_tier, soundboard_sounds=[object()] * sound_count
    )


def _sound(user_id=None):
    user = SimpleNamespace(id=user_id) if user_id is not None else None
    return SimpleNamespace(user=user)


def test_can_upload_true_with_create_expressions():
    assert soundboard.can_upload(_guild(create_expressions=True)) is True


def test_can_upload_false_without_create_expressions():
    assert soundboard.can_upload(_guild(create_expressions=False)) is False


def test_can_replace_own_sound_needs_only_create_expressions():
    guild = _guild(create_expressions=True, manage_expressions=False)
    sound = _sound(user_id=42)
    assert soundboard.can_replace(guild, sound, bot_user_id=42) is True


def test_can_replace_others_sound_needs_manage_expressions():
    guild = _guild(create_expressions=True, manage_expressions=False)
    sound = _sound(user_id=99)
    assert soundboard.can_replace(guild, sound, bot_user_id=42) is False

    guild_with_manage = _guild(create_expressions=True, manage_expressions=True)
    assert soundboard.can_replace(guild_with_manage, sound, bot_user_id=42) is True


def test_can_replace_sound_with_no_user_needs_manage_expressions():
    guild = _guild(create_expressions=True, manage_expressions=False)
    sound = _sound(user_id=None)
    assert soundboard.can_replace(guild, sound, bot_user_id=42) is False


@pytest.mark.parametrize(
    "premium_tier,sound_count,expected",
    [(0, 8, True), (0, 7, False), (1, 24, True), (2, 36, True), (3, 48, True), (3, 47, False)],
)
def test_is_full_matches_boost_tier_cap(premium_tier, sound_count, expected):
    guild = _guild(premium_tier=premium_tier, sound_count=sound_count)
    assert soundboard.is_full(guild) is expected
