import httpx
import pytest

from app.settings import LibraryConfig, Settings
from app.worker.jellyfin_client import JellyfinClient, _normalize_path
from app.worker.media_client import EpisodeNotFoundError, MovieNotFoundError, ShowNotFoundError


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


def _client_with_mock(
    handler, movie_folders=None, show_folders=None, other_folders=None
) -> JellyfinClient:
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
    # Every library on the mock "server" (configured or not) — mirrors
    # __init__ building this from the *unfiltered* /Library/VirtualFolders
    # response, since _configured_library_for()'s containment check needs
    # to see unconfigured libraries' own Locations too.
    all_folders = client._movie_folders + client._show_folders + list(other_folders or [])
    client._library_name_by_location = {
        _normalize_path(location): f["Name"]
        for f in all_folders
        for location in (f.get("Locations") or [])
    }
    return client


def _ancestors_response(*paths: str) -> httpx.Response:
    # Mirrors what a live Jellyfin server's /Items/{id}/Ancestors actually
    # returns: plain filesystem Folder/Series/Season nodes carrying their
    # own Path — never the owning library's own ItemId (confirmed against a
    # live server; see _configured_library_for()'s docstring).
    return httpx.Response(200, json=[{"Path": path} for path in paths])


def test_get_movie_returns_media_result_with_string_id():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/Items/abc-123/Ancestors":
            return _ancestors_response("/media/movies")
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


def test_get_movie_thumb_url_includes_api_key_when_primary_image_present():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/Items/abc-123/Ancestors":
            return _ancestors_response("/media/movies")
        return httpx.Response(
            200,
            json={
                "Id": "abc-123",
                "Name": "Film",
                "ProductionYear": 2020,
                "RunTimeTicks": 50_000_000,
                "MediaSources": [{"Path": "/media/movies/film.mkv"}],
                "Type": "Movie",
                "ImageTags": {"Primary": "sometag"},
            },
        )

    client = _client_with_mock(handler)
    result = client.get_movie("abc-123")

    assert result.thumb_url == "http://jf.test/Items/abc-123/Images/Primary?api_key=key123"


def test_get_movie_thumb_url_is_none_without_primary_image():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/Items/abc-123/Ancestors":
            return _ancestors_response("/media/movies")
        return httpx.Response(
            200,
            json={
                "Id": "abc-123",
                "Name": "Film",
                "ProductionYear": 2020,
                "RunTimeTicks": 50_000_000,
                "MediaSources": [{"Path": "/media/movies/film.mkv"}],
                "Type": "Movie",
                "ImageTags": {},
            },
        )

    client = _client_with_mock(handler)
    result = client.get_movie("abc-123")

    assert result.thumb_url is None


def test_get_movie_not_found_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={})

    client = _client_with_mock(handler)

    with pytest.raises(MovieNotFoundError):
        client.get_movie("missing")


def test_get_movie_raises_for_status_on_non_404_error():
    # A bad API key, a Jellyfin 5xx, or a reverse-proxy 502 must not be
    # silently treated as a success body — that produced a bare KeyError
    # on item["Id"] instead of a clear connection error (ultrareview
    # finding, issue #24).
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "unauthorized"})

    client = _client_with_mock(handler)

    with pytest.raises(httpx.HTTPStatusError):
        client.get_movie("abc-123")


def test_to_result_tolerates_explicit_null_fields():
    # Jellyfin returns these as JSON null under ordinary conditions (an
    # unassigned-episode special, an item ffprobe hasn't reached yet, and
    # every Series item for RunTimeTicks) — dict.get(key, default) only
    # fires its default when the key is MISSING, not when it's present
    # with null, so this used to crash (ultrareview finding, issue #24).
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/Items/series-1/Ancestors":
            return _ancestors_response("/media/movies")
        return httpx.Response(
            200,
            json={
                "Id": "series-1",
                "Name": "Some Show",
                "Type": "Series",
                "RunTimeTicks": None,
                "MediaSources": [{"Path": None}],
            },
        )

    client = _client_with_mock(handler)
    result = client.get_movie("series-1")

    assert result.duration_ms == 0
    assert result.source_path == ""


def test_to_result_tolerates_null_episode_numbers():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/Items/e1/Ancestors":
            return _ancestors_response("/media/movies")
        return httpx.Response(
            200,
            json={
                "Id": "e1",
                "Name": "Untitled Special",
                "SeriesName": "Some Show",
                "Type": "Episode",
                "ParentIndexNumber": None,
                "IndexNumber": None,
                "RunTimeTicks": None,
                "MediaSources": [{}],
            },
        )

    client = _client_with_mock(handler)
    result = client.get_movie("e1")

    assert result.title == "Some Show — S00E00 — Untitled Special"


def test_search_movies_sends_limit_and_filters_by_item_type():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["searchTerm"] == "matrix"
        assert request.url.params["IncludeItemTypes"] == "Movie"
        assert request.url.params["ParentId"] == "f1"
        # Real Jellyfin servers omit MediaSources from list endpoints unless
        # explicitly requested — confirmed against a live instance during
        # manual verification (issue #24). Without this, source_path is
        # always "" and every render fails.
        assert request.url.params["Fields"] == "MediaSources"
        # Without Limit, Jellyfin returns the folder's entire matching set
        # regardless of what the caller asked for — wasted transfer/parse
        # on a per-keystroke autocomplete call (ultrareview finding).
        assert request.url.params["Limit"] == "7"
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
    results = client.search_movies("matrix", limit=7)

    assert len(results) == 1
    assert results[0].media_id == "m1"


# ---- Regression: get_movie()/get_episode()/list_episodes() must scope every
# by-id lookup to CineSnip's own configured libraries, proven via Jellyfin's
# own authoritative parent/child item tree (/Items/{id}/Ancestors) — not
# inferred from the item's own file path, which can misattribute an
# unconfigured library nested under (or overlapping) a configured one's
# Locations (pre-publication security audit finding). --------------------


def test_get_movie_attributes_library_name_via_ancestors_not_path():
    # library_name is what settings.path_mappings_for() keys off. Two movie
    # folders share no path overlap here, so this also proves the ancestor
    # check picks the *correct* one of several configured libraries, not
    # just "some" configured library.
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/Items/abc-123/Ancestors":
            return _ancestors_response("/media/movies4k", "/config/root")
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


def test_get_movie_outside_every_configured_folder_raises_not_found():
    # No configured (or even known) library's Locations match any ancestor
    # here — confirmed against a live server that a genuinely out-of-scope
    # item's ancestor chain just terminates at the server's root folder
    # with nothing in between matching.
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/Items/abc-123/Ancestors":
            return _ancestors_response("/somewhere/else", "/config/root")
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

    with pytest.raises(MovieNotFoundError) as excinfo:
        client.get_movie("abc-123")
    assert "Film" not in str(excinfo.value)


def test_get_movie_rejects_unconfigured_library_nested_under_configured_path():
    # The specific misattribution the path-prefix heuristic was vulnerable
    # to: an unconfigured "Kids Movies" library whose on-disk folder
    # (/media/movies/kids) sits underneath the *configured* "Movies"
    # library's own Locations entry (/media/movies). A path-prefix match
    # alone would misattribute this item to "Movies" and let it render.
    # Confirmed against a live server: the real ancestor chain includes
    # BOTH folders (nearer "kids" first, then "movies" further out) — the
    # fix must prefer the closer, more specific match ("Kids Movies") over
    # the further-out one, not just find "any" configured-library match
    # anywhere in the chain.
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/Items/kid-1/Ancestors":
            return _ancestors_response("/media/movies/kids", "/media/movies", "/config/root")
        return httpx.Response(
            200,
            json={
                "Id": "kid-1",
                "Name": "Secret Kids Film",
                "RunTimeTicks": 0,
                "MediaSources": [{"Path": "/media/movies/kids/film.mkv"}],
                "Type": "Movie",
            },
        )

    client = _client_with_mock(
        handler,
        movie_folders=[_folder("Movies", "movies", ["/media/movies"], item_id="f1")],
        other_folders=[_folder("Kids Movies", "movies", ["/media/movies/kids"], item_id="f-kids")],
    )

    with pytest.raises(MovieNotFoundError) as excinfo:
        client.get_movie("kid-1")
    # No leak of the inaccessible item's title into the exception text.
    assert "Secret Kids Film" not in str(excinfo.value)
    assert "Kids Movies" not in str(excinfo.value)


def test_get_movie_resolves_despite_trailing_slash_mismatch():
    # A library's Locations entry and the Path an ancestor node reports for
    # the same physical folder should always be identical on a real
    # server (confirmed live: neither carries a trailing slash) — but nothing
    # guarantees that indefinitely across Jellyfin versions/configurations,
    # and the exact-match lookup in _configured_library_for() has no
    # tolerance for one side having a trailing slash the other lacks. That
    # must fail *open toward matching*, not silently reject an otherwise
    # legitimately configured library.
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/Items/abc-123/Ancestors":
            return _ancestors_response("/media/movies")
        return httpx.Response(
            200,
            json={
                "Id": "abc-123",
                "Name": "Film",
                "RunTimeTicks": 0,
                "MediaSources": [{"Path": "/media/movies/film.mkv"}],
                "Type": "Movie",
            },
        )

    client = _client_with_mock(
        handler,
        # Locations reported with a trailing slash; the ancestor Path (as
        # confirmed against a live server) never has one.
        movie_folders=[_folder("Movies", "movies", ["/media/movies/"], item_id="f1")],
    )

    assert client.get_movie("abc-123").library_name == "Movies"


def test_get_movie_resolves_across_multiple_locations_on_one_library():
    # "Movies" spanning two physical Locations (confirmed live against a
    # real multi-drive Jellyfin install) — an item under EITHER Location
    # must resolve to the same library, not just the first one.
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/Items/on-e/Ancestors":
            return _ancestors_response("/media/movies-e", "/config/root")
        return httpx.Response(
            200,
            json={
                "Id": "on-e",
                "Name": "Film On E",
                "RunTimeTicks": 0,
                "MediaSources": [{"Path": "/media/movies-e/film.mkv"}],
                "Type": "Movie",
            },
        )

    client = _client_with_mock(
        handler,
        movie_folders=[
            _folder("Movies", "movies", ["/media/movies-d", "/media/movies-e"], item_id="f1")
        ],
    )

    assert client.get_movie("on-e").library_name == "Movies"


def test_get_episode_rejects_unconfigured_show_library_nested_under_configured_path():
    # Mirrors test_get_movie_rejects_unconfigured_library_nested_under_configured_path
    # for the show/episode side — same misattribution class, same fix,
    # dedicated coverage rather than relying on the movie-side test alone
    # to prove the shared _configured_library_for() logic.
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/Items/kids-show/Ancestors":
            return _ancestors_response("/media/tv/kids", "/media/tv", "/config/root")
        return httpx.Response(
            200,
            json={
                "Items": [
                    {
                        "Id": "e1",
                        "Name": "Pilot",
                        "SeriesName": "Kids Show",
                        "ParentIndexNumber": 1,
                        "IndexNumber": 1,
                        "RunTimeTicks": 0,
                        "MediaSources": [{"Path": "/media/tv/kids/Kids Show/S01E01.mkv"}],
                        "Type": "Episode",
                    }
                ]
            },
        )

    client = _client_with_mock(
        handler,
        show_folders=[_folder("TV Shows", "tvshows", ["/media/tv"], item_id="f2")],
        other_folders=[_folder("Kids TV", "tvshows", ["/media/tv/kids"], item_id="f-kids-tv")],
    )

    with pytest.raises(EpisodeNotFoundError) as excinfo:
        client.get_episode("kids-show", 1, 1)
    assert "Kids Show" not in str(excinfo.value)
    assert "Kids TV" not in str(excinfo.value)


def test_get_movie_nested_subfolder_inside_configured_library_still_resolves():
    # A collection/boxset folder inside a genuinely configured library must
    # still resolve — the ancestor chain includes intermediate folders (with
    # no library of their own) in addition to the library root itself.
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/Items/nested-1/Ancestors":
            return _ancestors_response("/media/movies/collection", "/media/movies", "/config/root")
        return httpx.Response(
            200,
            json={
                "Id": "nested-1",
                "Name": "Film",
                "RunTimeTicks": 0,
                "MediaSources": [{"Path": "/media/movies/collection/film.mkv"}],
                "Type": "Movie",
            },
        )

    client = _client_with_mock(
        handler,
        movie_folders=[_folder("Movies", "movies", ["/media/movies"], item_id="f1")],
    )

    assert client.get_movie("nested-1").library_name == "Movies"


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
        if request.url.path == "/Items/show-1/Ancestors":
            return _ancestors_response("/media/tv/Some Show", "/media/tv", "/config/root")
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


def test_get_episode_outside_configured_show_libraries_raises_not_found():
    # An unconfigured, standalone show library (own Locations entry, not
    # nested under the configured one) must not be reachable via a
    # hand-typed show id — the ancestor check runs against the *show's*
    # own item id, not any individual episode's.
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/Items/private-show/Ancestors":
            return _ancestors_response("/media/private-shows/Private Show", "/media/private-shows", "/config/root")
        return httpx.Response(
            200,
            json={
                "Items": [
                    {
                        "Id": "e1",
                        "Name": "Pilot",
                        "SeriesName": "Private Show",
                        "ParentIndexNumber": 1,
                        "IndexNumber": 1,
                        "RunTimeTicks": 0,
                        "MediaSources": [{"Path": "/media/private-shows/Private Show/S01E01.mkv"}],
                        "Type": "Episode",
                    }
                ]
            },
        )

    client = _client_with_mock(
        handler,
        other_folders=[_folder("Private Shows", "tvshows", ["/media/private-shows"], item_id="f-private")],
    )

    with pytest.raises(EpisodeNotFoundError) as excinfo:
        client.get_episode("private-show", 1, 1)
    assert "Private Show" not in str(excinfo.value)
    assert "Pilot" not in str(excinfo.value)


def test_list_episodes_outside_configured_show_libraries_raises_show_not_found():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/Items/private-show/Ancestors":
            return _ancestors_response("/media/private-shows/Private Show", "/media/private-shows", "/config/root")
        return httpx.Response(
            200,
            json={
                "Items": [
                    {
                        "Id": "e1",
                        "Name": "Pilot",
                        "SeriesName": "Private Show",
                        "ParentIndexNumber": 1,
                        "IndexNumber": 1,
                        "RunTimeTicks": 0,
                        "MediaSources": [{"Path": "/media/private-shows/Private Show/S01E01.mkv"}],
                        "Type": "Episode",
                    }
                ]
            },
        )

    client = _client_with_mock(
        handler,
        other_folders=[_folder("Private Shows", "tvshows", ["/media/private-shows"], item_id="f-private")],
    )

    with pytest.raises(ShowNotFoundError):
        client.list_episodes("private-show")


def test_list_episodes_inside_configured_show_library_still_resolves():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/Items/show-1/Ancestors":
            return _ancestors_response("/media/tv/Some Show", "/media/tv", "/config/root")
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
    episodes = client.list_episodes("show-1")

    assert len(episodes) == 1
    assert episodes[0].library_name == "TV Shows"


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


def test_current_section_updated_ats_combines_newest_date_and_count():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["SortBy"] == "DateCreated"
        assert request.url.params["SortOrder"] == "Descending"
        assert request.url.params["Limit"] == "1"
        if request.url.params["ParentId"] == "f1":
            assert request.url.params["IncludeItemTypes"] == "Movie"
            return httpx.Response(
                200,
                json={
                    "Items": [{"DateCreated": "2024-01-01T00:00:00.0000000Z"}],
                    "TotalRecordCount": 42,
                },
            )
        assert request.url.params["IncludeItemTypes"] == "Episode"
        return httpx.Response(200, json={"Items": [], "TotalRecordCount": 0})

    client = _client_with_mock(
        handler,
        movie_folders=[_folder("Movies", "movies", ["/media/movies"], item_id="f1")],
        show_folders=[],
    )

    result = client.current_section_updated_ats()

    import datetime

    expected = int(
        datetime.datetime(2024, 1, 1, tzinfo=datetime.timezone.utc).timestamp()
    ) * 10_000_000 + 42
    assert result == {"Movies": expected}


def test_current_section_updated_ats_handles_empty_library():
    client = _client_with_mock(
        lambda request: httpx.Response(200, json={"Items": [], "TotalRecordCount": 0}),
        movie_folders=[_folder("Movies", "movies", ["/media/movies"], item_id="f1")],
        show_folders=[],
    )

    result = client.current_section_updated_ats()

    assert result == {"Movies": 0}
