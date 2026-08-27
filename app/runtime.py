from __future__ import annotations

from dataclasses import dataclass

from app.settings import Settings


@dataclass
class SettingsHolder:
    """Mutable holder shared between the persistent web app (app/web/app.py)
    and main()'s startup/reload loop. `settings` is None before first-run
    setup completes, then gets replaced in place after a later
    reconfiguration (Section 14/decision #6) — a single shared reference so
    "apply the new config" is one assignment, not a hunt through every
    closure that captured the old Settings by value.
    """

    settings: Settings | None = None
