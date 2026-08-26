from __future__ import annotations

import discord
from discord.ext import commands

from app.bot.cogs.gif import GifCog
from app.bot.worker_client import WorkerClient


class CineSnipBot(commands.Bot):
    def __init__(self, worker_base_url: str, dev_guild_id: int | None = None):
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)
        self.worker = WorkerClient(worker_base_url)
        self._dev_guild_id = dev_guild_id

    async def setup_hook(self) -> None:
        await self.add_cog(GifCog(self))
        if self._dev_guild_id is not None:
            # Sync ONLY to the dev guild while iterating — guild-scoped
            # syncs apply near-instantly. Syncing globally as well (even
            # once) leaves two registrations of the same command visible
            # side by side once the global one propagates to this guild,
            # which it eventually does even during dev — that's what
            # caused "/cinesnip" to show twice in the command picker.
            # Explicitly clear any global registration a prior run left
            # behind before the guild-scoped copy below.
            guild = discord.Object(id=self._dev_guild_id)
            self.tree.copy_global_to(guild=guild)
            self.tree.clear_commands(guild=None)
            await self.tree.sync()
            await self.tree.sync(guild=guild)
        else:
            # Global sync is required for the command to eventually appear
            # in every server the bot is invited to, but Discord can take
            # up to an hour to propagate a global change.
            await self.tree.sync()

    async def close(self) -> None:
        await self.worker.close()
        await super().close()


def build_bot(worker_base_url: str, dev_guild_id: int | None = None) -> CineSnipBot:
    return CineSnipBot(worker_base_url, dev_guild_id=dev_guild_id)
