"""Thin wrapper over discord.py's Soundboard API (2.5+) for issue #10:
letting a generated audio clip be added to a guild's Discord Soundboard.
Kept separate from app/bot/cogs/gif.py's UI/flow code and from
app/worker/ — this is Discord-API surface, not Plex/ffmpeg (CLAUDE.md
Section 9's Plex/worker-vs-bot separation extends here for the same
future-auditability reason)."""

from __future__ import annotations

import discord

# Discord's published per-boost-tier Soundboard slot caps. Confirm these
# against current Discord developer docs before relying on them in
# production — not verified against discord.py library source the way the
# method calls in this module are, since discord.py doesn't itself enforce
# or expose this cap.
_SLOT_CAP_BY_PREMIUM_TIER = {0: 8, 1: 24, 2: 36, 3: 48}


def can_upload(guild: discord.Guild) -> bool:
    return guild.me.guild_permissions.create_expressions


def can_replace(guild: discord.Guild, sound: discord.SoundboardSound, bot_user_id: int) -> bool:
    # A bot-created sound only needs create_expressions to replace (matches
    # SoundboardSound.edit/.delete's own documented permission rule); any
    # other sound needs the stronger manage_expressions.
    is_own_sound = sound.user is not None and sound.user.id == bot_user_id
    if is_own_sound:
        return guild.me.guild_permissions.create_expressions
    return guild.me.guild_permissions.manage_expressions


def is_full(guild: discord.Guild) -> bool:
    cap = _SLOT_CAP_BY_PREMIUM_TIER.get(guild.premium_tier, _SLOT_CAP_BY_PREMIUM_TIER[0])
    return len(guild.soundboard_sounds) >= cap


async def list_sounds(guild: discord.Guild) -> list[discord.SoundboardSound]:
    return await guild.fetch_soundboard_sounds()


async def upload(
    guild: discord.Guild, *, name: str, sound: bytes, reason: str | None = None
) -> discord.SoundboardSound:
    return await guild.create_soundboard_sound(name=name, sound=sound, reason=reason)


async def replace(
    sound: discord.SoundboardSound, *, name: str, new_sound: bytes, reason: str | None = None
) -> discord.SoundboardSound:
    guild = sound.guild
    await sound.delete(reason=reason)
    return await upload(guild, name=name, sound=new_sound, reason=reason)
