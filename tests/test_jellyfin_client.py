import httpx
import pytest

from app.settings import LibraryConfig, Settings
from app.worker.jellyfin_client import JellyfinClient
from app.worker.media_client import MovieNotFoundError, ShowNotFoundError


def _settings(libraries=None) -> Settings:
    return Settings(
        discord_token="t",
        jellyfin_url="http://jf.test",
        jellyfin_api_key="key123",
        media_server="jellyfin",
        libraries=libraries or [LibraryConfig(name="Movies")],
    )


def _folder(name: str, collection_type: str, locations: list[str], item_id: str = "f1") -> dict:
    return {
        "Name": name,
        "ItemId": item_id,
        "CollectionType": collection_type,
        "Locations": locations,
    }


def _client_with_mock(handler, movie_folders=None, show_folders=None) -> JellyfinClient:
    client = JellyfinClient.__new__(JellyfinClient)
    client._base_url = "http://jf.test"
    client._api_key = "key123"
    client._http = httpx.Client(
        base_url="http://jf.test",
        headers={"X-Emby-Token": "key123"},
        transport=httpx.MockTransport(handler),
    )
    client._user_id = "user-1"
    client._movie_folders = movie_folders if movie_folders is not None else [
        _folder("Movies", "movies", ["/media/movies"])
    ]
    client._show_folders = show_folders if show_folders is not None else [
        _folder("TV Shows", "tvshows", ["/media/tv"], item_id="f2")
    ]
    client.movie_library_names = frozenset(f["Name"] for f in client._movie_folders)
    client.show_library_names = frozenset(f["Name"] for f in client._show_folders)
    client._folder_locations = [
        (f["Name"], list(f.get("Locations") or []))
        for f in client._movie_folders + client._show_folders
    ]
    return client


def test_get_movie_returns_media_result_with_string_id():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/Users/user-1/Items/abc-123"
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
        assert request.url.params["ParentId"] == "f1"
        # Real Jellyfin servers omit MediaSources from list endpoints unless
        # explicitly requested — confirmed against a live instance during
        # manual verification (issue #24). Without this, source_path is
        # always "" and every render fails.
        assert request.url.params["Fields"] == "MediaSources"
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


def test_get_movie_attributes_library_name_from_its_own_path():
    # library_name is what settings.path_mappings_for() keys off — a movie
    # has no SeriesName, so it has to come from the configured folder whose
    # Locations its file lives under, not from the item payload.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "Id": "abc-123",
                "Name": "Film",
                "ProductionYear": 2020,
                "RunTimeTicks": 0,
                "MediaSources": [{"Path": "/media/movies4k/film.mkv"}],
                "Type": "Movie",
            },
        )

    client = _client_with_mock(
        handler,
        movie_folders=[
            _folder("Movies", "movies", ["/media/movies"]),
            _folder("Movies 4K", "movies", ["/media/movies4k"], item_id="f3"),
        ],
    )

    assert client.get_movie("abc-123").library_name == "Movies 4K"


def test_get_movie_outside_every_configured_folder_has_empty_library_name():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "Id": "abc-123",
                "Name": "Film",
                "RunTimeTicks": 0,
                "MediaSources": [{"Path": "/somewhere/else/film.mkv"}],
                "Type": "Movie",
            },
        )

    client = _client_with_mock(handler)

    assert client.get_movie("abc-123").library_name == ""


def test_enumerate_section_uses_the_folder_name_it_was_given():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["Fields"] == "MediaSources"
        return httpx.Response(
            200,
            json={
                "Items": [
                    {
                        "Id": "m1",
                        "Name": "Film",
                        "RunTimeTicks": 0,
                        "MediaSources": [{"Path": "/media/movies/film.mkv"}],
                        "Type": "Movie",
                    }
                ]
            },
        )

    client = _client_with_mock(handler)
    section = _folder("Movies", "movies", ["/media/movies"])

    assert client.enumerate_section(section)[0].library_name == "Movies"


def test_episode_library_name_is_the_configured_library_not_the_series():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["Fields"] == "MediaSources"
        return httpx.Response(
            200,
            json={
                "Items": [
                    {
                        "Id": "e1",
                        "Name": "Pilot",
                        "SeriesName": "Some Show",
                        "ParentIndexNumber": 1,
                        "IndexNumber": 1,
                        "RunTimeTicks": 0,
                        "MediaSources": [{"Path": "/media/tv/Some Show/S01E01.mkv"}],
                        "Type": "Episode",
                    }
                ]
            },
        )

    client = _client_with_mock(handler)
    episode = client.get_episode("show-1", 1, 1)

    assert episode.library_name == "TV Shows"
    assert "Some Show" in episode.title


def test_get_episode_404_on_the_show_itself_raises_show_not_found():
    # A 404 here means the *show* doesn't exist — distinct from the show
    # existing but not having that season/episode, which is a plain
    # fallthrough after a 200 with no matching item (not tested here).
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={})

    client = _client_with_mock(handler)

    with pytest.raises(ShowNotFoundError):
        client.get_episode("missing-show", 1, 1)


def test_search_shows_scopes_to_configured_show_folders():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.params["ParentId"])
        return httpx.Response(
            200,
            json={"Items": [{"Id": "s1", "Name": "Some Show", "RunTimeTicks": 0, "Type": "Series"}]},
        )

    client = _client_with_mock(handler)
    results = client.search_shows("some")

    assert seen == ["f2"]
    assert results[0].library_name == "TV Shows"


def test_current_section_updated_ats_not_implemented():
    client = _client_with_mock(lambda request: httpx.Response(200, json={}))

    with pytest.raises(NotImplementedError):
        client.current_section_updated_ats()
