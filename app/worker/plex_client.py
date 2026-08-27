from __future__ import annotations

import time
from dataclasses import dataclass

from plexapi.exceptions import NotFound
from plexapi.server import PlexServer

from app.settings import Settings


class MovieNotFoundError(RuntimeError):
    def __init__(self, rating_key: int):
        super().__init__(f"No film found with rating_key {rating_key}.")
        self.rating_key = rating_key


class ShowNotFoundError(RuntimeError):
    def __init__(self, rating_key: int):
        super().__init__(f"No show found with rating_key {rating_key}.")
        self.rating_key = rating_key


class EpisodeNotFoundError(RuntimeError):
    def __init__(self, show_rating_key: int, season: int, episode: int):
        super().__init__(
            f"No episode S{season:02d}E{episode:02d} found for show "
            f"rating_key {show_rating_key}."
        )
        self.show_rating_key = show_rating_key
        self.season = season
        self.episode = episode


@dataclass
class MovieResult:
    rating_key: int
    title: str
    year: int | None
    duration_ms: int
    thumb_url: str | None
    plex_path: str
    guid: str
    library_name: str


class PlexClient:
    # A single /snip invocation calls get_movie() up to three times for
    # the same rating_key (/resolve, /resolve-quote, /render), each a real
    # network round-trip to Plex. This TTL only needs to cover the handful
    # of seconds between those calls within one command — long enough to
    # dedupe that, short enough that a retitled/deleted item doesn't linger.
    _CACHE_TTL_SECONDS = 30.0

    def __init__(self, settings: Settings):
        self._server = PlexServer(settings.plex_url, settings.plex_token)
        # Only sections whose type is "movie"/"show" are searched for their
        # respective media types — a configured library still gets its
        # section resolved (its path mappings are stored in settings) even
        # if neither list picks it up.
        sections = [self._server.library.section(lib.name) for lib in settings.libraries]
        self._movie_sections = [s for s in sections if s.type == "movie"]
        self._show_sections = [s for s in sections if s.type == "show"]
        # /search-quote's cache/quote_index.db is shared by movies AND
        # episodes (both flow through the same generic /render and
        # /resolve-quote), but /search-quote itself is documented as
        # movie-only ("every film") — this is what lets it filter the
        # shared index back down to just movie libraries at read time,
        # rather than needing a schema change to tag rows by media type.
        self.movie_library_names = frozenset(s.title for s in self._movie_sections)
        self._movie_cache: dict[int, tuple[float, MovieResult]] = {}

    def library_sections(self) -> list[tuple[str, object]]:
        # (library_name, plexapi LibrarySection) for every configured
        # library — used by library_sync.py instead of reaching into the
        # private _movie_sections/_show_sections directly.
        return [(s.title, s) for s in self._movie_sections + self._show_sections]

    def enumerate_section(self, section) -> list[MovieResult]:
        # Movie sections: a flat list of movies. Show sections: every show's
        # full episode list (season 0 specials included natively by
        # plexapi's show.episodes(), same as list_episodes() above). Shared
        # by scripts/build_full_cache.py and library_sync.py so there's one
        # enumeration implementation, not two.
        if section.type == "movie":
            return [self._to_result(m) for m in section.search(libtype="movie")]
        results: list[MovieResult] = []
        for show in section.search(libtype="show"):
            results.extend(self._to_result(ep) for ep in show.episodes())
        return results

    def current_section_updated_ats(self) -> dict[str, int]:
        # section.reload() is required to get a genuinely live value —
        # confirmed directly that re-fetching via self._server.library
        # .sections()/.section(name) returns the exact same cached Python
        # object already held here (an `is` identity check confirmed no
        # network round-trip happens), so only reload() on the already-held
        # object actually asks Plex again. Lets a connection/timeout
        # exception propagate uncaught — callers must not treat a failed
        # call the same as "got a real value back".
        #
        # plexapi parses updatedAt into a datetime, not the raw epoch int
        # Plex actually returns — converted to an int timestamp here so the
        # rest of the app (SQLite storage, equality comparisons) deals with
        # one plain, storable type rather than a datetime object (which
        # Python 3.12 no longer adapts for sqlite3 automatically).
        result: dict[str, int] = {}
        for name, section in self.library_sections():
            section.reload()
            result[name] = int(section.updatedAt.timestamp())
        return result

    def search_movies(self, query: str, limit: int = 25) -> list[MovieResult]:
        results: list[MovieResult] = []
        for section in self._movie_sections:
            movies = section.search(title=query, libtype="movie")
            results.extend(self._to_result(m) for m in movies)
        return results[:limit]

    def search_shows(self, query: str, limit: int = 25) -> list[MovieResult]:
        results: list[MovieResult] = []
        for section in self._show_sections:
            shows = section.search(title=query, libtype="show")
            results.extend(self._show_to_result(s) for s in shows)
        return results[:limit]

    def get_movie(self, rating_key: int) -> MovieResult:
        cached = self._movie_cache.get(rating_key)
        if cached is not None:
            cached_at, result = cached
            if time.monotonic() - cached_at < self._CACHE_TTL_SECONDS:
                return result

        try:
            movie = self._server.fetchItem(rating_key)
        except NotFound as exc:
            raise MovieNotFoundError(rating_key) from exc

        result = self._to_result(movie)
        self._movie_cache[rating_key] = (time.monotonic(), result)
        return result

    def get_episode(self, show_rating_key: int, season: int, episode: int) -> MovieResult:
        try:
            show = self._server.fetchItem(show_rating_key)
            ep = show.episode(season=season, episode=episode)
        except NotFound as exc:
            raise EpisodeNotFoundError(show_rating_key, season, episode) from exc

        result = self._to_result(ep)
        self._movie_cache[result.rating_key] = (time.monotonic(), result)
        return result

    def list_episodes(self, show_rating_key: int) -> list[MovieResult]:
        try:
            show = self._server.fetchItem(show_rating_key)
            episodes = show.episodes()
        except NotFound as exc:
            raise ShowNotFoundError(show_rating_key) from exc

        results = [self._to_result(ep) for ep in episodes]
        now = time.monotonic()
        for result in results:
            self._movie_cache[result.rating_key] = (now, result)
        return results

    @staticmethod
    def _to_result(item) -> MovieResult:
        # First media/part only for MVP — an item with multiple Plex media
        # versions (e.g. a remux + a mobile version) or multi-part files
        # would need explicit version selection; not handled here.
        part = item.media[0].parts[0]
        thumb_url = item.thumbUrl if getattr(item, "thumb", None) else None

        if getattr(item, "TYPE", None) == "episode":
            # Episodes have no "year" of their own — fold show/season/episode
            # into the title instead, since every downstream consumer
            # (autocomplete labels, the "Searching X's subtitles..." status
            # text, Discord embeds) just displays MovieResult.title as-is and
            # has no separate show/season/episode fields to draw on.
            title = (
                f"{item.grandparentTitle} — S{item.parentIndex:02d}E{item.index:02d} "
                f"— {item.title}"
            )
            year = None
        else:
            title = item.title
            year = getattr(item, "year", None)

        return MovieResult(
            rating_key=item.ratingKey,
            title=title,
            year=year,
            duration_ms=item.duration or 0,
            thumb_url=thumb_url,
            plex_path=part.file,
            guid=item.guid,
            library_name=item.librarySectionTitle,
        )

    @staticmethod
    def _show_to_result(show) -> MovieResult:
        # Used for show_autocomplete only — never fed into /render, so the
        # placeholder duration/path are harmless; only rating_key/title/
        # year/library_name are ever read from this result.
        return MovieResult(
            rating_key=show.ratingKey,
            title=show.title,
            year=getattr(show, "year", None),
            duration_ms=0,
            thumb_url=show.thumbUrl if getattr(show, "thumb", None) else None,
            plex_path="",
            guid=show.guid,
            library_name=show.librarySectionTitle,
        )
