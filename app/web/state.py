from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from app.settings import LibraryConfig, PathMapping


@dataclass
class LibraryChoice:
    # One row in the step-3 checklist: a Plex library section the user can
    # opt into, plus the path-mapping guess we're offering them a chance to
    # confirm/correct before it's written to config.yaml. suggested_plex_prefix
    # is the library ROOT on the Plex side (e.g. "D:\Plex Additional\Movies"),
    # derived from where the sample file was actually found under the
    # suggested container mount — not just the sample file's own parent
    # folder, which would only be correct for that one title.
    name: str
    section_type: str  # "movie" | "show"
    sample_plex_path: str | None
    suggested_container_path: str | None
    suggested_plex_prefix: str | None = None
    selected: bool = False
    container_path: str = ""


@dataclass
class WizardState:
    # Everything accumulated across the wizard's steps, held in memory for
    # the lifetime of the (short-lived, single-admin) wizard process — never
    # written to disk until the final "Finish setup" step, and never logged
    # (Section 14's security requirements: no token echoes anywhere but the
    # config files they belong in).
    discord_token: str | None = None
    discord_username: str | None = None

    plex_pin: object | None = None  # plexapi.myplex.MyPlexPinLogin, once requested
    plex_account_token: str | None = None
    plex_url: str | None = None
    plex_server_name: str | None = None

    library_choices: list[LibraryChoice] = field(default_factory=list)
    last_validation_ok: bool = False

    def selected_libraries(self) -> list[LibraryConfig]:
        return [
            LibraryConfig(
                name=choice.name,
                path_mappings=(
                    [
                        PathMapping(
                            plex_prefix=choice.suggested_plex_prefix or _parent_dir(choice.sample_plex_path),
                            container_path=choice.container_path,
                        )
                    ]
                    if choice.container_path
                    else []
                ),
            )
            for choice in self.library_choices
            if choice.selected
        ]

    @property
    def current_step(self) -> int:
        if not self.discord_username:
            return 1
        if not self.plex_url:
            return 2
        if not any(c.selected for c in self.library_choices):
            return 3
        return 4


def _parent_dir(path: str | None) -> str:
    # Fallback only reached when suggestion couldn't derive a real library
    # root (see wizard.py's _suggest_mapping) and the user picked a
    # container path by hand — this only handles the one sampled file
    # correctly, not necessarily every other title under the library, since
    # there's no way to know the library's actual Plex-side root without a
    # match to derive it from. Handles both Windows and POSIX separators,
    # unlike a plain str.rsplit("/", 1).
    if not path:
        return ""
    normalized = path.replace("\\", "/")
    return normalized.rsplit("/", 1)[0]


def media_mount_candidates(media_root: Path = Path("/media")) -> list[str]:
    # Every directory bind-mounted under /media (docker-compose.yml mounts
    # one read-only folder per media root, Section 8) — the pool of
    # container paths step 3 can suggest a library's path mapping against.
    if not media_root.is_dir():
        return []
    return sorted(str(p) for p in media_root.iterdir() if p.is_dir())
