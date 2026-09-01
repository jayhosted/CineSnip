import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord

from app.bot import soundboard as sb
from app.bot.cogs.gif import (
    AudioClipResultView,
    _SoundboardNameModal,
    _SoundboardReplacePickerView,
    _filter_replace_candidates,
    _soundboard_default_name,
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


def test_soundboard_default_name_prefers_subtitle_text():
    assert _soundboard_default_name("The Matrix", "I know kung fu") == "I know kung fu"


def test_soundboard_default_name_falls_back_to_title_when_no_subtitle_text():
    assert _soundboard_default_name("The Matrix", None) == "The Matrix"


def test_soundboard_default_name_truncates_to_32_chars():
    long_text = "This is a very long subtitle line that exceeds Discord's limit"
    result = _soundboard_default_name("The Matrix", long_text)
    assert result == long_text[:32]
    assert len(result) == 32


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


def _make_view(*, duration=4.0, format="mp3", scope=None, subtitle_text=None) -> AudioClipResultView:
    return AudioClipResultView(
        999,
        _FakeWorker(),
        media_id="1",
        title="The Matrix",
        content=b"audio-bytes",
        filename="clip.mp3",
        clip_start=10.0,
        clip_duration=duration,
        format=format,
        soundboard_replace_scope=(lambda: scope) if scope is not None else None,
        subtitle_text=subtitle_text,
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


def test_add_to_soundboard_in_dm_is_plain_message_and_skips_can_upload(monkeypatch):
    can_upload_calls = []
    monkeypatch.setattr(sb, "can_upload", lambda guild: can_upload_calls.append(guild) or True)

    async def run():
        view = _make_view()
        interaction = _fake_interaction(guild=None)
        await view.add_to_soundboard.callback(interaction)
        return interaction

    interaction = asyncio.run(run())
    interaction.response.send_message.assert_awaited_once()
    (msg,), kwargs = interaction.response.send_message.await_args
    assert msg == "Soundboard sounds can only be added from inside a server."
    assert kwargs["ephemeral"] is True
    assert can_upload_calls == []


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


def test_add_to_soundboard_opens_name_modal_when_board_not_full(monkeypatch):
    async def run():
        view = _make_view(subtitle_text="I know kung fu")
        guild = _guild(create_expressions=True, sound_count=0)
        interaction = _fake_interaction(guild)
        await view.add_to_soundboard.callback(interaction)
        return interaction

    interaction = asyncio.run(run())
    interaction.response.send_message.assert_not_awaited()
    interaction.response.send_modal.assert_awaited_once()
    (modal,), _ = interaction.response.send_modal.await_args
    assert isinstance(modal, _SoundboardNameModal)
    assert modal.name_input.default == "I know kung fu"


def test_soundboard_name_modal_confirm_uploads_with_submitted_name(monkeypatch):
    upload_mock = AsyncMock()
    monkeypatch.setattr(sb, "upload", upload_mock)

    async def run():
        view = _make_view()
        guild = _guild(create_expressions=True, sound_count=0)
        interaction = _fake_interaction(guild)
        await view.add_to_soundboard.callback(interaction)
        (modal,), _ = interaction.response.send_modal.await_args

        # Simulates the user editing the pre-filled field and submitting —
        # discord.py itself populates `._value` from the real modal-submit
        # payload the same way (TextInput._refresh_state); `.default`'s
        # setter only updates the component's rendered default, not the
        # live submitted value, so there's no public API to fake this with.
        modal.name_input._value = "Custom Name"
        submit_interaction = AsyncMock()
        await modal.on_submit(submit_interaction)
        return submit_interaction

    submit_interaction = asyncio.run(run())
    upload_mock.assert_awaited_once()
    _, kwargs = upload_mock.await_args
    assert kwargs["name"] == "Custom Name"
    assert kwargs["sound"] == b"audio-bytes"
    submit_interaction.followup.send.assert_awaited_once()
    (msg,), _ = submit_interaction.followup.send.await_args
    assert "Added" in msg


def test_add_to_soundboard_full_board_none_scope_is_plain_error(monkeypatch):
    async def run():
        view = _make_view(scope="none")
        guild = _guild(create_expressions=True, sound_count=8)  # tier 0 cap is 8
        interaction = _fake_interaction(guild)
        await view.add_to_soundboard.callback(interaction)
        return interaction

    interaction = asyncio.run(run())
    interaction.response.send_message.assert_awaited_once()
    (msg,), kwargs = interaction.response.send_message.await_args
    assert msg == "This server's Soundboard is full."
    assert "view" not in kwargs


def test_add_to_soundboard_full_board_offers_picker_for_cinesnip_only(monkeypatch):
    bot_sound = SimpleNamespace(id=1, name="bot-sound", user=SimpleNamespace(id=999))
    human_sound = SimpleNamespace(id=2, name="human-sound", user=SimpleNamespace(id=1))
    monkeypatch.setattr(sb, "list_sounds", AsyncMock(return_value=[bot_sound, human_sound]))

    async def run():
        view = _make_view(scope="cinesnip_only")
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


def test_add_to_soundboard_full_board_no_eligible_candidates_is_plain_error(monkeypatch):
    # cinesnip_only scope but every sound on the board belongs to a human —
    # nothing eligible to offer, so this must be a plain error, not a
    # picker with zero options.
    human_sound = SimpleNamespace(id=2, name="human-sound", user=SimpleNamespace(id=1))
    monkeypatch.setattr(sb, "list_sounds", AsyncMock(return_value=[human_sound]))

    async def run():
        view = _make_view(scope="cinesnip_only")
        guild = _guild(create_expressions=True, sound_count=8)
        interaction = _fake_interaction(guild)
        await view.add_to_soundboard.callback(interaction)
        return interaction

    interaction = asyncio.run(run())
    interaction.followup.send.assert_awaited_once()
    (msg,), kwargs = interaction.followup.send.await_args
    assert "nothing eligible to replace" in msg
    assert "view" not in kwargs


def test_replace_picker_opens_name_modal_after_guards_pass(monkeypatch):
    sound = SimpleNamespace(id=1, name="old-sound", user=SimpleNamespace(id=999), guild=None)
    monkeypatch.setattr(sb, "can_replace", lambda guild, sound, bot_user_id: True)

    async def run():
        view = _make_view(subtitle_text="I know kung fu")
        picker = _SoundboardReplacePickerView(view, [sound], bot_user_id=999)
        interaction = AsyncMock()
        interaction.data = {"values": ["1"]}
        await picker._on_pick(interaction)
        return interaction

    interaction = asyncio.run(run())
    interaction.response.send_modal.assert_awaited_once()
    (modal,), _ = interaction.response.send_modal.await_args
    assert isinstance(modal, _SoundboardNameModal)
    assert modal.name_input.default == "I know kung fu"


def test_replace_picker_name_modal_confirm_reports_delete_then_upload_failure(monkeypatch):
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
        (modal,), _ = interaction.response.send_modal.await_args

        submit_interaction = AsyncMock()
        await modal.on_submit(submit_interaction)
        return submit_interaction

    submit_interaction = asyncio.run(run())
    submit_interaction.followup.send.assert_awaited_once()
    (msg,), _ = submit_interaction.followup.send.await_args
    assert "may now be missing" in msg
    assert "old-sound" in msg


def test_replace_picker_denies_pick_without_permission(monkeypatch):
    sound = SimpleNamespace(id=1, name="old-sound", user=SimpleNamespace(id=1), guild=None)
    replace_mock = AsyncMock()
    monkeypatch.setattr(sb, "replace", replace_mock)
    monkeypatch.setattr(sb, "can_replace", lambda guild, sound, bot_user_id: False)

    async def run():
        view = _make_view()
        picker = _SoundboardReplacePickerView(view, [sound], bot_user_id=999)
        interaction = AsyncMock()
        interaction.data = {"values": ["1"]}
        await picker._on_pick(interaction)
        return interaction

    interaction = asyncio.run(run())
    replace_mock.assert_not_awaited()
    interaction.response.send_message.assert_awaited_once()
    (msg,), kwargs = interaction.response.send_message.await_args
    assert "permission to replace" in msg
    assert kwargs["ephemeral"] is True


def test_soundboard_disabled_note_shown_only_when_duration_disables_it():
    async def run():
        eligible = _make_view(duration=4.0)
        over_cap = _make_view(duration=6.0)
        return eligible, over_cap

    eligible, over_cap = asyncio.run(run())
    assert eligible._soundboard_disabled_note() == ""
    note = over_cap._soundboard_disabled_note()
    assert "5.2s" in note
    assert "end_timecode" in note


def test_soundboard_disabled_note_appears_in_re_render_content():
    async def run():
        view = _make_view(duration=6.0)
        interaction = AsyncMock()
        render_result = SimpleNamespace(content=b"new-bytes", start=10.0, duration=6.0, format="mp3")
        await view._apply_render_result(interaction, render_result)
        return interaction

    interaction = asyncio.run(run())
    interaction.edit_original_response.assert_awaited_once()
    _, kwargs = interaction.edit_original_response.await_args
    assert "Add to Soundboard is disabled" in kwargs["content"]


def test_replace_picker_rejects_when_parent_clip_became_too_long(monkeypatch):
    # Simulates: picker opened while clip was eligible, then the parent
    # message's Duration/Merge controls mutated the clip past the
    # Soundboard's 5.2s cap before the user actually picked a sound.
    sound = SimpleNamespace(id=1, name="old-sound", user=SimpleNamespace(id=999), guild=None)
    replace_mock = AsyncMock()
    monkeypatch.setattr(sb, "replace", replace_mock)
    monkeypatch.setattr(sb, "can_replace", lambda guild, sound, bot_user_id: True)

    async def run():
        view = _make_view(duration=4.0)
        picker = _SoundboardReplacePickerView(view, [sound], bot_user_id=999)
        view._clip_duration = 6.0  # mutated after the picker was constructed
        interaction = AsyncMock()
        interaction.data = {"values": ["1"]}
        await picker._on_pick(interaction)
        return interaction

    interaction = asyncio.run(run())
    replace_mock.assert_not_awaited()
    interaction.response.send_message.assert_awaited_once()
    (msg,), kwargs = interaction.response.send_message.await_args
    assert "5.2s" in msg
    assert kwargs["ephemeral"] is True


def test_replace_picker_rejects_when_parent_content_became_too_large(monkeypatch):
    # Same scenario as above but for the 512KB size guard instead of
    # duration — a merge could grow the content past the cap too.
    sound = SimpleNamespace(id=1, name="old-sound", user=SimpleNamespace(id=999), guild=None)
    replace_mock = AsyncMock()
    monkeypatch.setattr(sb, "replace", replace_mock)
    monkeypatch.setattr(sb, "can_replace", lambda guild, sound, bot_user_id: True)

    async def run():
        view = _make_view()
        picker = _SoundboardReplacePickerView(view, [sound], bot_user_id=999)
        view._content = b"x" * 600_000  # mutated after the picker was constructed
        interaction = AsyncMock()
        interaction.data = {"values": ["1"]}
        await picker._on_pick(interaction)
        return interaction

    interaction = asyncio.run(run())
    replace_mock.assert_not_awaited()
    interaction.response.send_message.assert_awaited_once()
    (msg,), kwargs = interaction.response.send_message.await_args
    assert "too large" in msg
    assert kwargs["ephemeral"] is True


def test_replace_picker_name_modal_confirm_replaces_with_submitted_name(monkeypatch):
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
        (modal,), _ = interaction.response.send_modal.await_args

        modal.name_input._value = "New Sound Name"
        submit_interaction = AsyncMock()
        await modal.on_submit(submit_interaction)
        return submit_interaction

    submit_interaction = asyncio.run(run())
    replace_mock.assert_awaited_once()
    _, kwargs = replace_mock.await_args
    assert kwargs["name"] == "New Sound Name"
    submit_interaction.followup.send.assert_awaited_once()
    (msg,), _ = submit_interaction.followup.send.await_args
    assert "Replaced" in msg
