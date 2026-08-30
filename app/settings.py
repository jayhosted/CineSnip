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
    # 3D encodes pack both eyes into a single frame; this tags how a
    # *library* is encoded (CLAUDE.md Section 3) as a default — per-file
    # packing is auto-detected and can override it. "none" applies no crop.
    three_d_format: Literal["none", "side_by_side", "over_under"] = "none"


class RenderDefaults(BaseModel):
    duration_seconds: float = 4.0
    fps: int = 15
    width: int = 480
    timeout_seconds: float = 60.0
    # Separate from timeout_seconds above: that budgets a single ffmpeg
    # render call, but the gifsicle tier (app/worker/gif_optimize.py) runs
    # its own -O3 pass and then a batch of parallel --lossy=N passes, each
    # independently bounded by this value — reusing timeout_seconds would
    # silently couple two unrelated tuning knobs and let worst-case gifsicle
    # latency roughly double whenever someone raises the ffmpeg timeout.
    gifsicle_timeout_seconds: float = 60.0
    # GIF by default: mp4/webm are smaller but Discord renders them as a
    # real video player (no autoplay/loop, no GIF-picker favoriting) —
    # see CLAUDE.md decision #1. format:mp4/webm remain explicit opt-ins.
    format: Literal["gif", "mp4", "webm"] = "gif"
    # A quote-driven clip uses the matched subtitle line's own start/end
    # instead of duration_seconds (so the clip is exactly that line, no
    # more), but that raw span still needs bounds: a one-word cue is too
    # short to be a usable clip, and a long merged multi-line match
    # shouldn't produce an oversized render. duration_seconds itself is
    # also clamped to this range as a safety net against misconfiguration.
    min_duration_seconds: float = 1.0
    # Raised from 15s to 30s once ClipEditView (issue #5) added Merge/
    # Unmerge — deliberately pulling in several adjacent subtitle lines is
    # now a normal, expected action, and 15s was tight enough to reject a
    # 3-4 line merge outright.
    max_duration_seconds: float = 30.0
    # Empirically confirmed against this project's own dev Discord server
    # (a real, non-boosted guild) by uploading raw test files of known
    # sizes directly via Discord's REST API: 10MB (10,000,000 bytes)
    # succeeded, 10.5MB failed with a 413 (error code 40005). That matches
    # discord.py's Guild.filesize_limit for premium_tier 0 exactly
    # (10,485,760 bytes) — do NOT trust web-search summaries claiming
    # Discord raised this to 20MB "as of August 2026"; that claim was
    # checked here and is wrong (or refers to something other than a
    # regular bot/API channel upload), and briefly shipping with a 19MB
    # threshold based on it caused real Post-to-channel failures. A render
    # exceeding this gets retried at progressively smaller fps/width
    # (ffmpeg.py's _DOWNSCALE_TIERS in api.py) rather than failing
    # outright; raise this only if you've independently confirmed your own
    # server's real upload cap is higher (e.g. via a boost) the same way —
    # don't trust a documented number without testing it against a real
    # upload on your own guild first.
    max_file_size_bytes: int = 10_000_000


class WorkerConfig(BaseModel):
    port: int = 8000


class SubtitleDefaults(BaseModel):
    # ffmpeg's embedded-subtitle extraction has no equivalent of the -ss
    # fast seek used for clip rendering — it reads sequentially through the
    # whole container to demux the subtitle packets, which can take minutes
    # on a large remux over a slow mount even though the subtitle data
    # itself is tiny. 300s gives headroom over a measured ~252s real-world
    # case (a 39GB 2160p HDR remux); raise further for larger files. If
    # you raise this, also raise RESOLVE_QUOTE_TIMEOUT_SECONDS and
    # RENDER_TIMEOUT_SECONDS in app/bot/worker_client.py — both must stay
    # comfortably above whatever this is set to, or the bot's own client
    # times out first with a generic error instead of the worker's clean one.
    extraction_timeout_seconds: float = 300.0


class QuoteMatchDefaults(BaseModel):
    # Top-N candidates the WORKER fetches/ranks per search — deliberately
    # generous and decoupled from how many the bot shows at once. Issue #7:
    # ranking cost is dominated by the FTS5 pre-filter (low hundreds of ms
    # full-corpus, see docs/design/fts5-search-migration.md), not this
    # truncation step, so 50 instead of the old cap of 8 is negligible
    # extra cost. The bot's LibrarySearchView/QuoteMatchView (app/bot/cogs/
    # gif.py) hold the full fetched batch and page through it 8 at a time
    # with Next/Previous buttons — that page size is a bot-side constant
    # (_PAGE_SIZE), not read from this setting.
    fetch_limit: int = 50
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
    # /snip search only (library_search.search_cached_library): how many
    # of a single title's best-scoring lines are even eligible to compete
    # for a results slot. Diversity-first ranking already means a title's
    # 2nd/3rd-best line only ever displaces a *worse* match from another
    # title, never a better one — this just bounds how deep that backfill
    # can reach into one title (and how much matching work happens per
    # title) rather than controlling diversity itself.
    library_per_title_limit: int = 3
    # /search-quote-extend only: max not-yet-cached movie titles a single
    # extending search will extract+cache before stopping and reporting
    # remaining_uncached for a possible follow-up "search N more" call —
    # keeps one request bounded in time regardless of library size.
    library_extend_cap: int = 25


class LibrarySyncDefaults(BaseModel):
    # Off by default — this is background work that can delete cache
    # entries (for titles removed from Plex), so it must be an explicit
    # opt-in rather than something that just starts happening. Section
    # updatedAt-based change detection (see app/worker/library_sync.py) is
    # what makes this safe/cheap enough to offer at all.
    enabled: bool = False
    # The cheap "did anything change" check (a handful of section.reload()
    # calls) runs every wake-up regardless of this value — a short interval
    # costs almost nothing on cycles where nothing changed, since the
    # expensive full enumeration only happens when a section's updatedAt
    # actually moved. 24h matches how infrequently a personal library
    # typically changes; lower it if you add content more often.
    interval_hours: float = 24.0


class Settings(BaseModel):
    discord_token: str
    plex_url: str
    plex_token: str

    libraries: list[LibraryConfig] = Field(default_factory=list)
    render_defaults: RenderDefaults = Field(default_factory=RenderDefaults)
    subtitle_defaults: SubtitleDefaults = Field(default_factory=SubtitleDefaults)
    quote_match: QuoteMatchDefaults = Field(default_factory=QuoteMatchDefaults)
    library_sync: LibrarySyncDefaults = Field(default_factory=LibrarySyncDefaults)
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


def write_config_yaml(settings: Settings, config_path: Path = Path("config.yaml")) -> None:
    """Dumps every config.yaml-owned field of `settings` back to disk —
    the write half of the Settings area (app/web/settings.py): callers
    mutate one field on the in-memory Settings object (already validated
    by having been a real Settings instance) and then call this to persist
    the whole object, so a saved change never has to reason about which
    other sections to leave alone. Secrets (discord_token/plex_url/
    plex_token) live in .env, not here — see app/web/app.py's
    _write_config_files for that half, which this mirrors the shape of.
    """
    config = {
        "libraries": [lib.model_dump() for lib in settings.libraries],
        "render_defaults": settings.render_defaults.model_dump(),
        "subtitle_defaults": settings.subtitle_defaults.model_dump(),
        "quote_match": settings.quote_match.model_dump(),
        "worker": settings.worker.model_dump(),
        "library_sync": settings.library_sync.model_dump(),
    }
    config_path.write_text(yaml.safe_dump(config, sort_keys=False))


def load_settings(
    env_path: Path = Path(".env"),
    config_path: Path = Path("config.yaml"),
    override_env: bool = False,
) -> Settings:
    # override_env=False (default) matches python-dotenv's default: an
    # already-set os.environ key wins over .env, even if its value is an
    # empty string. Normally correct (real env/compose overrides shouldn't
    # be clobbered by a stale .env), but a trap for the wizard's post-setup
    # reload: Docker's `env_file:` has already loaded .env.example's empty
    # placeholders into os.environ before the wizard writes real tokens to
    # disk, so a plain reload would see those keys as "already set" and
    # keep the stale empty values. See app/main.py's post-wizard
    # load_settings(override_env=True) call.
    load_dotenv(env_path, override=override_env)

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
            library_sync=LibrarySyncDefaults(**raw_config.get("library_sync", {})),
            worker=WorkerConfig(**raw_config.get("worker", {})),
            dev_guild_id=dev_guild_id,
        )
    except Exception as exc:
        raise SettingsError(f"Invalid config.yaml: {exc}") from exc
