from __future__ import annotations

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
    def __init__(self, settings: Settings):
        self._server = PlexServer(settings.plex_url, settings.plex_token)
        self._section = self._server.library.section(settings.movies_library_name)

    def search_movies(self, query: str, limit: int = 25) -> list[MovieResult]:
        movies = self._section.search(title=query, libtype="movie")[:limit]
        return [self._to_result(m) for m in movies]

    def get_movie(self, rating_key: int) -> MovieResult:
        try:
            movie = self._server.fetchItem(rating_key)
        except NotFound as exc:
            raise MovieNotFoundError(rating_key) from exc
        return self._to_result(movie)

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
