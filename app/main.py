from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path

import uvicorn

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

    worker_app = create_app(settings)
    server_config = uvicorn.Config(
        worker_app, host="127.0.0.1", port=settings.worker.port, log_level="info"
    )
    server = uvicorn.Server(server_config)

    bot = build_bot(f"http://127.0.0.1:{settings.worker.port}")

    async with bot:
        await asyncio.gather(server.serve(), bot.start(settings.discord_token))


if __name__ == "__main__":
    asyncio.run(main())
