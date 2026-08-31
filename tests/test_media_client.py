from unittest.mock import patch

from app.settings import Settings
from app.worker.media_client import MovieResult, create_media_client


def test_movie_result_has_media_id_and_source_path_not_plex_named_fields():
    result = MovieResult(
        media_id="abc-123",
        title="Film",
        year=2020,
        duration_ms=5000,
        thumb_url=None,
        source_path="/media/movies/film.mkv",
        guid="plex://movie/abc",
        library_name="Movies",
    )
    assert result.media_id == "abc-123"
    assert result.source_path == "/media/movies/film.mkv"
    assert not hasattr(result, "rating_key")
    assert not hasattr(result, "plex_path")


def test_create_media_client_dispatches_to_jellyfin():
    settings = Settings(discord_token="t", media_server="jellyfin", jellyfin_url="http://x", jellyfin_api_key="k")
    with patch("app.worker.jellyfin_client.JellyfinClient.__init__", return_value=None) as mock_init:
        client = create_media_client(settings)
    mock_init.assert_called_once_with(settings)


def test_create_media_client_dispatches_to_plex_by_default():
    settings = Settings(discord_token="t", plex_url="http://x", plex_token="k")
    with patch("app.worker.plex_client.PlexClient.__init__", return_value=None) as mock_init:
        client = create_media_client(settings)
    mock_init.assert_called_once_with(settings)
