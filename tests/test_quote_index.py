from app.worker.quote_index import (
    CachedTitle,
    LibraryCoverage,
    SyncLogLine,
    SyncProgress,
    _connect,
    append_sync_log,
    finish_sync_run,
    get_section_updated_at,
    get_sync_progress,
    is_no_subtitle_title,
    library_coverage,
    list_cached_titles,
    list_no_subtitle_guids,
    get_library_item_count,
    reset_stale_running_status,
    set_library_item_count,
    set_section_updated_at,
    start_sync_run,
    tail_sync_log,
    update_sync_progress,
    upsert_no_subtitle_title,
)


def _upsert_cached_title(db_path, guid, media_id, title, library_name, source):
    """Writes a row into the legacy cached_titles table directly — no
    production code writes it anymore (search_index.py replaced it), so
    tests still exercising its readers (list_cached_titles,
    library_coverage) seed it via raw SQL rather than a since-removed
    quote_index writer."""
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT INTO cached_titles (guid, media_id, title, library_name, cached_at, source) "
            "VALUES (?, ?, ?, ?, datetime('now'), ?) "
            "ON CONFLICT(guid) DO UPDATE SET "
            "media_id=excluded.media_id, title=excluded.title, "
            "library_name=excluded.library_name, source=excluded.source",
            (guid, media_id, title, library_name, source),
        )


def test_upsert_and_list_round_trip(tmp_path):
    db_path = tmp_path / "quote_index.db"
    _upsert_cached_title(db_path, "guid-1", "101", "Film One", "Movies", "sidecar")
    _upsert_cached_title(db_path, "guid-2", "102", "Film Two", "3D", "embedded")

    titles = list_cached_titles(db_path)

    assert set(titles) == {
        CachedTitle(guid="guid-1", media_id="101", title="Film One", library_name="Movies", source="sidecar"),
        CachedTitle(guid="guid-2", media_id="102", title="Film Two", library_name="3D", source="embedded"),
    }


def test_upsert_overwrites_existing_guid(tmp_path):
    db_path = tmp_path / "quote_index.db"
    _upsert_cached_title(db_path, "guid-1", "101", "Old Title", "Movies", "sidecar")
    _upsert_cached_title(db_path, "guid-1", "101", "New Title", "Movies", "embedded")

    titles = list_cached_titles(db_path)

    assert len(titles) == 1
    assert titles[0].title == "New Title"
    assert titles[0].source == "embedded"


def test_list_cached_titles_on_missing_db_returns_empty(tmp_path):
    assert list_cached_titles(tmp_path / "does-not-exist.db") == []


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


def test_no_subtitle_title_round_trip(tmp_path):
    db_path = tmp_path / "quote_index.db"
    assert is_no_subtitle_title(db_path, "guid-1") is False

    upsert_no_subtitle_title(db_path, "guid-1", "101", "Film One", "Movies")

    assert is_no_subtitle_title(db_path, "guid-1") is True


def test_no_subtitle_title_upsert_overwrites(tmp_path):
    db_path = tmp_path / "quote_index.db"
    upsert_no_subtitle_title(db_path, "guid-1", "101", "Old Title", "Movies")
    upsert_no_subtitle_title(db_path, "guid-1", "101", "New Title", "Movies")

    # No exception on the second call is the behavior under test here —
    # ON CONFLICT DO UPDATE, not a duplicate-key error.
    assert is_no_subtitle_title(db_path, "guid-1") is True


def test_list_no_subtitle_guids_empty_db(tmp_path):
    db_path = tmp_path / "quote_index.db"
    assert list_no_subtitle_guids(db_path) == set()


def test_list_no_subtitle_guids_returns_full_set(tmp_path):
    db_path = tmp_path / "quote_index.db"
    upsert_no_subtitle_title(db_path, "guid-1", "101", "Film One", "Movies")
    upsert_no_subtitle_title(db_path, "guid-2", "102", "Film Two", "Movies")
    # A cached (searchable) title must not show up here — this set is
    # specifically the negative-case table.
    _upsert_cached_title(db_path, "guid-3", "103", "Film Three", "Movies", "sidecar")

    assert list_no_subtitle_guids(db_path) == {"guid-1", "guid-2"}


def test_library_coverage_counts_by_source_and_no_subtitle(tmp_path):
    db_path = tmp_path / "quote_index.db"
    _upsert_cached_title(db_path, "guid-1", "101", "Film One", "Movies", "sidecar")
    _upsert_cached_title(db_path, "guid-2", "102", "Film Two", "Movies", "sidecar")
    _upsert_cached_title(db_path, "guid-3", "103", "Film Three", "Movies", "embedded")
    upsert_no_subtitle_title(db_path, "guid-4", "104", "Film Four", "Movies")
    _upsert_cached_title(db_path, "guid-5", "105", "Other Library Film", "3D", "sidecar")

    coverage = library_coverage(db_path, "Movies")

    assert coverage == LibraryCoverage(sidecar_count=2, embedded_count=1, no_subtitle_count=1)


def test_library_coverage_on_missing_db_returns_zeros(tmp_path):
    assert library_coverage(tmp_path / "does-not-exist.db", "Movies") == LibraryCoverage(0, 0, 0)


def test_sync_progress_seeded_idle_zero_state(tmp_path):
    db_path = tmp_path / "quote_index.db"
    progress = get_sync_progress(db_path)

    assert progress.status == "idle"
    assert progress.processed == 0
    assert progress.total == 0
    assert progress.last_synced_at is None


def test_start_sync_run_succeeds_when_idle(tmp_path):
    db_path = tmp_path / "quote_index.db"
    assert start_sync_run(db_path) is True
    assert get_sync_progress(db_path).status == "running"


def test_start_sync_run_fails_when_already_running(tmp_path):
    db_path = tmp_path / "quote_index.db"
    assert start_sync_run(db_path) is True
    assert start_sync_run(db_path) is False


def test_update_and_finish_sync_run(tmp_path):
    db_path = tmp_path / "quote_index.db"
    start_sync_run(db_path)
    update_sync_progress(db_path, "Movies", "Some Film", 3, 10)

    mid = get_sync_progress(db_path)
    assert mid.current_library == "Movies"
    assert mid.current_title == "Some Film"
    assert mid.processed == 3
    assert mid.total == 10

    finish_sync_run(db_path, new_count=7)

    done = get_sync_progress(db_path)
    assert done.status == "idle"
    assert done.current_library is None
    assert done.last_run_new_count == 7
    assert done.last_synced_at is not None

    # A subsequent run can start again now that status is back to idle.
    assert start_sync_run(db_path) is True


def test_reset_stale_running_status(tmp_path):
    db_path = tmp_path / "quote_index.db"
    start_sync_run(db_path)
    assert get_sync_progress(db_path).status == "running"

    reset_stale_running_status(db_path)

    assert get_sync_progress(db_path).status == "idle"


def test_reset_stale_running_status_on_missing_db_is_a_noop(tmp_path):
    reset_stale_running_status(tmp_path / "does-not-exist.db")  # must not raise


def test_append_and_tail_sync_log_preserves_order(tmp_path):
    db_path = tmp_path / "quote_index.db"
    append_sync_log(db_path, "first")
    append_sync_log(db_path, "second")
    append_sync_log(db_path, "third")

    lines = tail_sync_log(db_path)

    assert [line.message for line in lines] == ["first", "second", "third"]


def test_append_sync_log_trims_to_50(tmp_path):
    db_path = tmp_path / "quote_index.db"
    for i in range(60):
        append_sync_log(db_path, f"line-{i}")

    lines = tail_sync_log(db_path, limit=100)

    assert len(lines) == 50
    assert lines[0].message == "line-10"
    assert lines[-1].message == "line-59"


def test_tail_sync_log_on_missing_db_returns_empty(tmp_path):
    assert tail_sync_log(tmp_path / "does-not-exist.db") == []


def test_set_and_get_library_item_count_round_trip(tmp_path):
    db_path = tmp_path / "quote_index.db"
    set_library_item_count(db_path, "Movies", 5000)

    assert get_library_item_count(db_path, "Movies") == 5000


def test_set_library_item_count_overwrites_on_second_set(tmp_path):
    db_path = tmp_path / "quote_index.db"
    set_library_item_count(db_path, "Movies", 5000)
    set_library_item_count(db_path, "Movies", 5100)

    assert get_library_item_count(db_path, "Movies") == 5100


def test_get_library_item_count_on_missing_db_returns_none(tmp_path):
    assert get_library_item_count(tmp_path / "does-not-exist.db", "Movies") is None


def test_get_library_item_count_for_unknown_library_returns_none(tmp_path):
    db_path = tmp_path / "quote_index.db"
    set_library_item_count(db_path, "Movies", 5000)

    assert get_library_item_count(db_path, "TV Shows") is None
