import asyncio

import pytest

from app.worker.subprocess_utils import SubprocessTimeoutError, run_and_capture


def test_run_and_capture_returns_only_stdout_by_default():
    result = asyncio.run(
        run_and_capture(
            ["sh", "-c", "echo out-text; echo err-text >&2"],
            timeout_seconds=5.0,
            error_prefix="test",
            capture_stdout=True,
        )
    )
    assert result == b"out-text\n"


def test_run_and_capture_returns_stderr_alongside_stdout_when_requested():
    stdout, stderr = asyncio.run(
        run_and_capture(
            ["sh", "-c", "echo out-text; echo err-text >&2"],
            timeout_seconds=5.0,
            error_prefix="test",
            capture_stdout=True,
            capture_stderr=True,
        )
    )
    assert stdout == b"out-text\n"
    assert stderr == b"err-text\n"


def test_run_and_capture_stderr_only_does_not_require_stdout():
    stdout, stderr = asyncio.run(
        run_and_capture(
            ["sh", "-c", "echo out-text; echo err-text >&2"],
            timeout_seconds=5.0,
            error_prefix="test",
            capture_stdout=False,
            capture_stderr=True,
        )
    )
    assert stdout is None
    assert stderr == b"err-text\n"


def test_run_and_capture_still_raises_on_nonzero_exit_with_capture_stderr():
    with pytest.raises(RuntimeError):
        asyncio.run(
            run_and_capture(
                ["sh", "-c", "echo boom >&2; exit 1"],
                timeout_seconds=5.0,
                error_prefix="test",
                capture_stderr=True,
            )
        )


def test_run_and_capture_does_not_leak_raw_stderr_in_exception_message(caplog):
    # A nonzero-exit ffmpeg/ffprobe/gifsicle call's stderr routinely echoes
    # absolute container file paths and codec diagnostics — this must never
    # reach a Discord/web-facing error message (pre-publication audit
    # finding). The full detail must still be available server-side.
    caplog.set_level("WARNING")
    sensitive_stderr = "/media/movies-d/Some Private Title (2020)/file.mkv: Invalid data"

    with pytest.raises(RuntimeError) as excinfo:
        asyncio.run(
            run_and_capture(
                ["sh", "-c", f"echo '{sensitive_stderr}' >&2; exit 1"],
                timeout_seconds=5.0,
                error_prefix="ffmpeg gif encode",
            )
        )

    assert sensitive_stderr not in str(excinfo.value)
    assert "Some Private Title" not in str(excinfo.value)
    # The generic message must still say *what* failed, just not *why* in
    # raw subprocess-internals detail.
    assert "ffmpeg gif encode" in str(excinfo.value)
    # ...but the full stderr must still be diagnosable server-side.
    assert sensitive_stderr in caplog.text
