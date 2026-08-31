import httpx
import pytest

from app.settings import LibraryConfig, Settings
from app.worker.jellyfin_client import JellyfinClient
from app.worker.media_client import MovieNotFoundError


def _settings(libraries=None) -> Settings:
    return Settings(
        discord_token="t",
        jellyfin_url="http://jf.test",
        jellyfin_api_key="key123",
        media_server="jellyfin",
        libraries=libraries or [LibraryConfig(name="Movies")],
    )


def _client_with_mock(handler) -> JellyfinClient:
    client = JellyfinClient.__new__(JellyfinClient)
    client._base_url = "http://jf.test"
    client._api_key = "key123"
    client._http = httpx.Client(
        base_url="http://jf.test",
        headers={"X-Emby-Token": "key123"},
        transport=httpx.MockTransport(handler),
    )
    client._user_id = "user-1"
    return client


def test_get_movie_returns_media_result_with_string_id():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/Items/abc-123"
        assert request.headers["X-Emby-Token"] == "key123"
        return httpx.Response(
            200,
            json={
                "Id": "abc-123",
                "Name": "Film",
                "ProductionYear": 2020,
                "RunTimeTicks": 50_000_000,  # 5s
                "MediaSources": [{"Path": "/media/movies/film.mkv"}],
                "Type": "Movie",
                "ImageTags": {},
            },
        )

    client = _client_with_mock(handler)
    result = client.get_movie("abc-123")

    assert result.media_id == "abc-123"
    assert isinstance(result.media_id, str)
    assert result.source_path == "/media/movies/film.mkv"
    assert result.duration_ms == 5000
    assert result.guid == "abc-123"


def test_get_movie_not_found_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={})

    client = _client_with_mock(handler)

    with pytest.raises(MovieNotFoundError):
        client.get_movie("missing")


def test_search_movies_filters_by_item_type():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["searchTerm"] == "matrix"
        assert request.url.params["IncludeItemTypes"] == "Movie"
        return httpx.Response(
            200,
            json={
                "Items": [
                    {
                        "Id": "m1",
                        "Name": "The Matrix",
                        "ProductionYear": 1999,
                        "RunTimeTicks": 0,
                        "MediaSources": [{"Path": "/media/movies/matrix.mkv"}],
                        "Type": "Movie",
                        "ImageTags": {},
                    }
                ]
            },
        )

    client = _client_with_mock(handler)
    results = client.search_movies("matrix")

    assert len(results) == 1
    assert results[0].media_id == "m1"


def test_current_section_updated_ats_not_implemented():
    client = _client_with_mock(lambda request: httpx.Response(200, json={}))

    with pytest.raises(NotImplementedError):
        client.current_section_updated_ats()
