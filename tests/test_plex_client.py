from types import SimpleNamespace

import pytest

from app.worker.media_client import EpisodeNotFoundError, MovieNotFoundError, ShowNotFoundError
from app.worker.plex_client import PlexClient


def _bare_client() -> PlexClient:
    # Bypasses __init__ (which opens a real Plex connection). fetchItem
    # must exist (even though it should never actually be called — int()
    # raises while evaluating the argument, before the call happens) since
    # attribute lookup on self._server.fetchItem resolves before that.
    def _unreachable(*args, **kwargs):
        raise AssertionError("fetchItem should not be called for a non-numeric media_id")

    client = PlexClient.__new__(PlexClient)
    client._server = SimpleNamespace(fetchItem=_unreachable)
    client._movie_cache = {}
    return client


def _fake_part(file_path: str):
    return SimpleNamespace(file=file_path)


def _fake_movie(rating_key=42, title="Film", year=2020, duration=5000, path="/m/f.mkv"):
    return SimpleNamespace(
        ratingKey=rating_key,
        title=title,
        year=year,
        duration=duration,
        thumb="thumb.jpg",
        thumbUrl="http://plex/thumb.jpg",
        guid="plex://movie/abc",
        librarySectionTitle="Movies",
        media=[SimpleNamespace(parts=[_fake_part(path)])],
        TYPE="movie",
    )


def test_to_result_produces_string_media_id_and_source_path():
    movie = _fake_movie(rating_key=42, path="/media/movies/film.mkv")

    result = PlexClient._to_result(movie)

    assert result.media_id == "42"
    assert isinstance(result.media_id, str)
    assert result.source_path == "/media/movies/film.mkv"


def test_to_result_episode_title_format_unaffected_by_rename():
    episode = SimpleNamespace(
        ratingKey=7,
        title="Pilot",
        grandparentTitle="Show",
        parentIndex=1,
        index=1,
        duration=1000,
        thumb=None,
        thumbUrl=None,
        guid="plex://episode/xyz",
        librarySectionTitle="TV Shows",
        media=[SimpleNamespace(parts=[_fake_part("/media/tv/s01e01.mkv")])],
        TYPE="episode",
    )

    result = PlexClient._to_result(episode)

    assert result.title == "Show — S01E01 — Pilot"
    assert result.media_id == "7"


def test_get_movie_with_non_numeric_media_id_raises_not_found():
    # media_id is opaque above this layer (a Jellyfin GUID can't be int()'d
    # either) — a mistyped autocomplete value used to raise an unhandled
    # ValueError straight out of int(), a bare 500 instead of the same
    # clean 404 a stale-but-numeric id already gets (ultrareview finding,
    # issue #24).
    client = _bare_client()

    with pytest.raises(MovieNotFoundError):
        client.get_movie("banana")


def test_get_episode_with_non_numeric_show_media_id_raises_not_found():
    client = _bare_client()

    with pytest.raises(EpisodeNotFoundError):
        client.get_episode("banana", 1, 1)


def test_list_episodes_with_non_numeric_show_media_id_raises_not_found():
    client = _bare_client()

    with pytest.raises(ShowNotFoundError):
        client.list_episodes("banana")
