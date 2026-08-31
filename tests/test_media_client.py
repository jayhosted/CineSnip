import inspect
from unittest.mock import patch

import app.main
import app.runtime
import app.web.dashboard
from app.runtime import SettingsHolder
from app.settings import Settings
from app.worker.api import create_app
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
        create_media_client(settings)
    mock_init.assert_called_once_with(settings)


def test_create_media_client_dispatches_to_plex_by_default():
    settings = Settings(discord_token="t", plex_url="http://x", plex_token="k")
    with patch("app.worker.plex_client.PlexClient.__init__", return_value=None) as mock_init:
        create_media_client(settings)
    mock_init.assert_called_once_with(settings)


# ---- app.state.media wiring (whole-branch review, Finding 1) --------------
#
# create_app() renamed app.state.plex to app.state.media, but app/main.py
# and app/runtime.py were never touched by that rename — every startup then
# died with AttributeError: 'State' object has no attribute 'plex', on Plex
# installs too. These lock the two halves of that wiring together.


def _startup_settings(tmp_path) -> Settings:
    return Settings(
        discord_token="t",
        plex_url="http://x",
        plex_token="k",
        cache_dir=tmp_path / "cache",
        scratch_dir=tmp_path / "scratch",
    )


def test_create_app_exposes_the_media_client_main_reads(tmp_path):
    settings = _startup_settings(tmp_path)
    with patch("app.worker.plex_client.PlexClient.__init__", return_value=None):
        worker_app = create_app(settings)

    # Exactly what app/main.py's loop does after (re)building the worker.
    holder = SettingsHolder(settings=settings)
    holder.media_client = worker_app.state.media

    assert holder.media_client is not None
    assert not hasattr(worker_app.state, "plex")


def test_main_startup_path_never_reads_the_old_state_plex(tmp_path):
    # A source-level check as well as the behavioral one above: main.py's
    # startup sequence isn't callable in a test without live Discord/Plex
    # credentials, so nothing else would catch a re-introduced app.state.plex
    # (or SettingsHolder.plex_client) until a real container start.
    for module in (app.main, app.runtime, app.web.dashboard):
        source = inspect.getsource(module)
        assert "state.plex" not in source, module.__name__
        assert "plex_client" not in source, module.__name__

    assert not hasattr(SettingsHolder(), "plex_client")
    assert hasattr(SettingsHolder(), "media_client")
