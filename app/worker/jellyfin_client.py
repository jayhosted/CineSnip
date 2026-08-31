from __future__ import annotations

import httpx

from app.settings import Settings
from app.worker.media_client import (
    EpisodeNotFoundError,
    MovieNotFoundError,
    MovieResult,
    ShowNotFoundError,
)


def _normalize_path(path: str) -> str:
    return path.replace("\\", "/").lower()


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
        # Folder name -> its on-disk roots, used to attribute a single item
        # fetched by ID (get_movie/get_episode/list_episodes) back to the
        # configured library it lives under. Every other call path already
        # knows its folder and passes the name in directly.
        self._folder_locations = [
            (f["Name"], list(f.get("Locations") or []))
            for f in self._movie_folders + self._show_folders
        ]

    def _library_name_for_path(self, source_path: str) -> str:
        """Longest-prefix match of an item's own file path against each
        configured folder's Jellyfin `Locations`, in the same spirit as
        path_mapper.resolve_container_path(). Returns "" when nothing
        matches — an item outside every configured library, which the
        movie_library_names/show_library_names filters in api.py then drop.
        """
        if not source_path:
            return ""
        normalized = _normalize_path(source_path)
        best_name = ""
        best_length = -1
        for name, locations in self._folder_locations:
            for location in locations:
                prefix = _normalize_path(location).rstrip("/")
                if normalized.startswith(prefix + "/") or normalized == prefix:
                    if len(prefix) > best_length:
                        best_name, best_length = name, len(prefix)
        return best_name

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
        return [
            self._to_result(item, library_name=section["Name"])
            for item in response.json().get("Items", [])
        ]

    def current_section_updated_ats(self) -> dict[str, int]:
        # No Jellyfin analog to Plex's per-section updatedAt — library_sync
        # stays Plex-only for this slice (issue #25).
        raise NotImplementedError(
            "library_sync is not supported with media_server: jellyfin (issue #25)."
        )

    def _search_folders(
        self, folders: list[dict], item_type: str, query: str, limit: int
    ) -> list[MovieResult]:
        # Searched one configured folder at a time (ParentId) rather than
        # once across the whole server: it scopes results to the libraries
        # this install actually configured, and — the reason it matters —
        # it's the only way to know which library each hit belongs to.
        # A search response carries no MediaSources path to fall back on.
        results: list[MovieResult] = []
        for folder in folders:
            if len(results) >= limit:
                break
            params = {
                "searchTerm": query,
                "IncludeItemTypes": item_type,
                "Recursive": "true",
                "ParentId": folder["ItemId"],
            }
            response = self._http.get("/Items", params=params)
            for item in response.json().get("Items", []):
                results.append(self._to_result(item, library_name=folder["Name"]))
                if len(results) >= limit:
                    break
        return results[:limit]

    def search_movies(self, query: str, limit: int = 25) -> list[MovieResult]:
        return self._search_folders(self._movie_folders, "Movie", query, limit)

    def search_shows(self, query: str, limit: int = 25) -> list[MovieResult]:
        return self._search_folders(self._show_folders, "Series", query, limit)

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

    def _to_result(self, item: dict, library_name: str | None = None) -> MovieResult:
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
            # The *configured library* this item lives in, never the series
            # title: library_name is what settings.path_mappings_for() and
            # api.py's movie/show library filters key off, exactly as
            # PlexClient reports librarySectionTitle here. Callers that
            # already know their folder pass it in; a single item fetched by
            # ID is attributed from its own file path instead.
            library_name=(
                library_name
                if library_name is not None
                else self._library_name_for_path(source_path)
            ),
        )
