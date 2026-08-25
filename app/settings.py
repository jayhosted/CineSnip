from __future__ import annotations

import os
from pathlib import Path

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field


class PathMapping(BaseModel):
    plex_prefix: str
    container_path: str


class RenderDefaults(BaseModel):
    duration_seconds: float = 4.0
    fps: int = 15
    width: int = 480
    timeout_seconds: float = 60.0


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


class Settings(BaseModel):
    discord_token: str
    plex_url: str
    plex_token: str

    movies_library_name: str = "Movies"
    path_mappings: list[PathMapping] = Field(default_factory=list)
    render_defaults: RenderDefaults = Field(default_factory=RenderDefaults)
    subtitle_defaults: SubtitleDefaults = Field(default_factory=SubtitleDefaults)
    worker: WorkerConfig = Field(default_factory=WorkerConfig)
    scratch_dir: Path = Path("scratch")
    cache_dir: Path = Path("cache")


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
            movies_library_name=raw_config.get("movies_library_name", "Movies"),
            path_mappings=[
                PathMapping(**m) for m in raw_config.get("path_mappings", [])
            ],
            render_defaults=RenderDefaults(**raw_config.get("render_defaults", {})),
            subtitle_defaults=SubtitleDefaults(
                **raw_config.get("subtitle_defaults", {})
            ),
            worker=WorkerConfig(**raw_config.get("worker", {})),
        )
    except Exception as exc:
        raise SettingsError(f"Invalid config.yaml: {exc}") from exc
