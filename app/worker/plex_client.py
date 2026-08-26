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


@dataclass
class MovieResult:
    rating_key: int
    title: str
    year: int | None
    duration_ms: int
    thumb_url: str | None
    plex_path: str
    guid: str


class PlexClient:
    # A single /cinesnip invocation calls get_movie() up to three times for
    # the same rating_key (/resolve, /resolve-quote, /render), each a real
    # network round-trip to Plex. This TTL only needs to cover the handful
    # of seconds between those calls within one command — long enough to
    # dedupe that, short enough that a retitled/deleted item doesn't linger.
    _CACHE_TTL_SECONDS = 30.0

    def __init__(self, settings: Settings):
        self._server = PlexServer(settings.plex_url, settings.plex_token)
        self._section = self._server.library.section(settings.movies_library_name)
        self._movie_cache: dict[int, tuple[float, MovieResult]] = {}

    def search_movies(self, query: str, limit: int = 25) -> list[MovieResult]:
        movies = self._section.search(title=query, libtype="movie")[:limit]
        return [self._to_result(m) for m in movies]

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

    @staticmethod
    def _to_result(movie) -> MovieResult:
        # First media/part only for MVP — a movie with multiple Plex media
        # versions (e.g. a remux + a mobile version) or multi-part files
        # would need explicit version selection; not handled here.
        part = movie.media[0].parts[0]
        thumb_url = movie.thumbUrl if getattr(movie, "thumb", None) else None
        return MovieResult(
            rating_key=movie.ratingKey,
            title=movie.title,
            year=getattr(movie, "year", None),
            duration_ms=movie.duration or 0,
            thumb_url=thumb_url,
            plex_path=part.file,
            guid=movie.guid,
        )
