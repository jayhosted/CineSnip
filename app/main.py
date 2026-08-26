from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path

import uvicorn
import uvloop

from app.bot.client import build_bot
from app.settings import load_settings
from app.worker.api import create_app

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("cinesnip")


def _clear_scratch_dir(scratch_dir: Path) -> None:
    shutil.rmtree(scratch_dir, ignore_errors=True)
    scratch_dir.mkdir(parents=True, exist_ok=True)


async def main() -> None:
    settings = load_settings()
    _clear_scratch_dir(settings.scratch_dir)
    settings.cache_dir.mkdir(parents=True, exist_ok=True)

    worker_app = create_app(settings)
    server_config = uvicorn.Config(
        worker_app, host="127.0.0.1", port=settings.worker.port, log_level="info"
    )
    server = uvicorn.Server(server_config)

    bot = build_bot(
        f"http://127.0.0.1:{settings.worker.port}",
        dev_guild_id=settings.dev_guild_id,
    )

    async with bot:
        await asyncio.gather(server.serve(), bot.start(settings.discord_token))


if __name__ == "__main__":
    # uvicorn.Server.serve() is awaited directly here rather than run via
    # uvicorn.run()/Server.run() — needed so the bot and worker share one
    # event loop (asyncio.gather() above) instead of uvicorn owning its own.
    # That means uvicorn's own automatic uvloop activation (which only
    # kicks in when it manages the top-level loop itself) never fires here
    # — confirmed via a real run: plain asyncio.run() left the standard
    # asyncio loop active, not uvloop, even with uvloop installed. Found
    # while trimming uvicorn's [standard] extra down to just its two
    # actually-used pieces (uvloop, httptools) — uvloop was already being
    # paid for in image size without the app ever actually getting its
    # benefit. uvloop.run() (a drop-in asyncio.run() replacement) is what
    # actually activates it.
    uvloop.run(main())
