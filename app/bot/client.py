from __future__ import annotations

import logging
from typing import Callable

import discord
from discord.ext import commands

from app.bot.cogs.gif import GifCog
from app.bot.worker_client import WorkerClient

logger = logging.getLogger(__name__)

# Default matches Settings.render_defaults.soundboard_replace_scope's own
# Pydantic default — used when no accessor was wired (tests) or settings
# aren't loaded yet.
_DEFAULT_SOUNDBOARD_REPLACE_SCOPE = "cinesnip_only"


class CineSnipBot(commands.Bot):
    def __init__(
        self,
        worker_base_url: str,
        dev_guild_id: int | None = None,
        soundboard_replace_scope: Callable[[], str] | None = None,
    ):
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)
        self.worker = WorkerClient(worker_base_url)
        self._dev_guild_id = dev_guild_id
        # Bot-layer-only config (issue #10): unlike render_defaults, which
        # the worker applies and echoes back via response headers rather
        # than the bot reading directly (see gif.py's ClipEditView comment
        # on "config the bot can't see"), this has nothing to do with
        # Plex/ffmpeg — it's pure Discord-side behavior, so there's no
        # worker round-trip to echo it through. Deliberately a narrow
        # closure over just this one field, not a handle to the whole
        # SettingsHolder/Settings tree (CLAUDE.md Section 9: Plex/Discord
        # tokens and media access must stay confined to the worker module
        # for future auditability) — bot-layer code should never be able to
        # reach discord_token/plex_token/library path mappings through
        # this. Called fresh each time rather than read once at
        # construction, so a later /wizard/restart reconfiguration is
        # picked up without needing a bot rebuild.
        self._soundboard_replace_scope = soundboard_replace_scope or (
            lambda: _DEFAULT_SOUNDBOARD_REPLACE_SCOPE
        )

    def soundboard_replace_scope(self) -> str:
        return self._soundboard_replace_scope()

    async def setup_hook(self) -> None:
        await self.add_cog(GifCog(self))
        # Global sync is required for commands to eventually appear in every
        # server the bot is invited to (Discord can take up to an hour to
        # propagate a global change) — always do this regardless of dev mode,
        # so other servers are never disrupted by local iteration.
        await self.tree.sync()
        if self._dev_guild_id is not None:
            logger.warning(
                "DEV_GUILD_ID=%s is set — commands will ALSO sync instantly "
                "to that guild for local iteration. This is a local-dev-only "
                "setting; unset it once you're done to remove the extra "
                "guild-scoped copy.",
                self._dev_guild_id,
            )
            # Guild-scoped syncs apply near-instantly, unlike the global sync
            # above. This is additive, not a replacement for it, so toggling
            # DEV_GUILD_ID on/off never wipes or delays commands anywhere else.
            guild = discord.Object(id=self._dev_guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)

    async def close(self) -> None:
        await self.worker.close()
        await super().close()


def build_bot(
    worker_base_url: str,
    dev_guild_id: int | None = None,
    soundboard_replace_scope: Callable[[], str] | None = None,
) -> CineSnipBot:
    return CineSnipBot(
        worker_base_url,
        dev_guild_id=dev_guild_id,
        soundboard_replace_scope=soundboard_replace_scope,
    )
