from __future__ import annotations

import asyncio
import logging
import os
import shutil
from pathlib import Path

import uvicorn
import uvloop

from app.bot.client import build_bot
from app.settings import SettingsError, load_settings
from app.web.wizard import create_wizard_app
from app.worker import library_sync
from app.worker.api import create_app

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("cinesnip")


def _clear_scratch_dir(scratch_dir: Path) -> None:
    shutil.rmtree(scratch_dir, ignore_errors=True)
    scratch_dir.mkdir(parents=True, exist_ok=True)


async def _run_wizard_until_complete() -> None:
    # Section 14: on first run (or any incomplete .env/config.yaml), serve
    # *only* the setup wizard until it's validated complete — no Discord
    # bot, no worker API. Runs its own uvicorn.Server on WIZARD_PORT (0.0.0.0
    # so Docker's port mapping can reach it).
    #
    # The *host*-side docker-compose.yml `127.0.0.1:1919:1919` mapping is
    # the ONLY thing enforcing "localhost only by default" — deliberately,
    # not for lack of trying an app-level check too. Verified directly
    # against this project's own reference environment (Docker Desktop,
    # WSL2 backend): a request reaching the container through a published
    # port arrives with its source rewritten to the docker bridge gateway
    # address (e.g. 172.17.0.1) by Docker's own proxying, for BOTH a
    # loopback-origin request and a LAN-origin request — confirmed
    # identical for both in a real test. So request.client.host inside the
    # app can't distinguish "someone on this machine" from "someone on the
    # LAN" at all on this topology; an app-level "reject non-loopback"
    # check here would silently be a no-op, not a real second layer of
    # defense. Do not add one without first confirming it actually sees a
    # meaningfully different address for the two cases on whatever Docker
    # topology it's meant to protect — on Docker Desktop it doesn't.
    #
    # This means the host-side port binding genuinely is the whole
    # boundary: anyone editing docker-compose.yml to drop the `127.0.0.1:`
    # prefix (Section 14's documented LAN opt-in) is trusted to understand
    # they're exposing the raw token-entry wizard to their LAN with no
    # other gate in front of it.
    #
    # Shuts itself down the moment the wizard's final step reports success
    # — main() then re-runs load_settings() and falls straight through into
    # the real startup path below, all in the same process, no container
    # restart required.
    done = asyncio.Event()
    wizard_app = create_wizard_app(on_complete=done.set)
    port = int(os.environ.get("WIZARD_PORT", "1919"))
    server = uvicorn.Server(uvicorn.Config(wizard_app, host="0.0.0.0", port=port, log_level="info"))
    logger.info("No valid config found — serving the setup wizard on port %d until it's complete.", port)
    serve_task = asyncio.create_task(server.serve())
    await done.wait()
    server.should_exit = True
    await serve_task


async def main() -> None:
    try:
        settings = load_settings()
    except SettingsError as exc:
        logger.info("%s", exc)
        await _run_wizard_until_complete()
        # override_env=True: the wizard just wrote real values to .env, but
        # os.environ already holds the empty placeholders Docker's
        # `env_file:` loaded from .env.example at container start — plain
        # load_settings() would see those keys as "already set" and never
        # pick up what the wizard wrote (see load_settings()'s override_env
        # docstring comment in app/settings.py). Still wrapped in
        # try/except: the wizard's own validation step doesn't run every
        # check load_settings()/Settings' pydantic validation does, so a
        # config that looked complete to the wizard could still fail here —
        # that must surface as a clear log message, not a raw traceback.
        try:
            settings = load_settings(override_env=True)
        except SettingsError as exc2:
            logger.error(
                "Setup wizard reported success, but the resulting config is "
                "still invalid: %s. Check .env/config.yaml, or delete them "
                "and re-run the wizard.",
                exc2,
            )
            raise
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

    coros = [server.serve(), bot.start(settings.discord_token)]
    if settings.library_sync.enabled:
        # Reuses the same PlexClient the worker API already constructed
        # (create_app() sets it on app.state.plex) rather than opening a
        # second Plex connection.
        coros.append(library_sync.library_sync_task(settings, worker_app.state.plex))

    async with bot:
        await asyncio.gather(*coros)


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
