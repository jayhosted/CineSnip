import pytest

from app.settings import PathMapping
from app.worker.path_mapper import NoPathMappingError, resolve_container_path


def test_resolves_windows_style_path_on_first_drive():
    mappings = [
        PathMapping(
            path_prefix="D:\\Movies", container_path="/media/movies-d"
        ),
        PathMapping(
            path_prefix="E:\\Media\\Video\\Movies", container_path="/media/movies-e"
        ),
    ]
    source_path = "D:\\Movies\\1917 (2019)\\1917.mkv"

    result = resolve_container_path(source_path, mappings)

    assert result == "/media/movies-d/1917 (2019)/1917.mkv"


def test_resolves_windows_style_path_on_second_drive():
    mappings = [
        PathMapping(
            path_prefix="D:\\Movies", container_path="/media/movies-d"
        ),
        PathMapping(
            path_prefix="E:\\Media\\Video\\Movies", container_path="/media/movies-e"
        ),
    ]
    source_path = "E:\\Media\\Video\\Movies\\12 Angry Men (1957)\\12 Angry Men.mkv"

    result = resolve_container_path(source_path, mappings)

    assert result == "/media/movies-e/12 Angry Men (1957)/12 Angry Men.mkv"


def test_longest_prefix_wins_when_overlapping():
    mappings = [
        PathMapping(path_prefix="D:\\Media", container_path="/media/generic"),
        PathMapping(
            path_prefix="D:\\Media\\Movies", container_path="/media/movies-specific"
        ),
    ]
    source_path = "D:\\Media\\Movies\\Film.mkv"

    result = resolve_container_path(source_path, mappings)

    assert result == "/media/movies-specific/Film.mkv"


def test_strips_windows_extended_length_path_prefix():
    # Real bug found via the full-library cache build: Plex reports the
    # \\?\ extended-length prefix for titles whose combined path/filename
    # exceeds Windows' 260-char MAX_PATH — e.g. a real Borat file and
    # several multi-episode Avatar: The Last Airbender files, both skipped
    # with "No path mapping configured" despite an otherwise-correct entry.
    mappings = [
        PathMapping(
            path_prefix="D:\\Movies", container_path="/media/movies-d"
        ),
    ]
    source_path = (
        "\\\\?\\D:\\Movies\\Borat - Cultural Learnings of America "
        "for Make Benefit Glorious Nation of Kazakhstan (2006)\\Borat Cultural "
        "Learnings of America for Make Benefit Glorious Nation of Kazakhstan "
        "(2006) {imdb-tt0443453} [Bluray-1080p Proper][DTS 5.1][x264]-NERDHD.mkv"
    )

    result = resolve_container_path(source_path, mappings)

    assert result.startswith("/media/movies-d/Borat")
    assert result.endswith(".mkv")
    assert "?" not in result


def test_strips_windows_extended_length_unc_prefix():
    # \\?\UNC\server\share\... is the extended-length form of a real network
    # UNC path (\\server\share\...), not a local drive — a distinct escape
    # sequence from the drive-letter form above.
    mappings = [
        PathMapping(path_prefix="\\\\nas\\Movies", container_path="/media/movies"),
    ]
    source_path = "\\\\?\\UNC\\nas\\Movies\\Film (2020)\\Film.mkv"

    result = resolve_container_path(source_path, mappings)

    assert result == "/media/movies/Film (2020)/Film.mkv"


def test_raises_when_no_mapping_matches():
    mappings = [
        PathMapping(
            path_prefix="D:\\Movies", container_path="/media/movies-d"
        ),
    ]

    with pytest.raises(NoPathMappingError):
        resolve_container_path("Z:\\Somewhere\\Else\\film.mkv", mappings)
