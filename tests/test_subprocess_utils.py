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
