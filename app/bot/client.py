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
        # Global sync is required for the command to eventually appear in
        # every server the bot is invited to, but Discord can take up to an
        # hour to propagate a global change. When DEV_GUILD_ID is set, also
        # copy + sync to that one guild directly — guild-scoped syncs apply
        # near-instantly, which is what you want while iterating on commands.
        await self.tree.sync()
        if self._dev_guild_id is not None:
            guild = discord.Object(id=self._dev_guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)

    async def close(self) -> None:
        await self.worker.close()
        await super().close()


def build_bot(worker_base_url: str, dev_guild_id: int | None = None) -> CineSnipBot:
    return CineSnipBot(worker_base_url, dev_guild_id=dev_guild_id)
