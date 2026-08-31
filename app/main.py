from __future__ import annotations

import asyncio
import logging
import os
import shutil
from pathlib import Path

import uvicorn
import uvloop

from app.bot.client import CineSnipBot, build_bot
from app.runtime import SettingsHolder
from app.settings import Settings, SettingsError, load_settings
from app.web.app import create_web_app
from app.worker import library_sync, quote_index
from app.worker.api import create_app

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("cinesnip")


def _clear_scratch_dir(scratch_dir: Path) -> None:
    shutil.rmtree(scratch_dir, ignore_errors=True)
    scratch_dir.mkdir(parents=True, exist_ok=True)


def _try_load_settings() -> Settings | None:
    try:
        return load_settings()
    except SettingsError as exc:
        logger.info("%s", exc)
        return None


async def _start_worker(settings: Settings) -> tuple[object, uvicorn.Server, asyncio.Task]:
    worker_app = create_app(settings)
    server = uvicorn.Server(
        uvicorn.Config(worker_app, host="127.0.0.1", port=settings.worker.port, log_level="info")
    )
    task = asyncio.create_task(server.serve())
    return worker_app, server, task


async def _stop_worker(server: uvicorn.Server, task: asyncio.Task) -> None:
    server.should_exit = True
    await task


def _read_soundboard_replace_scope(settings_holder: SettingsHolder) -> str:
    # Narrow accessor threaded into the bot instead of the whole
    # SettingsHolder/Settings tree (issue #10 review finding) — reads live
    # off settings_holder each call, so a /wizard/restart reconfiguration
    # is picked up without a bot rebuild, but bot-layer code never gets a
    # handle to discord_token/plex_token/library path mappings/etc.
    settings = settings_holder.settings
    if settings is None:
        return "cinesnip_only"
    return settings.render_defaults.soundboard_replace_scope


async def _start_bot(
    settings: Settings, settings_holder: SettingsHolder
) -> tuple[CineSnipBot, asyncio.Task]:
    bot = build_bot(
        f"http://127.0.0.1:{settings.worker.port}",
        dev_guild_id=settings.dev_guild_id,
        soundboard_replace_scope=lambda: _read_soundboard_replace_scope(settings_holder),
    )
    task = asyncio.create_task(bot.start(settings.discord_token))
    return bot, task


async def _stop_bot(bot: CineSnipBot, task: asyncio.Task) -> None:
    await bot.close()
    await task


async def main() -> None:
    settings_holder = SettingsHolder(settings=_try_load_settings())
    reconfigured = asyncio.Event()

    async def on_setup_complete() -> None:
        # Fired by the wizard's "Finish setup" step or a later
        # reconfiguration via /wizard/restart (decision #6) — either way
        # this must not restart the container, just swap in the newly
        # written config and let main()'s loop below react to it.
        try:
            new_settings = load_settings(override_env=True)
        except SettingsError as exc2:
            logger.error(
                "Setup wizard reported success, but the resulting config is "
                "still invalid: %s. Check .env/config.yaml, or use /wizard/restart "
                "to try again.",
                exc2,
            )
            return
        settings_holder.settings = new_settings
        reconfigured.set()

    # The web app runs for the container's entire lifetime, not just a
    # one-shot first-run phase: while settings_holder.settings is None it
    # serves only the setup wizard; once configured it also serves
    # /generate and stays reachable at /wizard/... as the reconfiguration
    # entry point.
    web_app = create_web_app(settings_holder, on_setup_complete)
    web_port = int(os.environ.get("WIZARD_PORT", "1919"))
    web_server = uvicorn.Server(uvicorn.Config(web_app, host="0.0.0.0", port=web_port, log_level="info"))
    web_task = asyncio.create_task(web_server.serve())

    if settings_holder.settings is None:
        logger.info(
            "No valid config found — serving the setup wizard on port %d until it's complete.",
            web_port,
        )
        reconfigured_wait = asyncio.create_task(reconfigured.wait())
        done, _pending = await asyncio.wait({web_task, reconfigured_wait}, return_when=asyncio.FIRST_COMPLETED)
        if web_task in done:
            # The web server itself died before setup ever completed —
            # nothing to fall through to.
            await web_task
            return
        reconfigured.clear()
        logger.info("Setup complete — starting the bot and worker.")

    # From here on, the worker (and the bot, when its token changes) get
    # rebuilt in place whenever settings_holder.settings changes, without
    # ever tearing down web_task — that's what lets a later /wizard/restart
    # reconfiguration apply live instead of requiring a container restart.
    worker_app = None
    worker_server: uvicorn.Server | None = None
    worker_task: asyncio.Task | None = None
    bot: CineSnipBot | None = None
    bot_task: asyncio.Task | None = None
    sync_task: asyncio.Task | None = None
    current_discord_token: str | None = None
    current_worker_port: int | None = None

    try:
        while True:
            settings = settings_holder.settings
            assert settings is not None
            _clear_scratch_dir(settings.scratch_dir)
            settings.cache_dir.mkdir(parents=True, exist_ok=True)
            quote_index.reset_stale_running_status(settings.quote_index_db_path)

            if worker_server is not None:
                await _stop_worker(worker_server, worker_task)
            worker_app, worker_server, worker_task = await _start_worker(settings)
            settings_holder.media_client = worker_app.state.media

            # discord.py's gateway connection can't swap tokens on an
            # already-connected Client, so only tear down and rebuild the
            # bot when the token (or the worker port it talks to) actually
            # changed — a Plex/library-only reconfiguration leaves the
            # running bot/gateway connection untouched.
            needs_new_bot = (
                bot is None
                or settings.discord_token != current_discord_token
                or settings.worker.port != current_worker_port
            )
            if needs_new_bot:
                if bot is not None:
                    settings_holder.bot = None
                    await _stop_bot(bot, bot_task)
                bot, bot_task = await _start_bot(settings, settings_holder)
                settings_holder.bot = bot
                current_discord_token = settings.discord_token
                current_worker_port = settings.worker.port

            if sync_task is not None:
                sync_task.cancel()
                await asyncio.gather(sync_task, return_exceptions=True)
            if settings.library_sync.enabled and settings.media_server == "plex":
                # Reuses the worker's own media client (create_app() sets it
                # on app.state.media) rather than opening a second Plex
                # connection. library_sync is Plex-only (issue #25);
                # load_settings() rejects jellyfin + library_sync.enabled
                # before it ever gets here, so the media_server check is a
                # guard for a Settings built some other way (e.g. the
                # Settings area toggling sync on in-process), logged loudly
                # below rather than left to fail inside the sync loop.
                sync_task = asyncio.create_task(
                    library_sync.library_sync_task(settings, worker_app.state.media)
                )
            else:
                if settings.library_sync.enabled:
                    logger.error(
                        "library_sync.enabled is set but media_server is '%s' — "
                        "library auto-sync is only supported with Plex (issue #25). "
                        "Not starting the sync task; set library_sync.enabled: false "
                        "in config.yaml to clear this message.",
                        settings.media_server,
                    )
                sync_task = None

            reconfigured.clear()
            reconfigured_wait = asyncio.create_task(reconfigured.wait())
            watched = {web_task, worker_task, bot_task, reconfigured_wait}
            if sync_task is not None:
                watched.add(sync_task)

            done, pending = await asyncio.wait(watched, return_when=asyncio.FIRST_COMPLETED)

            if reconfigured_wait in done:
                # A live reconfiguration — loop back around and rebuild
                # whatever needs rebuilding above.
                continue

            # Anything else finishing is unexpected (a crash, or the web
            # server dying) — surface it rather than looping forever.
            for task in pending:
                task.cancel()
            for finished in done:
                await finished
            return
    finally:
        web_server.should_exit = True
        try:
            await web_task
        except asyncio.CancelledError:
            pass


if __name__ == "__main__":
    # uvicorn.Server.serve() is awaited directly here (not via
    # uvicorn.run()) so the bot and worker share one event loop. That means
    # uvicorn's automatic uvloop activation never fires (it only kicks in
    # when uvicorn owns the top-level loop) — uvloop was being paid for in
    # image size with no benefit until this was caught. uvloop.run(), a
    # drop-in asyncio.run() replacement, is what actually activates it.
    # See docs/build-notes/docker-image.md.
    uvloop.run(main())
