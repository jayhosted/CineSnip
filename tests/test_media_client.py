from app.worker.media_client import MovieResult


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
