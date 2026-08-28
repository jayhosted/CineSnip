from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from app.settings import LibraryConfig, PathMapping

# A library can span more than one physical drive/folder (Section 3: "add
# one entry per folder that library spans" — this developer's own Movies
# and TV Shows libraries each really do span two drives). The wizard can
# only ever *suggest* however many distinct mappings it found in its
# sample, so the UI shows exactly those rows (never an arbitrary fixed
# count) plus a per-library "+ Add another location" (htmx, appends one row
# server-side and returns just that row's fragment) for anything the
# sampler missed or a library with more locations than were auto-detected.


@dataclass
class MappingRow:
    plex_prefix: str = ""
    container_path: str = ""


@dataclass
class LibraryChoice:
    # One block in the step-3 checklist: a Plex library section the user
    # can opt into, plus the path-mapping guesses we're offering a chance
    # to confirm/correct/extend before they're written to config.yaml.
    # suggested_rows is a frozen snapshot of what auto-discovery actually
    # found — mapping_rows is the live, user-editable copy. Kept separate so
    # an accidentally-deleted suggested row can be brought back with a
    # "Restore suggested" action rather than being gone for good (no other
    # way to reconstruct it short of reconnecting to Plex again).
    name: str
    section_type: str  # "movie" | "show"
    selected: bool = False
    mapping_rows: list[MappingRow] = field(default_factory=lambda: [MappingRow()])
    suggested_rows: list[MappingRow] = field(default_factory=list)
    # "none" | "side_by_side" | "over_under" (CLAUDE.md Section 3) — left at
    # "none" unless the user flags this library as holding 3D encodes.
    # Without it, a 3D file renders as a squished/doubled frame instead of a
    # normal flat clip; there's no way for the wizard to auto-detect this
    # from a sampled title, so it's an explicit opt-in field, not a guess.
    three_d_format: str = "none"

    def missing_suggestions(self) -> list[tuple[int, MappingRow]]:
        # (index into suggested_rows, the row) for every auto-suggested
        # mapping that isn't currently present in mapping_rows — the index
        # is what the "Restore" button needs to identify which suggestion
        # to bring back, since suggested_rows itself never changes.
        return [
            (idx, suggestion)
            for idx, suggestion in enumerate(self.suggested_rows)
            if suggestion not in self.mapping_rows
        ]


@dataclass
class WizardState:
    # Everything accumulated across the wizard's steps, held in memory for
    # the lifetime of the (short-lived, single-admin) wizard process — never
    # written to disk until the final "Finish setup" step, and never logged
    # (Section 14's security requirements: no token echoes anywhere but the
    # config files they belong in).
    discord_token: str | None = None
    discord_username: str | None = None
    discord_bot_id: str | None = None

    plex_pin: object | None = None  # plexapi.myplex.MyPlexPinLogin, once requested
    plex_account_token: str | None = None
    plex_url: str | None = None
    plex_server_name: str | None = None

    library_choices: list[LibraryChoice] = field(default_factory=list)
    last_validation_ok: bool = False

    # Set when a wizard step is entered via a Settings "Edit ___" link
    # (?return_to=<tab>) rather than first-run — lets the wizard panels show
    # a real "back to Settings" path instead of the only way out being the
    # wizard's own internal Back, and lets panel_complete.html send a
    # reconfiguring admin back to the tab they came from instead of the
    # first-run "CineSnip is starting up" screen.
    wizard_return_to: str | None = None

    def selected_libraries(self) -> list[LibraryConfig]:
        return [
            LibraryConfig(
                name=choice.name,
                path_mappings=[
                    PathMapping(plex_prefix=row.plex_prefix, container_path=row.container_path)
                    for row in choice.mapping_rows
                    if row.plex_prefix and row.container_path
                ],
                three_d_format=choice.three_d_format,
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


def media_mount_candidates(media_root: Path = Path("/media")) -> list[str]:
    # Every directory bind-mounted under /media (docker-compose.yml mounts
    # one read-only folder per media root, Section 8) — the pool of
    # container paths step 3 can suggest a library's path mapping against.
    if not media_root.is_dir():
        return []
    return sorted(str(p) for p in media_root.iterdir() if p.is_dir())
