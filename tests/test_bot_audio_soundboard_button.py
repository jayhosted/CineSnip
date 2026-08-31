import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord

from app.bot import soundboard as sb
from app.bot.cogs.gif import (
    AudioClipResultView,
    _SoundboardReplacePickerView,
    _filter_replace_candidates,
    _soundboard_eligible,
)


def test_soundboard_eligible_true_for_short_mp3():
    assert _soundboard_eligible(duration=4.0, format="mp3") is True


def test_soundboard_eligible_false_over_5_2_seconds():
    assert _soundboard_eligible(duration=5.3, format="mp3") is False


def test_soundboard_eligible_false_for_non_audio_format():
    assert _soundboard_eligible(duration=4.0, format="gif") is False


def test_filter_replace_candidates_cinesnip_only():
    class _S:
        def __init__(self, user_id):
            self.user = type("U", (), {"id": user_id})() if user_id else None

    sounds = [_S(1), _S(2), _S(None)]
    result = _filter_replace_candidates(sounds, scope="cinesnip_only", bot_user_id=1)
    assert result == [sounds[0]]


def test_filter_replace_candidates_any_returns_everything():
    class _S:
        def __init__(self, user_id):
            self.user = type("U", (), {"id": user_id})() if user_id else None

    sounds = [_S(1), _S(2), _S(None)]
    result = _filter_replace_candidates(sounds, scope="any", bot_user_id=1)
    assert result == sounds


def test_filter_replace_candidates_none_returns_empty():
    class _S:
        def __init__(self, user_id):
            self.user = type("U", (), {"id": user_id})() if user_id else None

    sounds = [_S(1), _S(2), _S(None)]
    result = _filter_replace_candidates(sounds, scope="none", bot_user_id=1)
    assert result == []


# --- Button/view wiring: fakes only, no mocking of discord.py's HTTP layer
# (per the brief's own guidance — mirror tests/test_render_size_limit.py). ---


class _FakeWorker:
    def __init__(self) -> None:
        self.subtitles = AsyncMock(return_value=[])


def _make_view(*, duration=4.0, format="mp3", settings_holder=None) -> AudioClipResultView:
    return AudioClipResultView(
        _FakeWorker(),
        rating_key=1,
        title="The Matrix",
        content=b"audio-bytes",
        filename="clip.mp3",
        clip_start=10.0,
        clip_duration=duration,
        format=format,
        settings_holder=settings_holder,
    )


def _guild(create_expressions=True, manage_expressions=False, premium_tier=0, sound_count=0):
    perms = SimpleNamespace(create_expressions=create_expressions, manage_expressions=manage_expressions)
    me = SimpleNamespace(guild_permissions=perms)
    return SimpleNamespace(
        me=me, premium_tier=premium_tier, soundboard_sounds=[object()] * sound_count
    )


def _fake_interaction(guild) -> AsyncMock:
    interaction = AsyncMock()
    interaction.guild = guild
    interaction.client.user.id = 999
    return interaction


def test_button_disabled_on_construction_when_ineligible():
    async def run():
        return _make_view(duration=6.0, format="mp3")

    view = asyncio.run(run())
    assert view.add_to_soundboard.disabled is True


def test_button_enabled_on_construction_when_eligible():
    async def run():
        return _make_view(duration=4.0, format="mp3")

    view = asyncio.run(run())
    assert view.add_to_soundboard.disabled is False


def test_button_re_enabled_state_updates_after_apply_render_result(monkeypatch):
    async def run():
        view = _make_view(duration=6.0, format="mp3")
        assert view.add_to_soundboard.disabled is True

        # Simulate a nudge/merge re-render bringing the clip under the cap,
        # via _apply_render_result directly (worker.render itself isn't
        # under test here).
        interaction = AsyncMock()
        render_result = SimpleNamespace(content=b"new-bytes", start=10.0, duration=3.0, format="mp3")
        await view._apply_render_result(interaction, render_result)
        assert view.add_to_soundboard.disabled is False

    asyncio.run(run())


def test_add_to_soundboard_blocks_without_create_expressions_permission():
    async def run():
        view = _make_view()
        guild = _guild(create_expressions=False)
        interaction = _fake_interaction(guild)
        await view.add_to_soundboard.callback(interaction)
        return interaction

    interaction = asyncio.run(run())
    interaction.response.send_message.assert_awaited_once()
    (msg,), kwargs = interaction.response.send_message.await_args
    assert "Create Expressions" in msg
    assert kwargs["ephemeral"] is True


def test_add_to_soundboard_blocks_when_content_too_large():
    async def run():
        view = _make_view()
        view._content = b"x" * 600_000
        guild = _guild(create_expressions=True)
        interaction = _fake_interaction(guild)
        await view.add_to_soundboard.callback(interaction)
        return interaction

    interaction = asyncio.run(run())
    interaction.response.send_message.assert_awaited_once()
    (msg,), _ = interaction.response.send_message.await_args
    assert "too large" in msg


def test_add_to_soundboard_uploads_when_board_not_full(monkeypatch):
    upload_mock = AsyncMock()
    monkeypatch.setattr(sb, "upload", upload_mock)

    async def run():
        view = _make_view()
        guild = _guild(create_expressions=True, sound_count=0)
        interaction = _fake_interaction(guild)
        await view.add_to_soundboard.callback(interaction)
        return interaction

    interaction = asyncio.run(run())
    upload_mock.assert_awaited_once()
    _, kwargs = upload_mock.await_args
    assert kwargs["sound"] == b"audio-bytes"
    interaction.followup.send.assert_awaited_once()
    (msg,), _ = interaction.followup.send.await_args
    assert "Added" in msg


def test_add_to_soundboard_full_board_none_scope_is_plain_error(monkeypatch):
    async def run():
        view = _make_view(settings_holder=SimpleNamespace(
            settings=SimpleNamespace(render_defaults=SimpleNamespace(soundboard_replace_scope="none"))
        ))
        guild = _guild(create_expressions=True, sound_count=8)  # tier 0 cap is 8
        interaction = _fake_interaction(guild)
        await view.add_to_soundboard.callback(interaction)
        return interaction

    interaction = asyncio.run(run())
    interaction.followup.send.assert_awaited_once()
    (msg,), kwargs = interaction.followup.send.await_args
    assert msg == "This server's Soundboard is full."
    assert "view" not in kwargs


def test_add_to_soundboard_full_board_offers_picker_for_cinesnip_only(monkeypatch):
    bot_sound = SimpleNamespace(id=1, name="bot-sound", user=SimpleNamespace(id=999))
    human_sound = SimpleNamespace(id=2, name="human-sound", user=SimpleNamespace(id=1))
    monkeypatch.setattr(sb, "list_sounds", AsyncMock(return_value=[bot_sound, human_sound]))

    async def run():
        view = _make_view(settings_holder=SimpleNamespace(
            settings=SimpleNamespace(
                render_defaults=SimpleNamespace(soundboard_replace_scope="cinesnip_only")
            )
        ))
        guild = _guild(create_expressions=True, sound_count=8)
        interaction = _fake_interaction(guild)
        await view.add_to_soundboard.callback(interaction)
        return interaction

    interaction = asyncio.run(run())
    interaction.followup.send.assert_awaited_once()
    _, kwargs = interaction.followup.send.await_args
    picker = kwargs["view"]
    assert isinstance(picker, _SoundboardReplacePickerView)
    assert list(picker._candidates.keys()) == [1]


def test_replace_picker_delete_then_upload_failure_reports_data_loss_risk(monkeypatch):
    sound = SimpleNamespace(id=1, name="old-sound", user=SimpleNamespace(id=999), guild=None)

    async def failing_replace(*args, **kwargs):
        raise discord.HTTPException(SimpleNamespace(status=500, reason="boom"), "server error")

    monkeypatch.setattr(sb, "replace", failing_replace)
    monkeypatch.setattr(sb, "can_replace", lambda guild, sound, bot_user_id: True)

    async def run():
        view = _make_view()
        picker = _SoundboardReplacePickerView(view, [sound], bot_user_id=999)
        interaction = AsyncMock()
        interaction.data = {"values": ["1"]}
        await picker._on_pick(interaction)
        return interaction

    interaction = asyncio.run(run())
    interaction.followup.send.assert_awaited_once()
    (msg,), _ = interaction.followup.send.await_args
    assert "may now be missing" in msg
    assert "old-sound" in msg


def test_replace_picker_success_sends_confirmation(monkeypatch):
    sound = SimpleNamespace(id=1, name="old-sound", user=SimpleNamespace(id=999), guild=None)
    replace_mock = AsyncMock()
    monkeypatch.setattr(sb, "replace", replace_mock)
    monkeypatch.setattr(sb, "can_replace", lambda guild, sound, bot_user_id: True)

    async def run():
        view = _make_view()
        picker = _SoundboardReplacePickerView(view, [sound], bot_user_id=999)
        interaction = AsyncMock()
        interaction.data = {"values": ["1"]}
        await picker._on_pick(interaction)
        return interaction

    interaction = asyncio.run(run())
    replace_mock.assert_awaited_once()
    interaction.followup.send.assert_awaited_once()
    (msg,), _ = interaction.followup.send.await_args
    assert "Replaced" in msg
