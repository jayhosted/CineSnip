from __future__ import annotations

from datetime import datetime, timezone

import httpx

from app.settings import Settings
from app.worker.media_client import (
    EpisodeNotFoundError,
    MovieNotFoundError,
    MovieResult,
    ShowNotFoundError,
)


def _normalize_path(path: str) -> str:
    # rstrip("/"): a Locations entry and the Path an ancestor node reports
    # for the same physical folder are identical on every server observed
    # live (neither carries a trailing slash) — but nothing guarantees that
    # indefinitely, and a mismatch here must not silently reject an
    # otherwise legitimately configured library (see
    # _configured_library_for()).
    return path.replace("\\", "/").rstrip("/").lower()


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
        # Every library's on-disk root(s), keyed by that exact path, for
        # every library on the server — deliberately NOT filtered to
        # configured_names. Confirmed against a live Jellyfin server: a
        # library's own ItemId (from /Library/VirtualFolders) never appears
        # as an ancestor of any item — /Items/{id}/Ancestors walks plain
        # filesystem Folder/Series/Season nodes, terminating at a Folder
        # whose own Path is exactly one of a library's Locations entries.
        # Matching against every library's Locations, not just configured
        # ones, is what lets _configured_library_for() tell an unconfigured
        # library nested under a configured one's path apart from a
        # genuinely nested subfolder inside a configured library — an
        # unconfigured library has its own Locations entry, which shows up
        # as a *closer* ancestor match than the configured library's own,
        # further-out one (pre-publication security audit finding).
        self._library_name_by_location: dict[str, str] = {
            _normalize_path(location): f["Name"]
            for f in folders
            for location in (f.get("Locations") or [])
        }

    def _configured_library_for(self, item_id: str, allowed_names: frozenset[str]) -> str | None:
        """Authoritative "does this item genuinely belong to one of
        allowed_names?" check, via Jellyfin's own filesystem ancestor chain
        (/Items/{id}/Ancestors) — deliberately not a prefix match against
        the item's own reported source_path (which can be empty/missing,
        e.g. when MediaSources wasn't requested for this call).

        Walks every ancestor, matching each one's own Path against every
        library's Locations on the server (self._library_name_by_location,
        built in __init__) — not just configured ones — and keeps the
        match with the longest (most specific) Path. That's what makes an
        unconfigured "Kids Movies" library nested at /media/movies/kids
        lose to nothing: it has its own Locations entry, so it's the
        closer/more specific ancestor match for anything under it, and
        since it isn't in allowed_names the item is correctly rejected —
        even though a *further-out* ancestor also matches the configured
        "Movies" library at /media/movies. A genuine subfolder with no
        library of its own at that path never produces a match there, so
        the walk still finds the real (configured) owning library further
        out. Returns None both when the item doesn't exist and when it
        exists but isn't under any allowed library — deliberately
        indistinguishable, so a caller can't tell an out-of-scope item from
        a nonexistent one.
        """
        response = self._http.get(f"/Items/{item_id}/Ancestors")
        if response.status_code == 404:
            return None
        response.raise_for_status()

        best_name: str | None = None
        best_length = -1
        for ancestor in response.json():
            path = ancestor.get("Path")
            if not path:
                continue
            name = self._library_name_by_location.get(_normalize_path(path))
            if name is not None and len(path) > best_length:
                best_name, best_length = name, len(path)

        if best_name is None or best_name not in allowed_names:
            return None
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
        # Jellyfin has no single field mirroring Plex's per-section
        # updatedAt (confirmed against a live server: CollectionFolder's own
        # DateLastMediaAdded is left at Jellyfin's zero-value sentinel,
        # "0001-01-01T00:00:00Z", on a real populated library — not a
        # trustworthy signal). Built from two cheap per-folder calls instead:
        # the newest item's DateCreated (catches additions/replacements) and
        # the folder's total item count (catches removals, which don't
        # change any existing item's DateCreated). Combined into one opaque
        # int — nothing outside this method interprets its structure, same
        # contract as Plex's own timestamp-shaped value.
        result: dict[str, int] = {}
        for name, section in self.library_sections():
            result[name] = self._section_version(section)
        return result

    def _section_version(self, section: dict) -> int:
        item_type = "Movie" if section.get("CollectionType") == "movies" else "Episode"
        params = {
            "ParentId": section["ItemId"],
            "IncludeItemTypes": item_type,
            "Recursive": "true",
            "Fields": "DateCreated",
            "SortBy": "DateCreated",
            "SortOrder": "Descending",
            "Limit": 1,
            "EnableTotalRecordCount": "true",
        }
        response = self._http.get(f"/Users/{self._user_id}/Items", params=params)
        response.raise_for_status()
        payload = response.json()

        items = payload.get("Items") or []
        if items and items[0].get("DateCreated"):
            newest = datetime.fromisoformat(items[0]["DateCreated"])
        else:
            newest = datetime.fromtimestamp(0, tz=timezone.utc)
        count = payload.get("TotalRecordCount") or 0

        return int(newest.timestamp()) * 10_000_000 + (count % 10_000_000)

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
        # Explicit, intentional scope check — same security role as
        # PlexClient.get_movie()'s librarySectionTitle check: a media_id is
        # opaque, unvalidated input from Discord/the web app, so without
        # this a hand-typed id could resolve metadata/render for ANY
        # library on the Jellyfin server, not just ones the admin
        # configured CineSnip to expose.
        library_name = self._configured_library_for(media_id, self.movie_library_names)
        if library_name is None:
            raise MovieNotFoundError(media_id)
        return self._to_result(response.json(), library_name=library_name)

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
        # Same explicit scope check as get_movie(), against the *show's*
        # id — every one of its episodes lives under the same library, same
        # as Plex's get_episode() checking show.librarySectionTitle once
        # rather than re-checking per episode.
        library_name = self._configured_library_for(show_media_id, self.show_library_names)
        if library_name is None:
            raise EpisodeNotFoundError(show_media_id, season, episode)
        for item in response.json().get("Items", []):
            if item.get("ParentIndexNumber") == season and item.get("IndexNumber") == episode:
                return self._to_result(item, library_name=library_name)
        raise EpisodeNotFoundError(show_media_id, season, episode)

    def list_episodes(self, show_media_id: str) -> list[MovieResult]:
        response = self._http.get(
            f"/Shows/{show_media_id}/Episodes", params={"Fields": "MediaSources"}
        )
        if response.status_code == 404:
            raise ShowNotFoundError(show_media_id)
        response.raise_for_status()
        library_name = self._configured_library_for(show_media_id, self.show_library_names)
        if library_name is None:
            raise ShowNotFoundError(show_media_id)
        return [
            self._to_result(item, library_name=library_name)
            for item in response.json().get("Items", [])
        ]

    def _to_result(self, item: dict, library_name: str) -> MovieResult:
        # `.get(key, default)` only fires its default when the key is
        # MISSING — Jellyfin returns these four as explicit JSON null under
        # ordinary conditions (an unassigned-episode special, an item on
        # unmounted storage, an item ffprobe hasn't reached yet, and every
        # Series item for RunTimeTicks, since a show has no single runtime),
        # so `item.get("RunTimeTicks", 0)` returns None, not 0, and crashes
        # downstream arithmetic/formatting. `or default` catches null too.
        media_sources = item.get("MediaSources") or [{}]
        source_path = media_sources[0].get("Path") or ""
        # Same precedent as PlexClient: plexapi's own thumbUrl embeds the
        # token as a query param (includeToken=True) rather than requiring a
        # header, so a bare <img src> can load it — this isn't a new class of
        # risk, it's matching what this codebase already ships for Plex.
        # Only set when the item actually has its own primary image (mirrors
        # PlexClient's `if getattr(item, "thumb", None)` guard) — a bare
        # ImageTags-less item has nothing to point at.
        thumb_url = (
            f"{self._base_url}/Items/{item['Id']}/Images/Primary?api_key={self._api_key}"
            if item.get("ImageTags", {}).get("Primary")
            else None
        )

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
            # PlexClient reports librarySectionTitle here. Always passed in
            # explicitly by the caller now — enumerate_section/_search_folders
            # already know their folder, and get_movie/get_episode/
            # list_episodes derive it authoritatively via
            # _configured_library_for() (Jellyfin's own ancestor tree), not
            # a path-prefix guess.
            library_name=library_name,
        )
