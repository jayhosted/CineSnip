from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field


class PathMapping(BaseModel):
    plex_prefix: str
    container_path: str


class LibraryConfig(BaseModel):
    # Must match the Plex library section's title exactly (case-sensitive) —
    # this is what PlexClient uses to look up the section and what a fetched
    # item's librarySectionTitle is compared against.
    name: str
    path_mappings: list[PathMapping] = Field(default_factory=list)
    # 3D encodes pack both eyes into a single frame. This is a property of
    # how a *library* was encoded/organized (CLAUDE.md Section 3), not
    # something to detect per-file — real files in this project's own 3D
    # library use both packings, so a single hardcoded crop doesn't work
    # across a whole library. "none" (default) applies no crop, for any
    # normal flat-video library.
    three_d_format: Literal["none", "side_by_side", "over_under"] = "none"


class RenderDefaults(BaseModel):
    duration_seconds: float = 4.0
    fps: int = 15
    width: int = 480
    timeout_seconds: float = 60.0
    # CLAUDE.md decision #1 (revised): GIF by default. mp4/webm were tried
    # as the default for their much smaller file size, but real Discord
    # testing showed they don't actually behave like a GIF there — no
    # autoplay/loop, a real video player with a play button and volume
    # slider instead, and no "Add to Favorites" GIF-picker entry, which was
    # the whole point. format:mp4/format:webm remain available as explicit
    # opt-ins for anyone who'd rather trade those for a much smaller file.
    format: Literal["gif", "mp4", "webm"] = "gif"
    # A quote-driven clip uses the matched subtitle line's own start/end
    # instead of duration_seconds (so the clip is exactly that line, no
    # more), but that raw span still needs bounds: a one-word cue is too
    # short to be a usable clip, and a long merged multi-line match
    # shouldn't produce an oversized render. duration_seconds itself is
    # also clamped to this range as a safety net against misconfiguration.
    min_duration_seconds: float = 1.0
    max_duration_seconds: float = 15.0


class WorkerConfig(BaseModel):
    port: int = 8000


class SubtitleDefaults(BaseModel):
    # ffmpeg's embedded-subtitle extraction has no equivalent of the -ss
    # fast seek used for clip rendering — it must read sequentially through
    # the whole container to demux the subtitle packets. On a large remux
    # over a slow I/O path (e.g. WSL2-bridged NTFS drives), that can take
    # well over a minute even though it's a tiny amount of actual subtitle
    # data. 180s gives real headroom; raise further if you see timeouts on
    # very large files.
    extraction_timeout_seconds: float = 180.0


class QuoteMatchDefaults(BaseModel):
    # Top-N candidates returned. CLAUDE.md Section 5 originally specced
    # "top 2-3", but real usage showed 3 too tight to browse — 8 still
    # comfortably fits Discord's 25-option select limit alongside the
    # Confirm/Cancel buttons, and match quality drops off fast past the
    # top handful anyway.
    candidate_limit: int = 8
    # Noise floor: rapidfuzz WRatio scores (0-100) below this are dropped
    # entirely rather than surfaced as a low-confidence result.
    min_score: float = 50.0
    # Presentation-only threshold echoed to the bot (which has no access to
    # Settings) so it can label a match "confident" without duplicating
    # config — does not affect which matches the engine returns.
    confident_score: float = 85.0
    # Adjacent subtitle cues are joined into one match candidate (to catch
    # a quote split across two lines) only if the gap between them is at
    # most this many seconds — prevents joining cues across a scene cut.
    max_window_gap_seconds: float = 3.0
    # Number of subtitle cues of surrounding context to include before/after
    # a match in the Discord embed.
    context_lines: int = 1
    # /cinesnip-search only (library_search.search_cached_library): how many
    # of a single title's best-scoring lines are even eligible to compete
    # for a results slot. Diversity-first ranking already means a title's
    # 2nd/3rd-best line only ever displaces a *worse* match from another
    # title, never a better one — this just bounds how deep that backfill
    # can reach into one title (and how much matching work happens per
    # title) rather than controlling diversity itself.
    library_per_title_limit: int = 3


class Settings(BaseModel):
    discord_token: str
    plex_url: str
    plex_token: str

    libraries: list[LibraryConfig] = Field(default_factory=list)
    render_defaults: RenderDefaults = Field(default_factory=RenderDefaults)
    subtitle_defaults: SubtitleDefaults = Field(default_factory=SubtitleDefaults)
    quote_match: QuoteMatchDefaults = Field(default_factory=QuoteMatchDefaults)
    worker: WorkerConfig = Field(default_factory=WorkerConfig)
    scratch_dir: Path = Path("scratch")
    cache_dir: Path = Path("cache")
    dev_guild_id: int | None = None

    @property
    def quote_index_db_path(self) -> Path:
        # Derived, not a config.yaml field — the index is a rebuildable
        # cache-of-a-cache (guid -> rating_key/title/library_name for
        # already-parsed titles), not something an installer needs to
        # configure separately from cache_dir itself.
        return self.cache_dir / "quote_index.db"

    def _library_config_for(self, library_name: str) -> LibraryConfig:
        for library in self.libraries:
            if library.name == library_name:
                return library
        raise SettingsError(
            f"'{library_name}' is not a configured library. Add it under "
            f"libraries in config.yaml."
        )

    def path_mappings_for(self, library_name: str) -> list[PathMapping]:
        return self._library_config_for(library_name).path_mappings

    def three_d_format_for(self, library_name: str) -> str:
        return self._library_config_for(library_name).three_d_format


class SettingsError(RuntimeError):
    pass


def load_settings(
    env_path: Path = Path(".env"),
    config_path: Path = Path("config.yaml"),
) -> Settings:
    load_dotenv(env_path)

    discord_token = os.environ.get("DISCORD_TOKEN", "").strip()
    plex_url = os.environ.get("PLEX_URL", "").strip()
    plex_token = os.environ.get("PLEX_TOKEN", "").strip()
    dev_guild_id_raw = os.environ.get("DEV_GUILD_ID", "").strip()

    missing = [
        name
        for name, value in [
            ("DISCORD_TOKEN", discord_token),
            ("PLEX_URL", plex_url),
            ("PLEX_TOKEN", plex_token),
        ]
        if not value
    ]
    if missing:
        raise SettingsError(
            f"Missing required .env values: {', '.join(missing)}. "
            f"Copy .env.example to .env and fill them in — see README.md."
        )

    dev_guild_id: int | None = None
    if dev_guild_id_raw:
        try:
            dev_guild_id = int(dev_guild_id_raw)
        except ValueError as exc:
            raise SettingsError(
                f"DEV_GUILD_ID must be a numeric Discord server ID, got '{dev_guild_id_raw}'."
            ) from exc

    if not config_path.exists():
        raise SettingsError(
            f"{config_path} not found. Copy config.yaml.example to {config_path} "
            f"and fill in your path mappings — see README.md."
        )

    with config_path.open("r", encoding="utf-8") as f:
        raw_config = yaml.safe_load(f) or {}

    try:
        return Settings(
            discord_token=discord_token,
            plex_url=plex_url,
            plex_token=plex_token,
            libraries=[
                LibraryConfig(**lib) for lib in raw_config.get("libraries", [])
            ],
            render_defaults=RenderDefaults(**raw_config.get("render_defaults", {})),
            subtitle_defaults=SubtitleDefaults(
                **raw_config.get("subtitle_defaults", {})
            ),
            quote_match=QuoteMatchDefaults(**raw_config.get("quote_match", {})),
            worker=WorkerConfig(**raw_config.get("worker", {})),
            dev_guild_id=dev_guild_id,
        )
    except Exception as exc:
        raise SettingsError(f"Invalid config.yaml: {exc}") from exc
