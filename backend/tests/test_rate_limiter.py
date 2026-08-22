import asyncio
import logging

import httpx

from investos.core.llm import (
    AsyncRateLimiter,
    CodexCLIFailedError,
    _cooldown_remaining,
    _provider_cooldowns,
    _summarize_process_stderr,
    call_llm_json,
    retry_with_backoff,
)


async def test_pacing_spaces_acquisitions():
    # 1200/min -> 50ms between grants. 5 grants => >= 4 intervals after the first.
    limiter = AsyncRateLimiter(rate_per_minute=1200, max_concurrency=10)
    loop = asyncio.get_running_loop()
    start = loop.time()
    for _ in range(5):
        await limiter.acquire()
        limiter.release()
    elapsed = loop.time() - start
    assert elapsed >= 0.18  # 4 * 50ms with slack


async def test_concurrency_cap_is_enforced():
    limiter = AsyncRateLimiter(rate_per_minute=0, max_concurrency=2)  # no pacing
    in_flight = 0
    peak = 0

    async def worker():
        nonlocal in_flight, peak
        async with limiter:
            in_flight += 1
            peak = max(peak, in_flight)
            await asyncio.sleep(0.05)
            in_flight -= 1

    await asyncio.gather(*[worker() for _ in range(6)])
    assert peak <= 2


async def test_semaphore_released_on_cancel_during_pacing():
    # A slow-paced limiter: ensure a cancelled acquire does not leak a slot.
    limiter = AsyncRateLimiter(rate_per_minute=60, max_concurrency=1)  # 1s interval
    await limiter.acquire()  # take the only slot, set next_allowed 1s out
    limiter.release()

    task = asyncio.create_task(limiter.acquire())
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    # Slot must be free again despite the cancellation mid-pacing-sleep.
    await asyncio.wait_for(limiter.acquire(), timeout=2.0)
    limiter.release()


async def test_short_timeout_retry_policy_fails_fast():
    calls = 0

    @retry_with_backoff(max_retries=3, base_delay=0.0, max_delay=0.0)
    async def flaky(*, timeout_seconds: int):
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("boom")

    try:
        await flaky(timeout_seconds=3)
    except httpx.ConnectError:
        pass

    assert calls == 1


async def test_medium_timeout_retry_policy_allows_one_retry():
    calls = 0

    @retry_with_backoff(max_retries=3, base_delay=0.0, max_delay=0.0)
    async def flaky(*, timeout_seconds: int):
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("boom")

    try:
        await flaky(timeout_seconds=6)
    except httpx.ConnectError:
        pass

    assert calls == 2


def test_codex_cli_failure_summary_omits_prompt_payload():
    stderr = "\n".join(
        [
            "debug transcript line",
            "SENSITIVE PROMPT PAYLOAD",
            "ERROR: You've hit your usage limit. Try again later.",
        ]
    )

    err = CodexCLIFailedError(exit_code=1, stderr_text=stderr)

    assert "usage limit" in str(err).lower()
    assert "SENSITIVE PROMPT PAYLOAD" not in str(err)
    assert len(str(err)) < 650


def test_process_stderr_summary_does_not_emit_arbitrary_tail():
    stderr = "\n".join(["line one", "SENSITIVE PROMPT PAYLOAD", "line three"])

    summary = _summarize_process_stderr(stderr)

    assert summary == "stderr_lines=3 stderr_chars=44"
    assert "SENSITIVE PROMPT PAYLOAD" not in summary


def test_process_stderr_summary_keeps_schema_diagnostics():
    stderr = "warning\ninvalid_json_schema: missing required field\nignored"

    summary = _summarize_process_stderr(stderr)

    assert summary == "invalid_json_schema: missing required field"


async def test_llm_request_error_logs_compact_warning(monkeypatch, caplog):
    async def fake_nvidia_json(**kwargs):
        raise httpx.ReadTimeout("provider timed out")

    monkeypatch.setattr("investos.core.llm._call_nvidia_json", fake_nvidia_json)

    with caplog.at_level(logging.WARNING):
        try:
            await call_llm_json(
                system_prompt="system",
                user_prompt="user",
                schema={
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {"ok": {"type": "boolean"}},
                    "required": ["ok"],
                },
                provider_override="nvidia_nim",
                timeout_seconds=1,
            )
        except httpx.ReadTimeout:
            pass

    text = caplog.text
    assert "Hosted LLM request failed: ReadTimeout: provider timed out" in text
    assert "Masked failure caught" not in text
    assert "Traceback" not in text


async def test_non_retryable_codex_failure_opens_recovery_cooldown(monkeypatch):
    async def incompatible_codex(**kwargs):
        raise CodexCLIFailedError(
            exit_code=1,
            stderr_text="ERROR: configured model requires a newer version of Codex.",
        )

    _provider_cooldowns.pop("codex_cli", None)
    monkeypatch.setattr("investos.core.llm._call_codex_json", incompatible_codex)

    try:
        await call_llm_json(
            system_prompt="system",
            user_prompt="user",
            schema={"type": "object", "properties": {}, "additionalProperties": False},
            provider_override="codex_cli",
            timeout_seconds=1,
        )
    except CodexCLIFailedError:
        pass

    assert _cooldown_remaining("codex_cli") > 0
    _provider_cooldowns.pop("codex_cli", None)
