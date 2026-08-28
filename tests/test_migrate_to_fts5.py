from app.worker import quote_index, search_index
from app.worker.subtitles import SubtitleEntry, SubtitleResult, SubtitleSource, write_cached_subtitles
from scripts.migrate_to_fts5 import migrate_one_title


def _seed_legacy_title(db_path, cache_dir, guid, rating_key, title, library_name, sidecar_path=None):
    """Populate one legacy cached_titles row + its per-title JSON cache
    file, mirroring what get_subtitles()/library_sync used to write before
    the search_index migration."""
    source = SubtitleSource.SIDECAR if sidecar_path else SubtitleSource.EMBEDDED
    entries = [
        SubtitleEntry(index=0, start=1.0, end=2.0, text=f"Hello from {title}"),
        SubtitleEntry(index=1, start=3.0, end=4.0, text="Goodbye"),
    ]
    result = SubtitleResult(
        guid=guid,
        source=source,
        entries=entries,
        sidecar_path=str(sidecar_path) if sidecar_path else None,
        stream_index=None if sidecar_path else 0,
    )
    write_cached_subtitles(cache_dir, result, source_path=sidecar_path)
    quote_index.upsert_cached_title(db_path, guid, rating_key, title, library_name, source.value)


def test_migrate_one_title_sidecar_gets_real_fingerprint(tmp_path):
    db_path = tmp_path / "quote_index.db"
    cache_dir = tmp_path / "cache"
    sidecar = tmp_path / "movie.en.srt"
    sidecar.write_text("dummy")

    _seed_legacy_title(db_path, cache_dir, "guid-1", 101, "Film One", "Movies", sidecar_path=sidecar)
    cached = quote_index.list_cached_titles(db_path)[0]

    outcome = migrate_one_title(db_path, cache_dir, cached)

    assert outcome.status == "migrated"
    assert outcome.fingerprint_kind == "sidecar"
    assert search_index.has_title(db_path, "guid-1")
    assert search_index.get_fingerprint(db_path, "guid-1") is not None
    entries = search_index.get_entries(db_path, "guid-1")
    assert [e.text for e in entries] == ["Hello from Film One", "Goodbye"]


def test_migrate_one_title_embedded_gets_no_fingerprint(tmp_path):
    db_path = tmp_path / "quote_index.db"
    cache_dir = tmp_path / "cache"

    _seed_legacy_title(db_path, cache_dir, "guid-2", 102, "Film Two", "Movies", sidecar_path=None)
    cached = quote_index.list_cached_titles(db_path)[0]

    outcome = migrate_one_title(db_path, cache_dir, cached)

    assert outcome.status == "migrated"
    assert outcome.fingerprint_kind == "embedded"
    assert search_index.has_title(db_path, "guid-2")
    assert search_index.get_fingerprint(db_path, "guid-2") is None


def test_migrate_one_title_missing_json_reports_failure(tmp_path):
    db_path = tmp_path / "quote_index.db"
    cache_dir = tmp_path / "cache"
    # cached_titles row exists but its JSON cache file was never written.
    quote_index.upsert_cached_title(db_path, "guid-3", 103, "Film Three", "Movies", "sidecar")
    cached = quote_index.list_cached_titles(db_path)[0]

    outcome = migrate_one_title(db_path, cache_dir, cached)

    assert outcome.status == "missing_json"
    assert not search_index.has_title(db_path, "guid-3")


def test_resumability_second_pass_completes_interrupted_run(tmp_path):
    db_path = tmp_path / "quote_index.db"
    cache_dir = tmp_path / "cache"

    titles = []
    for i in range(5):
        guid = f"guid-{i}"
        sidecar = tmp_path / f"movie-{i}.en.srt"
        sidecar.write_text("dummy")
        _seed_legacy_title(db_path, cache_dir, guid, 100 + i, f"Film {i}", "Movies", sidecar_path=sidecar)
        titles.append(guid)

    all_cached = quote_index.list_cached_titles(db_path)
    assert len(all_cached) == 5

    # Simulate an interrupted first run: only migrate the first 3 titles.
    for cached in all_cached[:3]:
        outcome = migrate_one_title(db_path, cache_dir, cached)
        assert outcome.status == "migrated"

    migrated_after_partial = {t.guid for t in search_index.list_titles(db_path)}
    assert migrated_after_partial == set(titles[:3])

    # Re-run over the FULL set (as the CLI would): already-migrated titles
    # are skipped, the rest get migrated, with no duplicate/corrupted rows.
    statuses = [migrate_one_title(db_path, cache_dir, cached).status for cached in all_cached]
    assert statuses.count("skipped") == 3
    assert statuses.count("migrated") == 2

    final_titles = search_index.list_titles(db_path)
    assert {t.guid for t in final_titles} == set(titles)
    # No duplicates: exactly one titles row per guid.
    assert len(final_titles) == len(set(t.guid for t in final_titles)) == 5

    for guid in titles:
        entries = search_index.get_entries(db_path, guid)
        assert len(entries) == 2  # not doubled/corrupted by the re-run

    # A third run (fully migrated already) should be an all-skip no-op.
    statuses_third = [migrate_one_title(db_path, cache_dir, cached).status for cached in all_cached]
    assert statuses_third == ["skipped"] * 5


def test_force_reprocesses_already_migrated_titles(tmp_path):
    db_path = tmp_path / "quote_index.db"
    cache_dir = tmp_path / "cache"
    sidecar = tmp_path / "movie.en.srt"
    sidecar.write_text("dummy")

    _seed_legacy_title(db_path, cache_dir, "guid-1", 101, "Film One", "Movies", sidecar_path=sidecar)
    cached = quote_index.list_cached_titles(db_path)[0]

    first = migrate_one_title(db_path, cache_dir, cached)
    assert first.status == "migrated"

    second = migrate_one_title(db_path, cache_dir, cached)
    assert second.status == "skipped"

    forced = migrate_one_title(db_path, cache_dir, cached, force=True)
    assert forced.status == "migrated"
