import asyncio

from app.settings import LibraryConfig, PathMapping, Settings
from app.worker import quote_index
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


def _precache(settings: Settings, guid: str, library_name: str = "Movies") -> None:
    # Makes sync_one_title() treat this title as already cached (a bare
    # existence check), so tests exercise the sync/removal orchestration
    # without needing real Plex/ffmpeg access.
    write_cached_subtitles(
        settings.cache_dir,
        SubtitleResult(
            guid=guid,
            source=SubtitleSource.SIDECAR,
            entries=[SubtitleEntry(index=1, start=0.0, end=1.0, text="Hi")],
        ),
    )
    quote_index.upsert_cached_title(settings.quote_index_db_path, guid, 1, "Film", library_name)


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
    # The removed title's cache must still be intact.
    assert quote_index.get_section_updated_at(settings.quote_index_db_path, "Movies") == 100
    assert any(t.guid == "guid-removed" for t in quote_index.list_cached_titles(settings.quote_index_db_path))


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


def test_both_guards_pass_deletes_removed_title_and_updates_state(tmp_path):
    root = tmp_path / "media"
    root.mkdir(exist_ok=True)
    # The still-present item's actual file, so the spot check finds it.
    (root / "still_present.mkv").write_text("video bytes")
    mappings = [PathMapping(plex_prefix="D:\\Movies", container_path=str(root))]
    settings = _settings(tmp_path, mappings=mappings)

    _precache(settings, "guid-removed")
    quote_index.set_section_updated_at(settings.quote_index_db_path, "Movies", 100)

    plex = _FakePlex(
        items=[_item("guid-still-present", 2, plex_path="D:\\Movies\\still_present.mkv")]
    )

    result = asyncio.run(sync_library(settings, plex, "Movies", section=None, updated_at=200))

    assert result.removal_skipped_reason is None
    assert result.removed == 1
    assert quote_index.list_cached_titles(settings.quote_index_db_path) == []
    assert read_cached_subtitles(settings.cache_dir, "guid-removed") is None
    assert quote_index.get_section_updated_at(settings.quote_index_db_path, "Movies") == 200
