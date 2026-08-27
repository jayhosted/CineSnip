from app.worker.quote_index import (
    CachedTitle,
    get_section_updated_at,
    list_cached_titles,
    list_cached_titles_for_library,
    remove_cached_title,
    set_section_updated_at,
    upsert_cached_title,
)


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


def test_list_cached_titles_for_library_filters_correctly(tmp_path):
    db_path = tmp_path / "quote_index.db"
    upsert_cached_title(db_path, "guid-1", 101, "Film One", "Movies")
    upsert_cached_title(db_path, "guid-2", 102, "Film Two", "3D")

    titles = list_cached_titles_for_library(db_path, "Movies")

    assert [t.guid for t in titles] == ["guid-1"]


def test_list_cached_titles_for_library_on_missing_db_returns_empty(tmp_path):
    assert list_cached_titles_for_library(tmp_path / "does-not-exist.db", "Movies") == []


def test_remove_cached_title_deletes_the_right_row(tmp_path):
    db_path = tmp_path / "quote_index.db"
    upsert_cached_title(db_path, "guid-1", 101, "Film One", "Movies")
    upsert_cached_title(db_path, "guid-2", 102, "Film Two", "Movies")

    remove_cached_title(db_path, "guid-1")

    assert [t.guid for t in list_cached_titles(db_path)] == ["guid-2"]


def test_remove_cached_title_is_a_noop_for_unknown_guid(tmp_path):
    db_path = tmp_path / "quote_index.db"
    upsert_cached_title(db_path, "guid-1", 101, "Film One", "Movies")

    remove_cached_title(db_path, "guid-never-existed")

    assert [t.guid for t in list_cached_titles(db_path)] == ["guid-1"]


def test_section_updated_at_round_trip(tmp_path):
    db_path = tmp_path / "quote_index.db"
    set_section_updated_at(db_path, "Movies", 12345)

    assert get_section_updated_at(db_path, "Movies") == 12345


def test_section_updated_at_missing_returns_none(tmp_path):
    db_path = tmp_path / "quote_index.db"
    set_section_updated_at(db_path, "Movies", 12345)

    assert get_section_updated_at(db_path, "TV Shows") is None


def test_section_updated_at_on_missing_db_returns_none(tmp_path):
    assert get_section_updated_at(tmp_path / "does-not-exist.db", "Movies") is None


def test_section_updated_at_upsert_overwrites(tmp_path):
    db_path = tmp_path / "quote_index.db"
    set_section_updated_at(db_path, "Movies", 111)
    set_section_updated_at(db_path, "Movies", 222)

    assert get_section_updated_at(db_path, "Movies") == 222
