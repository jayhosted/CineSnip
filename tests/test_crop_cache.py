from app.worker.crop_cache import get_cached_crop, set_cached_crop


def test_get_cached_crop_returns_none_when_not_cached(tmp_path):
    db_path = tmp_path / "quote_index.db"
    assert get_cached_crop(db_path, "/media/film.mkv", fingerprint=(123.0, 456)) is None


def test_set_and_get_cached_crop_round_trip(tmp_path):
    db_path = tmp_path / "quote_index.db"
    fingerprint = (123.456, 789)
    set_cached_crop(db_path, "/media/film.mkv", crop_box=(3840, 1604, 0, 278), fingerprint=fingerprint)

    result = get_cached_crop(db_path, "/media/film.mkv", fingerprint=fingerprint)

    assert result.crop_box == (3840, 1604, 0, 278)


def test_set_and_get_cached_crop_stores_confirmed_no_crop_distinctly(tmp_path):
    # A title with no baked-in bars must be cached as "confirmed: no crop
    # needed" (crop_box=None on a cache HIT), distinct from "never probed"
    # (a cache MISS, i.e. get_cached_crop returning None itself) — otherwise
    # every normal (bar-free) title would re-probe on every single render.
    db_path = tmp_path / "quote_index.db"
    fingerprint = (10.0, 20)
    set_cached_crop(db_path, "/media/normal.mkv", crop_box=None, fingerprint=fingerprint)

    result = get_cached_crop(db_path, "/media/normal.mkv", fingerprint=fingerprint)

    assert result is not None
    assert result.crop_box is None


def test_get_cached_crop_returns_none_when_fingerprint_mismatch(tmp_path):
    # The file changed on disk since it was probed (re-encode, replacement)
    # — the cached crop can no longer be trusted, must be treated as a miss.
    db_path = tmp_path / "quote_index.db"
    set_cached_crop(db_path, "/media/film.mkv", crop_box=(3840, 1604, 0, 278), fingerprint=(1.0, 100))

    result = get_cached_crop(db_path, "/media/film.mkv", fingerprint=(2.0, 200))

    assert result is None


def test_set_cached_crop_upsert_replaces_previous_value(tmp_path):
    db_path = tmp_path / "quote_index.db"
    set_cached_crop(db_path, "/media/film.mkv", crop_box=(3840, 1604, 0, 278), fingerprint=(1.0, 100))
    set_cached_crop(db_path, "/media/film.mkv", crop_box=(1920, 800, 0, 140), fingerprint=(2.0, 200))

    result = get_cached_crop(db_path, "/media/film.mkv", fingerprint=(2.0, 200))

    assert result.crop_box == (1920, 800, 0, 140)
