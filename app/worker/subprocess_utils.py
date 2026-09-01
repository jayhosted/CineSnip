from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


class SubprocessTimeoutError(RuntimeError):
    pass


async def run_and_capture(
    args: list[str],
    timeout_seconds: float,
    error_prefix: str,
    capture_stdout: bool = False,
    capture_stderr: bool = False,
    stdin_data: bytes | None = None,
) -> bytes | None | tuple[bytes | None, bytes]:
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.PIPE if stdin_data is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        # communicate() drains stdout and stderr concurrently, avoiding
        # the deadlock that follows from only reading one pipe while
        # the process blocks writing to the other once its buffer fills.
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=stdin_data), timeout=timeout_seconds
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise SubprocessTimeoutError(
            f"{error_prefix} timed out after {timeout_seconds:.0f}s "
            f"— this source file may be unusually slow to seek/decode."
        ) from None

    if proc.returncode != 0:
        # stderr routinely echoes absolute container file paths and
        # codec/filter diagnostics — never put it in the exception message
        # a Discord reply or the /generate web UI ends up displaying
        # (pre-publication audit finding). The full detail stays available
        # server-side for troubleshooting.
        logger.error(
            "%s failed (exit %s): %s",
            error_prefix,
            proc.returncode,
            stderr.decode(errors="replace"),
        )
        raise RuntimeError(f"{error_prefix} failed. See worker logs for details.")
    if capture_stderr:
        return (stdout if capture_stdout else None), stderr
    return stdout if capture_stdout else None
