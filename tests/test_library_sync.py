import asyncio

from app.settings import LibraryConfig, PathMapping, Settings
from app.worker import quote_index, search_index
from app.worker.library_sync import _mount_check, sync_library
from app.worker.plex_client import MovieResult
from app.worker.subtitles import (
    SubtitleEntry,
    SubtitleResult,
    SubtitleSource,
    read_cached_subtitles,
    write_cached_subtitles,
)


def _settings(tmp_path, library_name="Movies", mappings=None) -> Settings:
    root = tmp_path / "media"
    root.mkdir(exist_ok=True)
    (root / "placeholder.txt").write_text("x")  # non-empty mount by default
    if mappings is None:
        mappings = [PathMapping(plex_prefix="D:\\Movies", container_path=str(root))]
    return Settings(
        discord_token="x",
        plex_url="http://localhost",
        plex_token="x",
        libraries=[LibraryConfig(name=library_name, path_mappings=mappings)],
        cache_dir=tmp_path / "cache",
    )


def _item(guid, rating_key, title="Film", library_name="Movies", plex_path="D:\\Movies\\film.mkv"):
    return MovieResult(
        rating_key=rating_key,
        title=title,
        year=2000,
        duration_ms=1000,
        thumb_url=None,
        plex_path=plex_path,
        guid=guid,
        library_name=library_name,
    )


def _precache(settings: Settings, guid: str, library_name: str = "Movies", rating_key: int = 1) -> None:
    # Makes sync_one_title() treat this title as already indexed (via
    # search_index, the authoritative store), so tests exercise the
    # sync/removal orchestration without needing real Plex/ffmpeg access.
    search_index.upsert_title(
        settings.quote_index_db_path,
        guid,
        rating_key,
        "Film",
        library_name,
        "sidecar",
        None,
        None,
        [SubtitleEntry(index=1, start=0.0, end=1.0, text="Hi")],
        None,
    )


class _FakePlex:
    def __init__(self, items=None, raise_on_enumerate=False):
        self._items = items or []
        self._raise = raise_on_enumerate

    def enumerate_section(self, section):
        if self._raise:
            raise ConnectionError("plex unreachable")
        return self._items


# --- _mount_check (layer 1) -------------------------------------------------


def test_mount_check_passes_for_populated_mount(tmp_path):
    settings = _settings(tmp_path)
    assert _mount_check(settings, "Movies") is True


def test_mount_check_fails_for_missing_mount(tmp_path):
    mappings = [PathMapping(plex_prefix="D:\\Movies", container_path=str(tmp_path / "does-not-exist"))]
    settings = _settings(tmp_path, mappings=mappings)

    assert _mount_check(settings, "Movies") is False


def test_mount_check_fails_for_empty_mount(tmp_path):
    empty_root = tmp_path / "empty"
    empty_root.mkdir()
    mappings = [PathMapping(plex_prefix="D:\\Movies", container_path=str(empty_root))]
    settings = _settings(tmp_path, mappings=mappings)

    assert _mount_check(settings, "Movies") is False


def test_mount_check_fails_if_any_of_several_mappings_is_bad(tmp_path):
    good_root = tmp_path / "good"
    good_root.mkdir()
    (good_root / "f.txt").write_text("x")
    bad_root = tmp_path / "bad"  # never created
    mappings = [
        PathMapping(plex_prefix="D:\\A", container_path=str(good_root)),
        PathMapping(plex_prefix="E:\\B", container_path=str(bad_root)),
    ]
    settings = _settings(tmp_path, mappings=mappings)

    assert _mount_check(settings, "Movies") is False


# --- sync_library orchestration ---------------------------------------------


def test_enumeration_failure_touches_nothing_and_updates_no_state(tmp_path):
    settings = _settings(tmp_path)
    _precache(settings, "guid-1")
    quote_index.set_section_updated_at(settings.quote_index_db_path, "Movies", 100)
    plex = _FakePlex(raise_on_enumerate=True)

    result = asyncio.run(sync_library(settings, plex, "Movies", section=None, updated_at=200))

    assert result.plex_error is True
    assert result.added == 0
    assert result.removed == 0
    # State must NOT be bumped to 200 — the old value (100) needs to still
    # look "changed" next cycle so this gets retried.
    assert quote_index.get_section_updated_at(settings.quote_index_db_path, "Movies") == 100


def test_no_removal_candidates_updates_state_without_safety_checks(tmp_path):
    settings = _settings(tmp_path)
    _precache(settings, "guid-1")
    plex = _FakePlex(items=[_item("guid-1", 1)])  # still present -> no removal candidates

    result = asyncio.run(sync_library(settings, plex, "Movies", section=None, updated_at=200))

    assert result.removed == 0
    assert result.removal_skipped_reason is None
    assert quote_index.get_section_updated_at(settings.quote_index_db_path, "Movies") == 200


def test_sync_library_persists_item_count(tmp_path):
    settings = _settings(tmp_path)
    _precache(settings, "guid-1")
    plex = _FakePlex(items=[_item("guid-1", 1), _item("guid-2", 2)])

    asyncio.run(sync_library(settings, plex, "Movies", section=None, updated_at=200))

    assert quote_index.get_library_item_count(settings.quote_index_db_path, "Movies") == 2


def test_mount_check_failure_blocks_removal_but_not_addition(tmp_path):
    bad_root = tmp_path / "gone"  # never created -> mount check fails
    mappings = [PathMapping(plex_prefix="D:\\Movies", container_path=str(bad_root))]
    settings = _settings(tmp_path, mappings=mappings)
    _precache(settings, "guid-removed")  # cached, but no longer in the live list
    quote_index.set_section_updated_at(settings.quote_index_db_path, "Movies", 100)
    # A new item Plex reports as live — sync_one_title will SKIP it (no path
    # mapping resolves to a real file), which is fine: additions failing
    # safely per-title is unrelated to whether removal should be trusted.
    plex = _FakePlex(items=[_item("guid-new", 2)])

    result = asyncio.run(sync_library(settings, plex, "Movies", section=None, updated_at=200))

    assert result.removal_skipped_reason == "mount_check_failed"
    assert result.removed == 0
    # The removed title's index entry must still be intact.
    assert quote_index.get_section_updated_at(settings.quote_index_db_path, "Movies") == 100
    assert any(t.guid == "guid-removed" for t in search_index.list_titles(settings.quote_index_db_path))


def test_spot_check_failure_blocks_removal(tmp_path):
    root = tmp_path / "media"
    root.mkdir(exist_ok=True)
    (root / "placeholder.txt").write_text("x")
    mappings = [PathMapping(plex_prefix="D:\\Movies", container_path=str(root))]
    settings = _settings(tmp_path, mappings=mappings)
    _precache(settings, "guid-removed")
    quote_index.set_section_updated_at(settings.quote_index_db_path, "Movies", 100)
    # "guid-still-present" is what Plex claims is still there, but its
    # mapped file doesn't actually exist on disk -> spot check must fail.
    plex = _FakePlex(
        items=[_item("guid-still-present", 2, plex_path="D:\\Movies\\missing.mkv")]
    )

    result = asyncio.run(sync_library(settings, plex, "Movies", section=None, updated_at=200))

    assert result.removal_skipped_reason == "spot_check_failed"
    assert result.removed == 0
    assert quote_index.get_section_updated_at(settings.quote_index_db_path, "Movies") == 100


from app.worker.library_sync import sync_one_title
from app.worker.quote_index import is_no_subtitle_title


def test_sync_one_title_records_no_subtitle_titles(tmp_path):
    settings = _settings(tmp_path)
    item = _item("guid-1", 101)

    # No sidecar, no path mapping matches a real file — the extraction path
    # naturally can't find anything and falls through to SubtitleSource.NONE
    # once ffprobe/ffmpeg see a genuinely nonexistent/unmapped file... but
    # to keep this test hermetic (no real ffmpeg/ffprobe process), precache
    # a NONE result directly instead of exercising get_subtitles().
    write_cached_subtitles(settings.cache_dir, SubtitleResult(guid="guid-1", source=SubtitleSource.NONE, entries=[]))

    outcome = asyncio.run(sync_one_title(settings, item))

    assert outcome.startswith("CACHED (backfilled index)")
    assert is_no_subtitle_title(settings.quote_index_db_path, "guid-1") is True
    assert search_index.has_title(settings.quote_index_db_path, "guid-1") is False


def test_sync_one_title_backfills_legacy_cached_title_missing_from_index(tmp_path):
    settings = _settings(tmp_path)
    item = _item("guid-1", 101, title="Film One")

    write_cached_subtitles(
        settings.cache_dir,
        SubtitleResult(
            guid="guid-1", source=SubtitleSource.SIDECAR,
            entries=[SubtitleEntry(index=1, start=0.0, end=1.0, text="Hi")],
        ),
    )
    # Deliberately not writing into search_index — simulates a legacy cache
    # file from before this migration (or before source/no-subtitle
    # tracking existed at all).
    assert search_index.has_title(settings.quote_index_db_path, "guid-1") is False

    outcome = asyncio.run(sync_one_title(settings, item))

    assert outcome.startswith("CACHED (backfilled index)")
    assert search_index.has_title(settings.quote_index_db_path, "guid-1") is True
    source, sidecar_path, stream_index = search_index.get_source_info(
        settings.quote_index_db_path, "guid-1"
    )
    assert source == "sidecar"
    entries = search_index.get_entries(settings.quote_index_db_path, "guid-1")
    assert len(entries) == 1
    assert entries[0].text == "Hi"


def test_sync_one_title_skips_already_indexed_no_subtitle_title(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    item = _item("guid-1", 101)

    write_cached_subtitles(settings.cache_dir, SubtitleResult(guid="guid-1", source=SubtitleSource.NONE, entries=[]))
    quote_index.upsert_no_subtitle_title(settings.quote_index_db_path, "guid-1", 101, "Film", "Movies")

    def _boom(*args, **kwargs):
        raise AssertionError("read_cached_subtitles should not be called for an already-indexed title")
    monkeypatch.setattr("app.worker.library_sync.read_cached_subtitles", _boom)

    outcome = asyncio.run(sync_one_title(settings, item))

    assert outcome.startswith("CACHED (already have it)")


def test_sync_one_title_skips_already_indexed_search_index_title(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    item = _item("guid-1", 101)
    _precache(settings, "guid-1")

    def _boom(*args, **kwargs):
        raise AssertionError("read_cached_subtitles should not be called for an already-indexed title")
    monkeypatch.setattr("app.worker.library_sync.read_cached_subtitles", _boom)

    outcome = asyncio.run(sync_one_title(settings, item))

    assert outcome.startswith("CACHED (already have it)")


from app.worker.quote_index import get_sync_progress


def test_sync_library_writes_progress_per_item(tmp_path):
    settings = _settings(tmp_path)
    plex = _FakePlex(items=[_item("guid-1", 101, title="Film One"), _item("guid-2", 102, title="Film Two")])
    _precache(settings, "guid-1")
    _precache(settings, "guid-2")

    asyncio.run(sync_library(settings, plex, "Movies", section=None, updated_at=200))

    progress = get_sync_progress(settings.quote_index_db_path)
    # sync_library doesn't flip status itself (run_library_sync_once owns
    # that, Step 9 below) — this test only checks the per-item counters
    # landed correctly by the time the loop finished. current_title is
    # cleared once every item is done (issue #15) rather than staying
    # pinned on the last title through the removal/spot-check phase.
    assert progress.processed == 2
    assert progress.total == 2
    assert progress.current_title is None


def test_sync_library_shows_current_title_while_item_still_in_flight(tmp_path, monkeypatch):
    # Regression for issue #15: current_title must reflect the item actually
    # being processed, not the last one that finished — otherwise a slow
    # extraction shows a stale, already-completed title while it runs.
    settings = _settings(tmp_path)
    plex = _FakePlex(items=[_item("guid-1", 101, title="Film One"), _item("guid-2", 102, title="Film Two")])
    _precache(settings, "guid-1")

    seen_mid_flight = {}

    import app.worker.library_sync as library_sync_module

    real_sync_one_title = library_sync_module.sync_one_title

    async def _spy(settings, item, *, force=False):
        if item.title == "Film Two":
            progress = quote_index.get_sync_progress(settings.quote_index_db_path)
            seen_mid_flight["current_title"] = progress.current_title
            seen_mid_flight["processed"] = progress.processed
        return await real_sync_one_title(settings, item, force=force)

    monkeypatch.setattr(library_sync_module, "sync_one_title", _spy)

    asyncio.run(sync_library(settings, plex, "Movies", section=None, updated_at=200))

    assert seen_mid_flight["current_title"] == "Film Two"
    assert seen_mid_flight["processed"] == 1


from app.worker.library_sync import run_library_sync_once
from app.worker.quote_index import start_sync_run


def test_run_library_sync_once_resyncs_library_with_no_persisted_count_even_if_unchanged(tmp_path):
    # Simulates a library that was skipped forever under the old logic:
    # updated_at already matches (would normally short-circuit) but no
    # library_item_counts row exists yet (e.g. from before this stat was
    # tracked, or a library that has simply never changed since sync was
    # enabled). It must still get synced so the count gets populated.
    settings = _settings(tmp_path)
    quote_index.set_section_updated_at(settings.quote_index_db_path, "Movies", 999)
    assert quote_index.get_library_item_count(settings.quote_index_db_path, "Movies") is None

    class _PlexWithSections(_FakePlex):
        def current_section_updated_ats(self):
            return {"Movies": 999}  # unchanged from stored value

        def library_sections(self):
            return [("Movies", object())]

    plex = _PlexWithSections(items=[_item("guid-1", 1), _item("guid-2", 2)])

    results = asyncio.run(run_library_sync_once(settings, plex))

    assert len(results) == 1
    assert quote_index.get_library_item_count(settings.quote_index_db_path, "Movies") == 2


def test_run_library_sync_once_skips_when_already_running(tmp_path):
    settings = _settings(tmp_path)
    plex = _FakePlex()
    start_sync_run(settings.quote_index_db_path)  # simulate a run already in progress

    results = asyncio.run(run_library_sync_once(settings, plex))

    assert results == []


def test_run_library_sync_once_resets_status_to_idle_on_completion(tmp_path):
    settings = _settings(tmp_path)

    class _PlexWithSections(_FakePlex):
        def current_section_updated_ats(self):
            return {"Movies": 999}

        def library_sections(self):
            return [("Movies", object())]

    plex = _PlexWithSections(items=[])
    asyncio.run(run_library_sync_once(settings, plex))

    from app.worker.quote_index import get_sync_progress
    assert get_sync_progress(settings.quote_index_db_path).status == "idle"


def test_run_library_sync_once_resets_status_even_on_plex_error(tmp_path):
    settings = _settings(tmp_path)

    class _RaisingPlex:
        def current_section_updated_ats(self):
            raise ConnectionError("plex unreachable")

    asyncio.run(run_library_sync_once(settings, _RaisingPlex()))

    from app.worker.quote_index import get_sync_progress
    assert get_sync_progress(settings.quote_index_db_path).status == "idle"


def test_both_guards_pass_deletes_removed_title_and_updates_state(tmp_path):
    root = tmp_path / "media"
    root.mkdir(exist_ok=True)
    # The still-present item's actual file, so the spot check finds it.
    (root / "still_present.mkv").write_text("video bytes")
    mappings = [PathMapping(plex_prefix="D:\\Movies", container_path=str(root))]
    settings = _settings(tmp_path, mappings=mappings)

    _precache(settings, "guid-removed")
    # A legacy JSON cache file also happens to exist for this guid (e.g.
    # left over from before this migration) — removal must not touch it,
    # since deleting legacy JSON is explicitly out of scope for this
    # migration.
    write_cached_subtitles(
        settings.cache_dir,
        SubtitleResult(
            guid="guid-removed", source=SubtitleSource.SIDECAR,
            entries=[SubtitleEntry(index=1, start=0.0, end=1.0, text="Hi")],
        ),
    )
    quote_index.set_section_updated_at(settings.quote_index_db_path, "Movies", 100)

    plex = _FakePlex(
        items=[_item("guid-still-present", 2, plex_path="D:\\Movies\\still_present.mkv")]
    )

    result = asyncio.run(sync_library(settings, plex, "Movies", section=None, updated_at=200))

    assert result.removal_skipped_reason is None
    assert result.removed == 1
    assert search_index.list_titles(settings.quote_index_db_path) == []
    # Legacy JSON is left alone, not deleted.
    assert read_cached_subtitles(settings.cache_dir, "guid-removed") is not None
    assert quote_index.get_section_updated_at(settings.quote_index_db_path, "Movies") == 200
