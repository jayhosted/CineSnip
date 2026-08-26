from app.worker.quote_index import CachedTitle, list_cached_titles, upsert_cached_title


def test_upsert_and_list_round_trip(tmp_path):
    db_path = tmp_path / "quote_index.db"
    upsert_cached_title(db_path, "guid-1", 101, "Film One", "Movies")
    upsert_cached_title(db_path, "guid-2", 102, "Film Two", "3D")

    titles = list_cached_titles(db_path)

    assert set(titles) == {
        CachedTitle(guid="guid-1", rating_key=101, title="Film One", library_name="Movies"),
        CachedTitle(guid="guid-2", rating_key=102, title="Film Two", library_name="3D"),
    }


def test_upsert_overwrites_existing_guid(tmp_path):
    db_path = tmp_path / "quote_index.db"
    upsert_cached_title(db_path, "guid-1", 101, "Old Title", "Movies")
    upsert_cached_title(db_path, "guid-1", 101, "New Title", "Movies")

    titles = list_cached_titles(db_path)

    assert len(titles) == 1
    assert titles[0].title == "New Title"


def test_list_cached_titles_on_missing_db_returns_empty(tmp_path):
    assert list_cached_titles(tmp_path / "does-not-exist.db") == []
