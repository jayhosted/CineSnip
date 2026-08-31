from types import SimpleNamespace

from app.worker.plex_client import PlexClient


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
