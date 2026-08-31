from __future__ import annotations

import httpx

from app.settings import Settings
from app.worker.media_client import (
    EpisodeNotFoundError,
    MovieNotFoundError,
    MovieResult,
    ShowNotFoundError,
)


class JellyfinClient:
    def __init__(self, settings: Settings):
        self._base_url = settings.jellyfin_url
        self._api_key = settings.jellyfin_api_key
        self._http = httpx.Client(
            base_url=self._base_url,
            headers={"X-Emby-Token": self._api_key},
            timeout=30.0,
        )
        # Several endpoints (/Users/{id}/Items) need a userId in the path —
        # resolved once here, same spirit as PlexClient resolving its
        # configured sections once at construction. Single-owner install
        # (CLAUDE.md Section 10), so the first user returned is the one.
        users = self._http.get("/Users").json()
        self._user_id = users[0]["Id"] if users else None

        folders = self._http.get("/Library/VirtualFolders").json()
        configured_names = {lib.name for lib in settings.libraries}
        self._movie_folders = [
            f for f in folders
            if f.get("CollectionType") == "movies" and f["Name"] in configured_names
        ]
        self._show_folders = [
            f for f in folders
            if f.get("CollectionType") == "tvshows" and f["Name"] in configured_names
        ]
        self.movie_library_names = frozenset(f["Name"] for f in self._movie_folders)
        self.show_library_names = frozenset(f["Name"] for f in self._show_folders)

    def library_sections(self) -> list[tuple[str, object]]:
        return [(f["Name"], f) for f in self._movie_folders + self._show_folders]

    def enumerate_section(self, section) -> list[MovieResult]:
        item_type = "Movie" if section.get("CollectionType") == "movies" else "Episode"
        params = {
            "ParentId": section["ItemId"],
            "IncludeItemTypes": item_type,
            "Recursive": "true",
        }
        response = self._http.get(f"/Users/{self._user_id}/Items", params=params)
        return [self._to_result(item) for item in response.json().get("Items", [])]

    def current_section_updated_ats(self) -> dict[str, int]:
        # No Jellyfin analog to Plex's per-section updatedAt — library_sync
        # stays Plex-only for this slice (issue #25).
        raise NotImplementedError(
            "library_sync is not supported with media_server: jellyfin (issue #25)."
        )

    def search_movies(self, query: str, limit: int = 25) -> list[MovieResult]:
        params = {"searchTerm": query, "IncludeItemTypes": "Movie", "Recursive": "true"}
        response = self._http.get("/Items", params=params)
        items = response.json().get("Items", [])[:limit]
        return [self._to_result(item) for item in items]

    def search_shows(self, query: str, limit: int = 25) -> list[MovieResult]:
        params = {"searchTerm": query, "IncludeItemTypes": "Series", "Recursive": "true"}
        response = self._http.get("/Items", params=params)
        items = response.json().get("Items", [])[:limit]
        return [self._to_result(item) for item in items]

    def get_movie(self, media_id: str) -> MovieResult:
        response = self._http.get(f"/Items/{media_id}")
        if response.status_code == 404:
            raise MovieNotFoundError(media_id)
        return self._to_result(response.json())

    def get_episode(self, show_media_id: str, season: int, episode: int) -> MovieResult:
        response = self._http.get(f"/Shows/{show_media_id}/Episodes")
        if response.status_code == 404:
            raise EpisodeNotFoundError(show_media_id, season, episode)
        for item in response.json().get("Items", []):
            if item.get("ParentIndexNumber") == season and item.get("IndexNumber") == episode:
                return self._to_result(item)
        raise EpisodeNotFoundError(show_media_id, season, episode)

    def list_episodes(self, show_media_id: str) -> list[MovieResult]:
        response = self._http.get(f"/Shows/{show_media_id}/Episodes")
        if response.status_code == 404:
            raise ShowNotFoundError(show_media_id)
        return [self._to_result(item) for item in response.json().get("Items", [])]

    @staticmethod
    def _to_result(item: dict) -> MovieResult:
        media_sources = item.get("MediaSources") or [{}]
        source_path = media_sources[0].get("Path", "")
        thumb_url = None  # populated in Task 6 once wired behind api.py's thumb_url usage

        if item.get("Type") == "Episode":
            title = (
                f"{item.get('SeriesName', '')} — "
                f"S{item.get('ParentIndexNumber', 0):02d}E{item.get('IndexNumber', 0):02d} "
                f"— {item.get('Name', '')}"
            )
            year = None
        else:
            title = item.get("Name", "")
            year = item.get("ProductionYear")

        return MovieResult(
            media_id=item["Id"],
            title=title,
            year=year,
            duration_ms=item.get("RunTimeTicks", 0) // 10_000,
            thumb_url=thumb_url,
            source_path=source_path,
            guid=item["Id"],
            library_name=item.get("SeriesName") or "",
        )
