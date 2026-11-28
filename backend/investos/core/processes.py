"""Run a child process under any event loop.

The backend runs on the selector event loop on every platform. On Windows the
default proactor loop uses overlapped I/O, and the backend crashed natively
(an access violation during garbage collection) right after model requests
timed out and their reads were cancelled (2026-11-27 and 11-28). The selector
loop cannot start subprocesses on Windows, so child processes run in a worker
thread through ``subprocess.run``. That works under any loop, and the child is
killed when its timeout expires.
"""

from __future__ import annotations

import asyncio
import subprocess
from dataclasses import dataclass


@dataclass(frozen=True)
class ProcessResult:
    returncode: int
    stdout: bytes
    stderr: bytes


async def run_process(
    *args: str,
    timeout: float,
    input: bytes | None = None,
    cwd: str | None = None,
) -> ProcessResult:
    """Run ``args`` and capture its output; raise ``TimeoutError`` past ``timeout``.

    A cancelled caller stops waiting at once; the thread finishes when the
    child exits or its timeout kills it.
    """

    def run() -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            list(args),
            input=input,
            capture_output=True,
            timeout=timeout,
            cwd=cwd,
            check=False,
        )

    try:
        completed = await asyncio.to_thread(run)
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(f"{args[0]} exceeded {timeout:g} seconds") from exc
    return ProcessResult(
        returncode=completed.returncode,
        stdout=completed.stdout or b"",
        stderr=completed.stderr or b"",
    )
