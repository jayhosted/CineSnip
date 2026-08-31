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
        users_response = self._http.get("/Users")
        users_response.raise_for_status()
        users = users_response.json()
        self._user_id = users[0]["Id"] if users else None

        folders_response = self._http.get("/Library/VirtualFolders")
        folders_response.raise_for_status()
        folders = folders_response.json()
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
            # Confirmed against a live server: list endpoints omit
            # MediaSources (and so source_path) unless explicitly asked for
            # — unlike a single-item fetch by ID, which includes it by
            # default. Without this, every enumerated item has
            # source_path="" and can never be rendered.
            "Fields": "MediaSources",
        }
        response = self._http.get(f"/Users/{self._user_id}/Items", params=params)
        response.raise_for_status()
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
        # A search response carries no MediaSources path unless explicitly
        # requested (confirmed against a live server — same gap as
        # enumerate_section) — still needed here even though library_name
        # itself comes from the folder, not a source_path fallback, because
        # source_path is what a later /render call actually needs.
        results: list[MovieResult] = []
        for folder in folders:
            if len(results) >= limit:
                break
            params = {
                "searchTerm": query,
                "IncludeItemTypes": item_type,
                "Recursive": "true",
                "ParentId": folder["ItemId"],
                "Fields": "MediaSources",
                # Without this Jellyfin returns every match in the folder,
                # not just what this call can use — wasted transfer/parse
                # for a per-keystroke autocomplete call on a large library.
                # The len(results) >= limit breaks below still cap the
                # total across folders; this just stops one folder alone
                # from over-fetching.
                "Limit": limit,
            }
            response = self._http.get("/Items", params=params)
            response.raise_for_status()
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
        # A bare /Items/{id} (no userId) returns HTTP 400 on real Jellyfin
        # servers — confirmed against a live instance during manual
        # verification (issue #24) — Jellyfin genuinely requires the
        # /Users/{userId}/Items/{itemId} form for a single-item fetch by ID,
        # same as enumerate_section/_search_folders already use.
        response = self._http.get(f"/Users/{self._user_id}/Items/{media_id}")
        if response.status_code == 404:
            raise MovieNotFoundError(media_id)
        response.raise_for_status()
        return self._to_result(response.json())

    def get_episode(self, show_media_id: str, season: int, episode: int) -> MovieResult:
        # Confirmed against a live Jellyfin server: /Shows/{id}/Episodes
        # (unlike a single-item fetch) works fine without a userId prefix —
        # do not "fix" this to match get_movie's /Users/{id}/... form, that
        # 404s here. Fields=MediaSources is still required, same gap as
        # enumerate_section/_search_folders — omitted, every episode's
        # source_path is "".
        response = self._http.get(
            f"/Shows/{show_media_id}/Episodes", params={"Fields": "MediaSources"}
        )
        if response.status_code == 404:
            # The show itself doesn't exist — distinct from "this show
            # exists but has no SxxExx" (the fallthrough below).
            raise ShowNotFoundError(show_media_id)
        response.raise_for_status()
        for item in response.json().get("Items", []):
            if item.get("ParentIndexNumber") == season and item.get("IndexNumber") == episode:
                return self._to_result(item)
        raise EpisodeNotFoundError(show_media_id, season, episode)

    def list_episodes(self, show_media_id: str) -> list[MovieResult]:
        response = self._http.get(
            f"/Shows/{show_media_id}/Episodes", params={"Fields": "MediaSources"}
        )
        if response.status_code == 404:
            raise ShowNotFoundError(show_media_id)
        response.raise_for_status()
        return [self._to_result(item) for item in response.json().get("Items", [])]

    def _to_result(self, item: dict, library_name: str | None = None) -> MovieResult:
        # `.get(key, default)` only fires its default when the key is
        # MISSING — Jellyfin returns these four as explicit JSON null under
        # ordinary conditions (an unassigned-episode special, an item on
        # unmounted storage, an item ffprobe hasn't reached yet, and every
        # Series item for RunTimeTicks, since a show has no single runtime),
        # so `item.get("RunTimeTicks", 0)` returns None, not 0, and crashes
        # downstream arithmetic/formatting. `or default` catches null too.
        media_sources = item.get("MediaSources") or [{}]
        source_path = media_sources[0].get("Path") or ""
        thumb_url = None  # deferred — auth story not yet decided, issue #27

        if item.get("Type") == "Episode":
            season = item.get("ParentIndexNumber") or 0
            episode = item.get("IndexNumber") or 0
            title = f"{item.get('SeriesName', '')} — S{season:02d}E{episode:02d} — {item.get('Name', '')}"
            year = None
        else:
            title = item.get("Name", "")
            year = item.get("ProductionYear")

        return MovieResult(
            media_id=item["Id"],
            title=title,
            year=year,
            duration_ms=(item.get("RunTimeTicks") or 0) // 10_000,
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
