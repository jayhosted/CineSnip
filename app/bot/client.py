from __future__ import annotations

import discord
from discord.ext import commands

from app.bot.cogs.gif import GifCog
from app.bot.worker_client import WorkerClient


class CineSnipBot(commands.Bot):
    def __init__(self, worker_base_url: str):
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)
        self.worker = WorkerClient(worker_base_url)

    async def setup_hook(self) -> None:
        await self.add_cog(GifCog(self))
        await self.tree.sync()

    async def close(self) -> None:
        await self.worker.close()
        await super().close()


def build_bot(worker_base_url: str) -> CineSnipBot:
    return CineSnipBot(worker_base_url)
