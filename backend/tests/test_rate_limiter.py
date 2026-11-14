import asyncio
import logging

import httpx
import pytest

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


@pytest.mark.parametrize("status", [429, 503])
async def test_long_server_wait_defers_instead_of_retrying_early(monkeypatch, status):
    from unittest.mock import AsyncMock

    from investos.core import llm

    monkeypatch.setattr(llm, "_provider_cooldowns", {})
    sleep = AsyncMock()
    monkeypatch.setattr(llm.asyncio, "sleep", sleep)
    calls = 0

    @retry_with_backoff(max_retries=3)
    async def nvidia_synthetic_request(*, model):
        nonlocal calls
        calls += 1
        httpx.Response(
            status,
            headers={"Retry-After": "7200"},
            request=httpx.Request("POST", "https://example.test"),
        ).raise_for_status()

    with pytest.raises(httpx.HTTPStatusError):
        await nvidia_synthetic_request(model="synthetic/maintenance")
    assert calls == 1
    assert _cooldown_remaining("nvidia_nim", "synthetic/maintenance") > 7195
    sleep.assert_not_awaited()


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


@pytest.fixture(autouse=True)
def isolated_provider_health(monkeypatch, tmp_path):
    from investos.core.provider_health import ProviderHealthStore

    monkeypatch.setattr(
        ProviderHealthStore,
        "_path",
        staticmethod(lambda: tmp_path / "provider-health.json"),
    )


async def test_hosted_cooldown_keeps_other_model_available_without_retrying_failed_model(
    monkeypatch,
):
    from investos.core import llm
    from investos.core.provider_health import LLMProviderCooldownError

    monkeypatch.setattr(llm, "_provider_cooldowns", {})
    calls = []

    @retry_with_backoff(max_retries=0)
    async def nvidia_synthetic_request(*, payload):
        calls.append(payload["model"])
        if payload["model"] == "example/analyst":
            response = httpx.Response(
                429,
                headers={"Retry-After": "120"},
                request=httpx.Request(
                    "POST", "https://llm.example.test/v1/chat/completions"
                ),
            )
            response.raise_for_status()
        return {"ok": True}

    with pytest.raises(httpx.HTTPStatusError):
        await nvidia_synthetic_request(payload={"model": "example/analyst"})
    assert _cooldown_remaining("nvidia_nim", "example/analyst") > 100
    assert await nvidia_synthetic_request(payload={"model": "example/reader"}) == {
        "ok": True
    }
    with pytest.raises(LLMProviderCooldownError):
        await nvidia_synthetic_request(payload={"model": "example/analyst"})
    assert calls == ["example/analyst", "example/reader"]
