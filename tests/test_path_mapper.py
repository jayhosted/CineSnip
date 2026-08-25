import pytest

from app.settings import PathMapping
from app.worker.path_mapper import NoPathMappingError, resolve_container_path


def test_resolves_windows_style_path_on_first_drive():
    mappings = [
        PathMapping(
            plex_prefix="D:\\Plex Additional\\Movies", container_path="/media/movies-d"
        ),
        PathMapping(
            plex_prefix="E:\\Media\\Video\\Movies", container_path="/media/movies-e"
        ),
    ]
    plex_path = "D:\\Plex Additional\\Movies\\1917 (2019)\\1917.mkv"

    result = resolve_container_path(plex_path, mappings)

    assert result == "/media/movies-d/1917 (2019)/1917.mkv"


def test_resolves_windows_style_path_on_second_drive():
    mappings = [
        PathMapping(
            plex_prefix="D:\\Plex Additional\\Movies", container_path="/media/movies-d"
        ),
        PathMapping(
            plex_prefix="E:\\Media\\Video\\Movies", container_path="/media/movies-e"
        ),
    ]
    plex_path = "E:\\Media\\Video\\Movies\\12 Angry Men (1957)\\12 Angry Men.mkv"

    result = resolve_container_path(plex_path, mappings)

    assert result == "/media/movies-e/12 Angry Men (1957)/12 Angry Men.mkv"


def test_longest_prefix_wins_when_overlapping():
    mappings = [
        PathMapping(plex_prefix="D:\\Media", container_path="/media/generic"),
        PathMapping(
            plex_prefix="D:\\Media\\Movies", container_path="/media/movies-specific"
        ),
    ]
    plex_path = "D:\\Media\\Movies\\Film.mkv"

    result = resolve_container_path(plex_path, mappings)

    assert result == "/media/movies-specific/Film.mkv"


def test_raises_when_no_mapping_matches():
    mappings = [
        PathMapping(
            plex_prefix="D:\\Plex Additional\\Movies", container_path="/media/movies-d"
        ),
    ]

    with pytest.raises(NoPathMappingError):
        resolve_container_path("Z:\\Somewhere\\Else\\film.mkv", mappings)
