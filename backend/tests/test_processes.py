"""Child processes run under any event loop, with their timeout enforced."""

import asyncio
import sys
import time

import pytest

from investos.core.processes import run_process


async def test_a_child_process_returns_its_output_and_exit_code():
    result = await run_process(
        sys.executable, "-c", "import sys; print('ok'); sys.exit(3)", timeout=30
    )
    assert result.stdout.strip() == b"ok"
    assert result.returncode == 3


async def test_input_reaches_the_child():
    result = await run_process(
        sys.executable,
        "-c",
        "import sys; print(sys.stdin.read().upper())",
        input=b"hello",
        timeout=30,
    )
    assert result.stdout.strip() == b"HELLO"


async def test_a_slow_child_is_killed_at_its_timeout():
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        await run_process(
            sys.executable, "-c", "import time; time.sleep(30)", timeout=0.5
        )
    assert time.monotonic() - started < 10


def test_works_under_the_selector_loop_the_backend_uses():
    # On Windows the selector loop cannot start subprocesses itself.
    loop = asyncio.SelectorEventLoop()
    try:
        result = loop.run_until_complete(
            run_process(sys.executable, "-c", "print(1)", timeout=30)
        )
    finally:
        loop.close()
    assert result.stdout.strip() == b"1"
