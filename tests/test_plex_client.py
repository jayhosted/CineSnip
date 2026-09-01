from types import SimpleNamespace

import pytest

from app.worker.media_client import EpisodeNotFoundError, MovieNotFoundError, ShowNotFoundError
from app.worker.plex_client import PlexClient


def _bare_client(fetch_item=None, movie_libraries=(), show_libraries=()) -> PlexClient:
    # Bypasses __init__ (which opens a real Plex connection). fetchItem
    # must exist (even though it should never actually be called — int()
    # raises while evaluating the argument, before the call happens) since
    # attribute lookup on self._server.fetchItem resolves before that.
    def _unreachable(*args, **kwargs):
        raise AssertionError("fetchItem should not be called for a non-numeric media_id")

    client = PlexClient.__new__(PlexClient)
    client._server = SimpleNamespace(fetchItem=fetch_item or _unreachable)
    client._movie_cache = {}
    client.movie_library_names = frozenset(movie_libraries)
    client.show_library_names = frozenset(show_libraries)
    client._configured_library_names = frozenset(movie_libraries) | frozenset(show_libraries)
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


# ---- Regression: get_movie()/get_episode()/list_episodes() must scope every
# fetchItem() lookup to CineSnip's own configured libraries — a Plex
# ratingKey is opaque, unvalidated input from Discord/the web app, so
# without this check a hand-typed ratingKey could fetch metadata (including
# the real library name) for ANY library on the same Plex server, not just
# ones the admin configured CineSnip to expose (pre-publication security
# audit finding). ----------------------------------------------------------


def test_get_movie_outside_configured_libraries_raises_not_found():
    movie = _fake_movie(rating_key=42, path="/media/private/secret.mkv")
    movie.librarySectionTitle = "Private Home Videos"  # never configured for CineSnip

    client = _bare_client(fetch_item=lambda key: movie, movie_libraries=["Movies"])

    with pytest.raises(MovieNotFoundError) as excinfo:
        client.get_movie("42")
    # The whole point of scoping the lookup is that a caller (and anything
    # that later echoes this exception's text into a Discord reply, e.g.
    # gif.py's _error_detail) never learns the private library's name.
    assert "Private Home Videos" not in str(excinfo.value)


def test_get_movie_inside_configured_library_still_resolves():
    movie = _fake_movie(rating_key=42, path="/media/movies/film.mkv")
    assert movie.librarySectionTitle == "Movies"

    client = _bare_client(fetch_item=lambda key: movie, movie_libraries=["Movies"])

    result = client.get_movie("42")
    assert result.media_id == "42"
    assert result.library_name == "Movies"


def test_get_movie_on_non_video_item_raises_not_found_not_a_crash():
    # A ratingKey for e.g. a playlist/collection has no .media/.parts —
    # _to_result would previously raise an unguarded AttributeError/
    # IndexError straight out of get_movie() instead of a clean 404.
    not_a_video = SimpleNamespace(librarySectionTitle="Movies")  # no .media at all

    client = _bare_client(fetch_item=lambda key: not_a_video, movie_libraries=["Movies"])

    with pytest.raises(MovieNotFoundError):
        client.get_movie("42")


def test_get_episode_outside_configured_show_libraries_raises_not_found():
    show = SimpleNamespace(librarySectionTitle="Private Shows", episode=lambda **kw: None)

    client = _bare_client(fetch_item=lambda key: show, show_libraries=["TV Shows"])

    with pytest.raises(EpisodeNotFoundError):
        client.get_episode("7", 1, 1)


def test_list_episodes_outside_configured_show_libraries_raises_not_found():
    show = SimpleNamespace(librarySectionTitle="Private Shows", episodes=lambda: [])

    client = _bare_client(fetch_item=lambda key: show, show_libraries=["TV Shows"])

    with pytest.raises(ShowNotFoundError):
        client.list_episodes("7")
