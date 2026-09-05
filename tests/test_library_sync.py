import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

from app.settings import LibraryConfig, PathMapping, Settings
from app.worker import quote_index, search_index
from app.worker.library_sync import _mount_check, sync_library
from app.worker.plex_client import MovieResult
from app.worker.subtitles import (
    SubtitleEntry,
    SubtitleResult,
    SubtitleSource,
    cache_path_for_guid,
    read_cached_subtitles,
)


def _write_legacy_cache_file(cache_dir, result: SubtitleResult) -> None:
    """Writes a JSON cache file in the pre-FTS5 on-disk format that
    read_cached_subtitles() still reads for backward compatibility — no
    production code writes this format anymore (search_index.py replaced
    it), so tests exercising that legacy-read path write it directly."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "guid": result.guid,
        "source": result.source.value,
        "sidecar_path": result.sidecar_path,
        "stream_index": result.stream_index,
        "cached_at": datetime.now(timezone.utc).isoformat(),
        "source_fingerprint": None,
        "entries": [
            {"index": e.index, "start": e.start, "end": e.end, "text": e.text}
            for e in result.entries
        ],
    }
    cache_path_for_guid(cache_dir, result.guid).write_text(json.dumps(payload), encoding="utf-8")


def _settings(tmp_path, library_name="Movies", mappings=None) -> Settings:
    root = tmp_path / "media"
    root.mkdir(exist_ok=True)
    (root / "placeholder.txt").write_text("x")  # non-empty mount by default
    if mappings is None:
        mappings = [PathMapping(path_prefix="D:\\Movies", container_path=str(root))]
    return Settings(
        discord_token="x",
        plex_url="http://localhost",
        plex_token="x",
        libraries=[LibraryConfig(name=library_name, path_mappings=mappings)],
        cache_dir=tmp_path / "cache",
    )


def _item(guid, media_id, title="Film", library_name="Movies", source_path="D:\\Movies\\film.mkv"):
    return MovieResult(
        media_id=media_id,
        title=title,
        year=2000,
        duration_ms=1000,
        thumb_url=None,
        source_path=source_path,
        guid=guid,
        library_name=library_name,
    )


def _precache(settings: Settings, guid: str, library_name: str = "Movies", media_id: str = "1") -> None:
    # Makes sync_one_title() treat this title as already indexed (via
    # search_index, the authoritative store), so tests exercise the
    # sync/removal orchestration without needing real Plex/ffmpeg access.
    search_index.upsert_title(
        settings.quote_index_db_path,
        guid,
        media_id,
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
    mappings = [PathMapping(path_prefix="D:\\Movies", container_path=str(tmp_path / "does-not-exist"))]
    settings = _settings(tmp_path, mappings=mappings)

    assert _mount_check(settings, "Movies") is False


def test_mount_check_fails_for_empty_mount(tmp_path):
    empty_root = tmp_path / "empty"
    empty_root.mkdir()
    mappings = [PathMapping(path_prefix="D:\\Movies", container_path=str(empty_root))]
    settings = _settings(tmp_path, mappings=mappings)

    assert _mount_check(settings, "Movies") is False


def test_mount_check_fails_if_any_of_several_mappings_is_bad(tmp_path):
    good_root = tmp_path / "good"
    good_root.mkdir()
    (good_root / "f.txt").write_text("x")
    bad_root = tmp_path / "bad"  # never created
    mappings = [
        PathMapping(path_prefix="D:\\A", container_path=str(good_root)),
        PathMapping(path_prefix="E:\\B", container_path=str(bad_root)),
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

    assert result.media_error is True
    assert result.added == 0
    assert result.removed == 0
    # State must NOT be bumped to 200 — the old value (100) needs to still
    # look "changed" next cycle so this gets retried.
    assert quote_index.get_section_updated_at(settings.quote_index_db_path, "Movies") == 100


def test_no_removal_candidates_updates_state_without_safety_checks(tmp_path):
    settings = _settings(tmp_path)
    _precache(settings, "guid-1")
    plex = _FakePlex(items=[_item("guid-1", "1")])  # still present -> no removal candidates

    result = asyncio.run(sync_library(settings, plex, "Movies", section=None, updated_at=200))

    assert result.removed == 0
    assert result.removal_skipped_reason is None
    assert quote_index.get_section_updated_at(settings.quote_index_db_path, "Movies") == 200


def test_sync_library_persists_item_count(tmp_path):
    settings = _settings(tmp_path)
    _precache(settings, "guid-1")
    plex = _FakePlex(items=[_item("guid-1", "1"), _item("guid-2", "2")])

    asyncio.run(sync_library(settings, plex, "Movies", section=None, updated_at=200))

    assert quote_index.get_library_item_count(settings.quote_index_db_path, "Movies") == 2


def test_mount_check_failure_blocks_removal_but_not_addition(tmp_path):
    bad_root = tmp_path / "gone"  # never created -> mount check fails
    mappings = [PathMapping(path_prefix="D:\\Movies", container_path=str(bad_root))]
    settings = _settings(tmp_path, mappings=mappings)
    _precache(settings, "guid-removed")  # cached, but no longer in the live list
    quote_index.set_section_updated_at(settings.quote_index_db_path, "Movies", 100)
    # A new item Plex reports as live — sync_one_title will SKIP it (no path
    # mapping resolves to a real file), which is fine: additions failing
    # safely per-title is unrelated to whether removal should be trusted.
    plex = _FakePlex(items=[_item("guid-new", "2")])

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
    mappings = [PathMapping(path_prefix="D:\\Movies", container_path=str(root))]
    settings = _settings(tmp_path, mappings=mappings)
    _precache(settings, "guid-removed")
    quote_index.set_section_updated_at(settings.quote_index_db_path, "Movies", 100)
    # "guid-still-present" is what Plex claims is still there, but its
    # mapped file doesn't actually exist on disk -> spot check must fail.
    plex = _FakePlex(
        items=[_item("guid-still-present", "2", source_path="D:\\Movies\\missing.mkv")]
    )

    result = asyncio.run(sync_library(settings, plex, "Movies", section=None, updated_at=200))

    assert result.removal_skipped_reason == "spot_check_failed"
    assert result.removed == 0
    assert quote_index.get_section_updated_at(settings.quote_index_db_path, "Movies") == 100


from app.worker.library_sync import sync_one_title
from app.worker.quote_index import is_no_subtitle_title


def test_sync_one_title_records_no_subtitle_titles(tmp_path):
    settings = _settings(tmp_path)
    item = _item("guid-1", "101")

    # No sidecar, no path mapping matches a real file — the extraction path
    # naturally can't find anything and falls through to SubtitleSource.NONE
    # once ffprobe/ffmpeg see a genuinely nonexistent/unmapped file... but
    # to keep this test hermetic (no real ffmpeg/ffprobe process), precache
    # a NONE result directly instead of exercising get_subtitles().
    _write_legacy_cache_file(settings.cache_dir, SubtitleResult(guid="guid-1", source=SubtitleSource.NONE, entries=[]))

    outcome = asyncio.run(sync_one_title(settings, item))

    assert outcome.startswith("CACHED (backfilled index)")
    assert is_no_subtitle_title(settings.quote_index_db_path, "guid-1") is True
    assert search_index.has_title(settings.quote_index_db_path, "guid-1") is False


def test_sync_one_title_backfills_legacy_cached_title_missing_from_index(tmp_path):
    settings = _settings(tmp_path)
    item = _item("guid-1", "101", title="Film One")

    _write_legacy_cache_file(
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
    item = _item("guid-1", "101")

    _write_legacy_cache_file(settings.cache_dir, SubtitleResult(guid="guid-1", source=SubtitleSource.NONE, entries=[]))
    quote_index.upsert_no_subtitle_title(settings.quote_index_db_path, "guid-1", "101", "Film", "Movies")

    def _boom(*args, **kwargs):
        raise AssertionError("read_cached_subtitles should not be called for an already-indexed title")
    monkeypatch.setattr("app.worker.library_sync.read_cached_subtitles", _boom)

    outcome = asyncio.run(sync_one_title(settings, item))

    assert outcome.startswith("CACHED (already have it)")


def test_sync_one_title_known_guid_without_cache_meta_stays_trusted_forever(tmp_path, monkeypatch):
    """Backward compat: a caller that doesn't pass cache_meta at all (e.g.
    scripts/build_full_cache.py's own known_guids-less call, or any future
    caller that hasn't opted in) keeps the original trust-forever behavior
    for a known guid — no freshness recheck, no filesystem access."""
    settings = _settings(tmp_path)
    item = _item("guid-1", "101")

    def _boom(*args, **kwargs):
        raise AssertionError("no filesystem/get_subtitles work should happen without cache_meta")
    monkeypatch.setattr("app.worker.library_sync.get_subtitles", _boom)
    monkeypatch.setattr("app.worker.library_sync.find_sidecar_subtitle", _boom)

    outcome = asyncio.run(
        sync_one_title(
            settings, item, known_guids=frozenset({"guid-1"}), no_subtitle_guids=frozenset()
        )
    )

    assert outcome.startswith("CACHED (already have it)")


def test_sync_one_title_known_guid_with_cache_meta_stays_cached_when_fresh(tmp_path, monkeypatch):
    """The whole point of the cache_meta recheck: an unchanged SIDECAR title
    still resolves as a cheap cache hit, not a full re-extraction."""
    settings = _settings(tmp_path)
    item = _item("guid-1", "101")

    media_root = settings.libraries[0].path_mappings[0].container_path
    (Path(media_root) / "film.mkv").write_bytes(b"x")
    sidecar = Path(media_root) / "film.srt"
    sidecar.write_text("1\n00:00:00,000 --> 00:00:01,000\nHi\n", encoding="utf-8")
    stat = sidecar.stat()

    def _boom(*args, **kwargs):
        raise AssertionError("get_subtitles should not be called for an already-fresh title")
    monkeypatch.setattr("app.worker.library_sync.get_subtitles", _boom)

    outcome = asyncio.run(
        sync_one_title(
            settings, item, known_guids=frozenset({"guid-1"}), no_subtitle_guids=frozenset(),
            cache_meta={"guid-1": ("sidecar", str(sidecar), (stat.st_mtime, stat.st_size))},
        )
    )

    assert outcome.startswith("CACHED (already have it)")


def test_sync_one_title_known_guid_with_cache_meta_reprocesses_when_stale(tmp_path, monkeypatch):
    """The gap this feature closes: a SIDECAR title's subtitle file changed
    (e.g. Bazarr re-fetching a corrected .srt) since it was cached — a
    known_guids-only caller would trust it forever; with cache_meta, the
    scheduled sync itself now catches this instead of waiting for some
    other flow to touch this exact title again."""
    settings = _settings(tmp_path)
    item = _item("guid-1", "101")

    media_root = settings.libraries[0].path_mappings[0].container_path
    (Path(media_root) / "film.mkv").write_bytes(b"x")
    sidecar = Path(media_root) / "film.srt"
    sidecar.write_text("1\n00:00:00,000 --> 00:00:01,000\nHi\n", encoding="utf-8")

    found_entries = [SubtitleEntry(index=1, start=0.0, end=1.0, text="Edited")]

    async def _fake_get_subtitles(movie, container_video_path, cache_dir, db_path, ffprobe_timeout=180.0, ffmpeg_timeout=180.0):
        search_index.upsert_title(
            db_path, movie.guid, movie.media_id, movie.title, movie.library_name,
            "sidecar", container_video_path, None, found_entries, None,
        )
        return SubtitleResult(guid=movie.guid, source=SubtitleSource.SIDECAR, entries=found_entries)

    monkeypatch.setattr("app.worker.library_sync.get_subtitles", _fake_get_subtitles)

    outcome = asyncio.run(
        sync_one_title(
            settings, item, known_guids=frozenset({"guid-1"}), no_subtitle_guids=frozenset(),
            # Stale fingerprint on purpose — doesn't match the sidecar's real
            # (mtime, size), simulating a subtitle edited since last synced.
            cache_meta={"guid-1": ("sidecar", str(sidecar), (1.0, 1))},
        )
    )

    assert outcome.startswith("OK")


def test_sync_one_title_bulk_path_stays_cached_when_no_sidecar_appeared(tmp_path, monkeypatch):
    """The known_guids/no_subtitle_guids bulk-set path (used by
    sync_library's per-item loop) still needs to recheck a NONE title for a
    newly-appeared sidecar — but with none present, it must stay a cheap
    skip, not fall through to a real (re-)extraction attempt."""
    settings = _settings(tmp_path)
    item = _item("guid-1", "101")

    def _boom(*args, **kwargs):
        raise AssertionError("get_subtitles should not be called when no sidecar appeared")
    monkeypatch.setattr("app.worker.library_sync.get_subtitles", _boom)

    outcome = asyncio.run(
        sync_one_title(
            settings, item, known_guids=frozenset(), no_subtitle_guids=frozenset({"guid-1"})
        )
    )

    assert outcome.startswith("CACHED (already have it)")


def test_sync_one_title_bulk_path_reprocesses_when_sidecar_appeared(tmp_path, monkeypatch):
    """The one behavior this feature exists for: a title previously cached
    as NONE, with a sidecar .srt that has since appeared next to its video,
    must be picked back up on the next sync pass rather than staying
    permanently skipped."""
    settings = _settings(tmp_path)
    item = _item("guid-1", "101")

    media_root = settings.libraries[0].path_mappings[0].container_path
    (Path(media_root) / "film.mkv").write_bytes(b"x")
    (Path(media_root) / "film.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\nHi\n", encoding="utf-8")

    found_entries = [SubtitleEntry(index=1, start=0.0, end=1.0, text="Hi")]

    async def _fake_get_subtitles(movie, container_video_path, cache_dir, db_path, ffprobe_timeout=180.0, ffmpeg_timeout=180.0):
        search_index.upsert_title(
            db_path, movie.guid, movie.media_id, movie.title, movie.library_name,
            "sidecar", container_video_path, None, found_entries, None,
        )
        return SubtitleResult(guid=movie.guid, source=SubtitleSource.SIDECAR, entries=found_entries)

    monkeypatch.setattr("app.worker.library_sync.get_subtitles", _fake_get_subtitles)

    outcome = asyncio.run(
        sync_one_title(
            settings, item, known_guids=frozenset(), no_subtitle_guids=frozenset({"guid-1"})
        )
    )

    assert outcome.startswith("OK")
    assert search_index.has_title(settings.quote_index_db_path, "guid-1") is True


def test_sync_one_title_bulk_path_ignores_stale_legacy_json_when_sidecar_appeared(tmp_path, monkeypatch):
    """A lingering legacy JSON cache file (from before the search_index
    migration) still says NONE from before the sidecar existed — the
    recheck must not let that stale on-disk verdict silently override a
    freshly-found sidecar."""
    settings = _settings(tmp_path)
    item = _item("guid-1", "101")
    _write_legacy_cache_file(settings.cache_dir, SubtitleResult(guid="guid-1", source=SubtitleSource.NONE, entries=[]))

    media_root = settings.libraries[0].path_mappings[0].container_path
    (Path(media_root) / "film.mkv").write_bytes(b"x")
    (Path(media_root) / "film.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\nHi\n", encoding="utf-8")

    found_entries = [SubtitleEntry(index=1, start=0.0, end=1.0, text="Hi")]

    async def _fake_get_subtitles(movie, container_video_path, cache_dir, db_path, ffprobe_timeout=180.0, ffmpeg_timeout=180.0):
        search_index.upsert_title(
            db_path, movie.guid, movie.media_id, movie.title, movie.library_name,
            "sidecar", container_video_path, None, found_entries, None,
        )
        return SubtitleResult(guid=movie.guid, source=SubtitleSource.SIDECAR, entries=found_entries)

    monkeypatch.setattr("app.worker.library_sync.get_subtitles", _fake_get_subtitles)

    outcome = asyncio.run(
        sync_one_title(
            settings, item, known_guids=frozenset(), no_subtitle_guids=frozenset({"guid-1"})
        )
    )

    assert outcome.startswith("OK")
    assert search_index.has_title(settings.quote_index_db_path, "guid-1") is True
    assert is_no_subtitle_title(settings.quote_index_db_path, "guid-1") is False


def test_sync_one_title_skips_already_indexed_search_index_title(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    item = _item("guid-1", "101")
    _precache(settings, "guid-1")

    def _boom(*args, **kwargs):
        raise AssertionError("read_cached_subtitles should not be called for an already-indexed title")
    monkeypatch.setattr("app.worker.library_sync.read_cached_subtitles", _boom)

    outcome = asyncio.run(sync_one_title(settings, item))

    assert outcome.startswith("CACHED (already have it)")


from app.worker.quote_index import get_sync_progress


def test_sync_library_writes_progress_per_item(tmp_path):
    settings = _settings(tmp_path)
    plex = _FakePlex(items=[_item("guid-1", "101", title="Film One"), _item("guid-2", "102", title="Film Two")])
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


def test_sync_library_progress_never_shows_complete_while_a_slow_item_is_in_flight(
    tmp_path, monkeypatch
):
    # Regression for issue #15, adapted for sync_library's now-concurrent
    # per-item loop (see _SYNC_TITLE_CHECK_CONCURRENCY's docstring): the
    # dashboard must never show the bar as fully done while a slow item (a
    # real extraction can take minutes) is still genuinely in flight.
    #
    # A precise "current_title equals exactly this one item" assertion (the
    # original, strictly-sequential form of this test) is no longer
    # meaningful with several items racing concurrently — which of two
    # near-instant cache hits writes current_title last is now an honest
    # race, not a bug. What must still hold deterministically: processed
    # never reaches total while at least one item (here, deliberately held
    # open via an event) hasn't actually finished yet.
    settings = _settings(tmp_path)
    plex = _FakePlex(items=[
        _item("guid-1", "101", title="Film One"),
        _item("guid-2", "102", title="Film Two"),
        _item("guid-3", "103", title="Slow Film"),
    ])
    _precache(settings, "guid-1")
    _precache(settings, "guid-2")

    import app.worker.library_sync as library_sync_module

    real_sync_one_title = library_sync_module.sync_one_title
    slow_item_started = asyncio.Event()
    release_slow_item = asyncio.Event()

    async def _spy(
        settings, item, *, force=False, known_guids=None, no_subtitle_guids=None, cache_meta=None
    ):
        if item.title == "Slow Film":
            slow_item_started.set()
            await release_slow_item.wait()
        return await real_sync_one_title(
            settings, item, force=force, known_guids=known_guids, no_subtitle_guids=no_subtitle_guids,
            cache_meta=cache_meta,
        )

    monkeypatch.setattr(library_sync_module, "sync_one_title", _spy)

    async def _run():
        sync_task = asyncio.create_task(
            sync_library(settings, plex, "Movies", section=None, updated_at=200)
        )
        await slow_item_started.wait()
        await asyncio.sleep(0.05)  # let the two fast cache hits actually finish
        mid_flight_progress = quote_index.get_sync_progress(settings.quote_index_db_path)
        release_slow_item.set()
        await sync_task
        return mid_flight_progress

    mid_flight_progress = asyncio.run(_run())

    assert mid_flight_progress.processed < 3


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
        def current_section_updated_ats(self, names=None):
            return {"Movies": 999}  # unchanged from stored value

        def library_sections(self):
            return [("Movies", object())]

    plex = _PlexWithSections(items=[_item("guid-1", "1"), _item("guid-2", "2")])

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
        def current_section_updated_ats(self, names=None):
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
        def current_section_updated_ats(self, names=None):
            raise ConnectionError("plex unreachable")

    asyncio.run(run_library_sync_once(settings, _RaisingPlex()))

    from app.worker.quote_index import get_sync_progress
    assert get_sync_progress(settings.quote_index_db_path).status == "idle"


def test_both_guards_pass_deletes_removed_title_and_updates_state(tmp_path):
    root = tmp_path / "media"
    root.mkdir(exist_ok=True)
    # The still-present item's actual file, so the spot check finds it.
    (root / "still_present.mkv").write_text("video bytes")
    mappings = [PathMapping(path_prefix="D:\\Movies", container_path=str(root))]
    settings = _settings(tmp_path, mappings=mappings)

    _precache(settings, "guid-removed")
    # A legacy JSON cache file also happens to exist for this guid (e.g.
    # left over from before this migration) — removal must not touch it,
    # since deleting legacy JSON is explicitly out of scope for this
    # migration.
    _write_legacy_cache_file(
        settings.cache_dir,
        SubtitleResult(
            guid="guid-removed", source=SubtitleSource.SIDECAR,
            entries=[SubtitleEntry(index=1, start=0.0, end=1.0, text="Hi")],
        ),
    )
    quote_index.set_section_updated_at(settings.quote_index_db_path, "Movies", 100)

    plex = _FakePlex(
        items=[_item("guid-still-present", "2", source_path="D:\\Movies\\still_present.mkv")]
    )

    result = asyncio.run(sync_library(settings, plex, "Movies", section=None, updated_at=200))

    assert result.removal_skipped_reason is None
    assert result.removed == 1
    assert search_index.list_titles(settings.quote_index_db_path) == []
    # Legacy JSON is left alone, not deleted.
    assert read_cached_subtitles(settings.cache_dir, "guid-removed") is not None
    assert quote_index.get_section_updated_at(settings.quote_index_db_path, "Movies") == 200
