from __future__ import annotations

from dataclasses import dataclass

from app.bot.client import CineSnipBot
from app.settings import Settings


@dataclass
class SettingsHolder:
    """Mutable holder shared between the persistent web app (app/web/app.py)
    and main()'s startup/reload loop. `settings` is None before first-run
    setup completes, then gets replaced in place after a later
    reconfiguration (Section 14/decision #6) — a single shared reference so
    "apply the new config" is one assignment, not a hunt through every
    closure that captured the old Settings by value.

    `bot` mirrors the same pattern for the live discord.py client: main()'s
    loop rebuilds the bot in place whenever the Discord token (or the worker
    port it talks to) changes, and a plain module-level reference would go
    stale across that rebuild. The web app's /generate "Post to Discord"
    action (a second, thin client of the same bot instance the Discord
    commands use — same reasoning as decision #3's worker API split) reads
    this field live rather than holding its own copy, so it always sees the
    currently-connected bot, or None while one isn't up yet (startup, a
    reconfiguration mid-swap, or the gateway connection still handshaking).
    """

    settings: Settings | None = None
    bot: CineSnipBot | None = None
