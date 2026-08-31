from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.settings import Settings


class MovieNotFoundError(RuntimeError):
    def __init__(self, media_id: str):
        super().__init__(f"No film found with media_id {media_id}.")
        self.media_id = media_id


class ShowNotFoundError(RuntimeError):
    def __init__(self, media_id: str):
        super().__init__(f"No show found with media_id {media_id}.")
        self.media_id = media_id


class EpisodeNotFoundError(RuntimeError):
    def __init__(self, show_media_id: str, season: int, episode: int):
        super().__init__(
            f"No episode S{season:02d}E{episode:02d} found for show "
            f"media_id {show_media_id}."
        )
        self.show_media_id = show_media_id
        self.season = season
        self.episode = episode


@dataclass
class MovieResult:
    media_id: str
    title: str
    year: int | None
    duration_ms: int
    thumb_url: str | None
    source_path: str
    guid: str
    library_name: str


class MediaClient(Protocol):
    movie_library_names: frozenset[str]
    show_library_names: frozenset[str]

    def library_sections(self) -> list[tuple[str, object]]: ...
    def enumerate_section(self, section) -> list[MovieResult]: ...
    def current_section_updated_ats(self) -> dict[str, int]: ...
    def search_movies(self, query: str, limit: int = 25) -> list[MovieResult]: ...
    def search_shows(self, query: str, limit: int = 25) -> list[MovieResult]: ...
    def get_movie(self, media_id: str) -> MovieResult: ...
    def get_episode(self, show_media_id: str, season: int, episode: int) -> MovieResult: ...
    def list_episodes(self, show_media_id: str) -> list[MovieResult]: ...


def create_media_client(settings: Settings) -> MediaClient:
    if settings.media_server == "jellyfin":
        from app.worker.jellyfin_client import JellyfinClient

        return JellyfinClient(settings)
    from app.worker.plex_client import PlexClient

    return PlexClient(settings)
