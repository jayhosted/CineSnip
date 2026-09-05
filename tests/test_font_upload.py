import glob

import pytest

from app.worker.font_upload import (
    FontValidationError,
    delete_font_file,
    font_file_path,
    process_font_upload,
)


@pytest.fixture
def real_ttf_bytes():
    # Building a valid TTF from scratch in a test is more machinery than
    # this feature warrants (would need fontTools as a new dependency just
    # for tests). Instead, this fixture requires a real system font already
    # present in the dev/CI environment. Liberation Sans (installed by the
    # Dockerfile via fonts-liberation) is tried first, but this dev box only
    # has DejaVu/Ubuntu fonts installed, so a few other common regular-style
    # TTF families are tried as fallbacks — any real, non-variable .ttf file
    # is equally good for exercising fc-scan.
    candidate_globs = [
        "/usr/share/fonts/**/LiberationSans-Regular.ttf",
        "/usr/share/fonts/**/DejaVuSans.ttf",
        "/usr/share/fonts/**/DejaVuSansMono.ttf",
    ]
    for pattern in candidate_globs:
        matches = glob.glob(pattern, recursive=True)
        if matches:
            return open(matches[0], "rb").read()
    pytest.skip("No system TTF found to use as a real-font fixture")


@pytest.mark.anyio
async def test_process_font_upload_accepts_a_real_font(tmp_path, real_ttf_bytes):
    result = await process_font_upload(
        real_ttf_bytes, "custom.ttf", "simpsons", tmp_path / "fonts"
    )
    assert result.family
    assert (tmp_path / "fonts" / "simpsons.ttf").exists()


@pytest.mark.anyio
async def test_process_font_upload_rejects_wrong_extension(tmp_path, real_ttf_bytes):
    with pytest.raises(FontValidationError):
        await process_font_upload(real_ttf_bytes, "custom.exe", "simpsons", tmp_path / "fonts")


@pytest.mark.anyio
async def test_process_font_upload_rejects_garbage_with_font_extension(tmp_path):
    with pytest.raises(FontValidationError):
        await process_font_upload(b"not a real font at all", "custom.ttf", "simpsons", tmp_path / "fonts")


@pytest.mark.anyio
async def test_process_font_upload_rejects_oversized_file(tmp_path, real_ttf_bytes):
    oversized = real_ttf_bytes + b"\x00" * (5 * 1024 * 1024)
    with pytest.raises(FontValidationError):
        await process_font_upload(oversized, "custom.ttf", "simpsons", tmp_path / "fonts")


@pytest.mark.anyio
async def test_process_font_upload_flags_missing_punctuation_without_rejecting(tmp_path, real_ttf_bytes):
    # A standard system font covers common dialogue punctuation, so this
    # exercises the "nothing missing" branch — real coverage of the
    # "something missing" branch is the unit test on _decode_fc_charset
    # directly (a font that only draws letters+digits isn't something we
    # can easily source in CI, so the charset-parsing logic itself is what's
    # unit tested).
    result = await process_font_upload(real_ttf_bytes, "custom.ttf", "simpsons", tmp_path / "fonts")
    assert result.missing_punctuation == ""


def test_decode_fc_charset_flags_missing_punctuation():
    from app.worker.font_upload import _COMMON_DIALOGUE_PUNCTUATION, _decode_fc_charset

    # A charset covering only 'a'-'z' (0x61-0x7a) — no punctuation at all.
    covered = _decode_fc_charset("61-7a")
    missing = "".join(ch for ch in _COMMON_DIALOGUE_PUNCTUATION if ord(ch) not in covered)
    assert missing == _COMMON_DIALOGUE_PUNCTUATION


def test_decode_fc_charset_handles_full_ascii_range():
    from app.worker.font_upload import _COMMON_DIALOGUE_PUNCTUATION, _decode_fc_charset

    covered = _decode_fc_charset("20-7e")
    missing = "".join(ch for ch in _COMMON_DIALOGUE_PUNCTUATION if ord(ch) not in covered)
    assert missing == ""


def test_delete_font_file_removes_an_existing_file(tmp_path):
    font_path = tmp_path / "custom.ttf"
    font_path.write_bytes(b"x")
    delete_font_file(str(font_path))
    assert not font_path.exists()


def test_delete_font_file_is_a_noop_for_none():
    delete_font_file(None)  # must not raise


def test_font_file_path_keeps_normal_preset_name_unchanged(tmp_path):
    assert font_file_path(tmp_path, "simpsons", ".ttf") == tmp_path / "simpsons.ttf"


@pytest.mark.parametrize(
    "preset_name, expected_name",
    [
        ("../../etc/passwd", "passwd.ttf"),
        ("foo/bar", "bar.ttf"),
        ("../simpsons", "simpsons.ttf"),
    ],
)
def test_font_file_path_confines_traversal_attempts_to_fonts_dir(tmp_path, preset_name, expected_name):
    # Any '../' or embedded separator is stripped down to its final
    # component, so the result can never land outside fonts_dir.
    result = font_file_path(tmp_path, preset_name, ".ttf")
    assert result == tmp_path / expected_name
    assert result.parent == tmp_path


@pytest.mark.parametrize("bad_preset_name", ["", ".", ".."])
def test_font_file_path_rejects_names_that_collapse_to_nothing(tmp_path, bad_preset_name):
    with pytest.raises(FontValidationError):
        font_file_path(tmp_path, bad_preset_name, ".ttf")


@pytest.mark.anyio
async def test_process_font_upload_confines_traversal_preset_name_to_fonts_dir(tmp_path, real_ttf_bytes):
    fonts_dir = tmp_path / "fonts"
    result = await process_font_upload(real_ttf_bytes, "custom.ttf", "../../etc/passwd", fonts_dir)
    # Sanitized down to the final path component — written inside fonts_dir,
    # never outside it.
    assert result.font_path == str(fonts_dir / "passwd.ttf")
    assert not (tmp_path / "etc").exists()


@pytest.mark.anyio
async def test_process_font_upload_rejects_preset_name_that_collapses_to_dotdot(tmp_path, real_ttf_bytes):
    fonts_dir = tmp_path / "fonts"
    with pytest.raises(FontValidationError):
        await process_font_upload(real_ttf_bytes, "custom.ttf", "..", fonts_dir)
    # Fails fast before any temp file is written.
    assert not fonts_dir.exists() or list(fonts_dir.glob(".upload-*")) == []
