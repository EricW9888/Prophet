"""Persistent degradation, actual probe leases and domain counter reconciliation."""

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from investos.config import settings
from investos.core.llm import call_llm_json
from investos.core.provider_health import (
    LLMProviderCooldownError,
    ProviderHealthStore,
    probe_llm_provider,
    retry_after_seconds,
)
from investos.core.providers import StructuredGenerationError
from investos.models.evidence import EvidenceProcessingState, RawEvidence
from investos.models.reasoning import CritiqueRun, ReasoningRun
from investos.models.source import Source
from investos.services.automation import AutomationCoordinator, JobTelemetry
from investos.services.dashboard import DashboardService
from investos.services.evidence_processing import EvidenceProcessingService
from investos.services.runtime_settings import RuntimeSettings, RuntimeSettingsStore
from investos.services.work_outcomes import (
    evidence_work_outcome,
    group_research_operations,
    model_run_outcome,
)
from investos.workers.extraction import ExtractionWorker


@pytest.fixture(autouse=True)
def isolated_health(monkeypatch, tmp_path):
    runtime = RuntimeSettings()
    runtime.llm.api_key = "synthetic-secret-never-persisted"
    monkeypatch.setattr(RuntimeSettingsStore, "load", lambda: runtime)
    monkeypatch.setattr(
        ProviderHealthStore, "_path", staticmethod(lambda: tmp_path / "health.json")
    )
    return runtime


def open_health():
    key, provider = ProviderHealthStore.configuration()
    ProviderHealthStore.failed(
        key, provider, httpx.ReadTimeout("private provider diagnostic")
    )
    return key, provider


def make_probe_due():
    registry = ProviderHealthStore._read()
    for record in registry.records.values():
        record.next_probe_at = datetime.now(UTC) - timedelta(seconds=1)
    ProviderHealthStore._write(registry)


@pytest.mark.parametrize(
    "header,expected",
    [
        ("7200", 7200),
        ("0", 0),
        ("Sat, 14 Nov 2026 08:00:00 GMT", 7200),
        ("Saturday, 14-Nov-26 08:00:00 GMT", 7200),
        ("Sat Nov 14 08:00:00 2026", 7200),
        ("Sat, 14 Nov 2026 05:00:00 GMT", 0),
        ("not-a-date", None),
        ("-1", None),
        ("NaN", None),
        ("inf", None),
        ("9" * 400, None),
    ],
)
def test_retry_after_accepts_standard_dates_and_seconds_without_a_local_cap(
    header, expected
):
    assert (
        retry_after_seconds(
            httpx.Headers({"Retry-After": header}),
            now=datetime(2026, 11, 14, 6, tzinfo=UTC),
        )
        == expected
    )


@pytest.mark.parametrize("status", [429, 503])
@pytest.mark.parametrize("date_form", [False, True])
async def test_server_requested_wait_survives_in_the_durable_circuit(
    monkeypatch, status, date_form
):
    deadline = datetime.now(UTC) + timedelta(hours=2)
    header = format_datetime(deadline, usegmt=True) if date_form else "7200"
    response = httpx.Response(
        status,
        headers={"Retry-After": header},
        request=httpx.Request("POST", "https://example.test"),
    )
    model = AsyncMock(
        side_effect=httpx.HTTPStatusError(
            "Synthetic provider maintenance",
            request=response.request,
            response=response,
        )
    )
    monkeypatch.setattr("investos.core.llm._call_nvidia_json", model)
    with pytest.raises(httpx.HTTPStatusError):
        await call_llm_json("system", "work", {"type": "object"})
    record = ProviderHealthStore.read()
    assert record.next_probe_at >= deadline - timedelta(seconds=2)
    assert not ProviderHealthStore.can_admit_work(ProviderHealthStore.public_status())
    assert model.await_count == 1


async def test_cancelled_probe_releases_its_lease_without_fabricating_success(
    monkeypatch,
):
    from investos.core.provider_health import probe_llm_provider

    open_health()
    make_probe_due()
    entered = asyncio.Event()

    async def pending(**kwargs):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr("investos.core.llm.call_llm_json", pending)
    task = asyncio.create_task(probe_llm_provider())
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    health = ProviderHealthStore.public_status()
    assert health["state"] == "open" and health["lease_until"] is None
    assert health["last_success_at"] is None and health["failure_count"] == 1
    monkeypatch.setattr(
        "investos.core.llm.call_llm_json", AsyncMock(return_value={"ok": True})
    )
    assert (await probe_llm_provider())["state"] == "recovering"


def test_restart_retains_failure_and_configuration_change_does_not_erase_it(
    isolated_health,
):
    key, _ = open_health()
    assert ProviderHealthStore().read().state == "open"
    with pytest.raises(LLMProviderCooldownError):
        ProviderHealthStore.require_available()
    contents = ProviderHealthStore._path().read_text(encoding="utf-8")
    assert (
        "synthetic-secret" not in contents
        and "private provider diagnostic" not in contents
    )
    isolated_health.llm.hosted_model = "synthetic-different-model"
    assert ProviderHealthStore.read().state == "unobserved"
    assert ProviderHealthStore._read().records[key].state == "open"


def test_old_success_cannot_clear_new_failure():
    key, provider = ProviderHealthStore.configuration()
    revision = ProviderHealthStore.require_available()
    open_health()
    ProviderHealthStore.succeeded(key, provider, revision)
    assert ProviderHealthStore.read().state == "open"


def test_changed_request_protocol_has_its_own_health_and_preserves_previous_failure(
    monkeypatch,
):
    key, _ = open_health()
    monkeypatch.setattr(
        "investos.core.provider_health.llm_structured_request_options",
        lambda *args: {"chat_template_kwargs": {"enable_thinking": False}},
    )
    assert ProviderHealthStore.configuration()[0] != key
    assert ProviderHealthStore.read().state == "unobserved"
    assert ProviderHealthStore._read().records[key].state == "open"


def test_structured_failure_clears_only_after_real_work_recovers():
    key, provider = ProviderHealthStore.configuration()
    ProviderHealthStore.failed(
        key,
        provider,
        StructuredGenerationError(
            "truncated_output",
            finish_reason="length",
            content_chars=0,
            completion_tokens=64,
            reasoning_tokens=64,
            max_tokens=64,
        ),
    )
    status = ProviderHealthStore.public_status()
    assert status["failure_code"] == "invalid_response"
    assert status["failure_detail"]["kind"] == "truncated_output"
    make_probe_due()
    _, _, token = ProviderHealthStore.claim_probe()
    assert ProviderHealthStore.complete_probe(key, token)
    assert (
        ProviderHealthStore.public_status()["failure_detail"]["kind"]
        == "truncated_output"
    )
    revision = ProviderHealthStore.claim_work(key, provider)
    ProviderHealthStore.succeeded(key, provider, revision)
    assert ProviderHealthStore.public_status()["failure_detail"] is None


async def test_open_circuit_does_not_call_provider(monkeypatch):
    open_health()
    request = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr("investos.core.llm._call_nvidia_json", request)
    with pytest.raises(LLMProviderCooldownError):
        await call_llm_json("system", "user", {"type": "object"})
    request.assert_not_awaited()


async def test_waiting_request_rechecks_circuit_at_admission_without_inflating_backoff(
    monkeypatch,
):
    from investos.core import llm

    limiter = llm.AsyncRateLimiter(0, 1)
    await limiter.acquire()
    monkeypatch.setattr(llm, "_get_rate_limiter", lambda: limiter)
    calls = []
    original_client = httpx.AsyncClient

    def transport(request):
        calls.append(request)
        return httpx.Response(
            200, json={"choices": [{"message": {"content": '{"ok":true}'}}]}
        )

    monkeypatch.setattr(
        llm.httpx,
        "AsyncClient",
        lambda **kwargs: original_client(
            transport=httpx.MockTransport(transport), **kwargs
        ),
    )
    task = asyncio.create_task(
        call_llm_json("Return JSON", "Synthetic input", {"type": "object"})
    )
    await asyncio.sleep(0)
    open_health()
    limiter.release()
    with pytest.raises(LLMProviderCooldownError):
        await task
    assert not calls
    assert ProviderHealthStore.read().failure_count == 1
    await asyncio.wait_for(limiter.acquire(), timeout=1)
    limiter.release()


async def test_probe_uses_one_transport_attempt_and_lease_covers_its_budget(
    monkeypatch,
):
    from investos.core import llm
    from investos.core.provider_health import _probe_call

    monkeypatch.setattr(llm, "_get_rate_limiter", lambda: llm.AsyncRateLimiter(0, 1))
    attempts = []

    def transport(request):
        attempts.append(request)
        raise httpx.ReadTimeout("synthetic timeout")

    token = _probe_call.set(True)
    try:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(transport)
        ) as client:
            with pytest.raises(httpx.ReadTimeout):
                await llm._nvidia_chat_json_text(
                    client=client,
                    base_url="https://example.test",
                    api_key="synthetic",
                    payload={},
                    timeout_seconds=settings.LLM_TIMEOUT_SECONDS,
                )
    finally:
        _probe_call.reset(token)
    assert len(attempts) == 1
    ProviderHealthStore.claim_probe()
    assert (
        ProviderHealthStore.read().lease_until - datetime.now(UTC)
    ).total_seconds() >= settings.LLM_TIMEOUT_SECONDS


def test_unobserved_provider_defers_model_work_until_a_real_probe(monkeypatch):
    coordinator = AutomationCoordinator()
    coordinator.telemetry["research_loop"] = JobTelemetry("research_loop", 300)
    monkeypatch.setattr(coordinator, "_log_job_action", lambda **kwargs: None)
    assert coordinator._provider_defers("research_loop") is True
    assert coordinator._provider_defers("integrity_audit") is False


async def test_only_one_actual_generation_probe_resumes_work(monkeypatch):
    open_health()
    make_probe_due()
    calls = []

    async def request(**kwargs):
        calls.append(kwargs)
        await asyncio.sleep(0.01)
        return {"ok": True}

    monkeypatch.setattr("investos.core.llm._call_nvidia_json", request)
    await asyncio.gather(probe_llm_provider(), probe_llm_provider())
    assert len(calls) == 1
    assert calls[0]["timeout_seconds"] == settings.LLM_TIMEOUT_SECONDS
    assert ProviderHealthStore.read().state == "recovering"
    assert ProviderHealthStore.read().last_probe_success_at is not None
    assert ProviderHealthStore.read().last_success_at is None
    assert ProviderHealthStore.require_available() >= 1


async def test_vision_model_probe_does_not_clear_an_unrelated_model_outage(monkeypatch):
    open_health()
    model = "synthetic-vision-model"
    key, provider = ProviderHealthStore.configuration(model=model)
    ProviderHealthStore.failed(key, provider, TimeoutError())
    make_probe_due()
    request = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr("investos.core.llm._call_nvidia_json", request)
    await probe_llm_provider(model=model)
    assert request.await_args.kwargs["model"] == model
    assert ProviderHealthStore.public_status(model=model)["state"] == "recovering"
    assert ProviderHealthStore.public_status()["state"] == "open"


async def test_failed_probe_stays_open_and_backoff_prevents_retry_churn(monkeypatch):
    open_health()
    make_probe_due()
    request = AsyncMock(side_effect=httpx.ReadTimeout("private"))
    monkeypatch.setattr("investos.core.llm._call_nvidia_json", request)
    await probe_llm_provider()
    await probe_llm_provider()
    assert request.await_count == 1
    assert ProviderHealthStore.read().state == "open"
    assert ProviderHealthStore.read().failure_count == 2


def test_persisted_lease_and_stale_probe_cannot_replace_new_health():
    open_health()
    make_probe_due()
    key, provider, token = ProviderHealthStore.claim_probe()
    assert ProviderHealthStore().claim_probe() is None
    registry = ProviderHealthStore._read()
    registry.records[key].lease_until = datetime.now(UTC) - timedelta(seconds=1)
    ProviderHealthStore._write(registry)
    _, _, second = ProviderHealthStore.claim_probe()
    assert ProviderHealthStore.complete_probe(key, second)
    assert not ProviderHealthStore.complete_probe(key, token)
    ProviderHealthStore.failed(key, provider, TimeoutError(), probe_token=token)
    assert ProviderHealthStore.read().state == "recovering"


async def test_recovery_admits_one_real_request_and_defers_competitors(monkeypatch):
    open_health()
    make_probe_due()
    key, _, token = ProviderHealthStore.claim_probe()
    ProviderHealthStore.complete_probe(key, token)
    started, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def request(**kwargs):
        # The admitted request must also pass the transport's admission check.
        ProviderHealthStore.require_available()
        calls.append(kwargs)
        started.set()
        await release.wait()
        return {"ok": True}

    monkeypatch.setattr("investos.core.llm._call_nvidia_json", request)
    task = asyncio.create_task(call_llm_json("system", "user", {"type": "object"}))
    await started.wait()
    try:
        with pytest.raises(LLMProviderCooldownError):
            await call_llm_json("system", "competing", {"type": "object"})
        assert len(calls) == 1
        assert ProviderHealthStore().claim_probe() is None
        coordinator = AutomationCoordinator()
        coordinator.telemetry["research_loop"] = JobTelemetry("research_loop", 300)
        monkeypatch.setattr(coordinator, "_log_job_action", lambda **kwargs: None)
        assert coordinator._provider_defers("research_loop")
    finally:
        release.set()
        await task
    record = ProviderHealthStore.read()
    assert record.state == "healthy"
    assert record.failure_count == 0
    assert record.last_success_at is not None
    assert record.lease_until is None


async def test_small_probes_cannot_erase_repeated_full_request_throttling(monkeypatch):
    key, provider = ProviderHealthStore.configuration()
    response = httpx.Response(
        429, request=httpx.Request("POST", "https://example.test")
    )
    failure = httpx.HTTPStatusError(
        "synthetic limit", request=response.request, response=response
    )
    ProviderHealthStore.failed(key, provider, failure)
    request = AsyncMock(side_effect=failure)
    monkeypatch.setattr("investos.core.llm._call_nvidia_json", request)
    for count in range(2, 5):
        make_probe_due()
        _, _, token = ProviderHealthStore.claim_probe()
        ProviderHealthStore.complete_probe(key, token)
        with pytest.raises(httpx.HTTPStatusError):
            await call_llm_json("system", "real work", {"type": "object"})
        assert ProviderHealthStore.read().failure_count == count
    assert request.await_count == 3
    assert (
        ProviderHealthStore.read().next_probe_at - datetime.now(UTC)
    ).total_seconds() > 470


async def test_cancelled_recovery_trial_releases_lease_without_resetting_failures(
    monkeypatch,
):
    open_health()
    make_probe_due()
    key, _, token = ProviderHealthStore.claim_probe()
    ProviderHealthStore.complete_probe(key, token)
    started = asyncio.Event()

    async def request(**kwargs):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr("investos.core.llm._call_nvidia_json", request)
    task = asyncio.create_task(call_llm_json("system", "user", {"type": "object"}))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert ProviderHealthStore.read().state == "recovering"
    assert ProviderHealthStore.read().lease_until is None
    assert ProviderHealthStore.read().failure_count == 1


@pytest.mark.parametrize(
    "error,status",
    [
        (LLMProviderCooldownError("synthetic shared recovery"), "deferred"),
        (RuntimeError("synthetic processing failure"), "error"),
    ],
)
async def test_extraction_worker_reports_capacity_deferral_separately_from_failure(
    monkeypatch, error, status
):
    @asynccontextmanager
    async def session():
        yield object()

    monkeypatch.setattr("investos.services.automation.async_session_maker", session)
    monkeypatch.setattr(
        EvidenceProcessingService,
        "next_due_evidence",
        AsyncMock(return_value=SimpleNamespace(id="synthetic-evidence")),
    )
    monkeypatch.setattr(
        ExtractionWorker, "process_evidence", AsyncMock(side_effect=error)
    )
    coordinator = AutomationCoordinator()
    coordinator.telemetry["evidence_processing"] = JobTelemetry(
        "evidence_processing", 30
    )
    logged = []
    monkeypatch.setattr(
        coordinator, "_log_job_action", lambda **kwargs: logged.append(kwargs)
    )
    await coordinator._run_evidence_processing()
    assert coordinator.telemetry["evidence_processing"].last_status == status
    assert logged[-1]["status"] == status
    assert ("deferred" in logged[-1]["summary"]) == (status == "deferred")


def test_corrupt_health_storage_defers_instead_of_claiming_healthy():
    ProviderHealthStore._path().write_text("broken", encoding="utf-8")
    assert ProviderHealthStore.public_status()["state"] == "storage_unavailable"
    with pytest.raises(LLMProviderCooldownError):
        ProviderHealthStore.require_available()


def test_expired_trial_lease_is_fenced_after_restart():
    open_health()
    make_probe_due()
    key, provider, token = ProviderHealthStore.claim_probe()
    ProviderHealthStore.complete_probe(key, token)
    old_revision = ProviderHealthStore.claim_work(key, provider)
    registry = ProviderHealthStore._read()
    registry.records[key].lease_until = datetime.now(UTC) - timedelta(seconds=1)
    ProviderHealthStore._write(registry)
    new_revision = ProviderHealthStore().claim_work(key, provider)
    ProviderHealthStore.succeeded(key, provider, old_revision)
    ProviderHealthStore.release_trial(key, old_revision)
    assert ProviderHealthStore.read().state == "recovering"
    assert ProviderHealthStore.read().lease_until > datetime.now(UTC)
    ProviderHealthStore.succeeded(key, provider, new_revision)
    assert ProviderHealthStore.read().state == "healthy"


async def test_dependent_automation_defers_but_deterministic_job_runs(monkeypatch):
    open_health()
    coordinator = AutomationCoordinator()
    coordinator.telemetry["strategist_cycle"] = JobTelemetry("strategist_cycle", 300)
    monkeypatch.setattr(coordinator, "_log_job_action", lambda **kwargs: None)
    dependent = AsyncMock()
    deterministic = AsyncMock()
    await coordinator._scheduled_job("strategist_cycle", dependent)()
    await coordinator._scheduled_job("market_data_refresh", deterministic)()
    dependent.assert_not_awaited()
    deterministic.assert_awaited_once()
    assert coordinator.telemetry["strategist_cycle"].last_status == "deferred"


@pytest.mark.parametrize(
    "detail,outcome",
    [
        ({"processing": {"extraction_status": "not_applicable"}}, "skipped"),
        ({"outcome": "provider_deferred"}, "deferred"),
        ({"quarantined": True}, "rejected"),
        ({"processing": {"extraction_status": "completed"}}, "enriched"),
        (
            {
                "processing": {
                    "persisted_object_count": 2,
                    "investment_object_status": "completed",
                }
            },
            "knowledge_promoted",
        ),
        ({}, "no_new_knowledge"),
    ],
)
def test_evidence_outcomes_are_not_method_completion(detail, outcome):
    assert evidence_work_outcome(detail) == outcome


def test_one_research_operation_retains_phases_and_separate_legacy_entries():
    newer = {
        "status": "retry_scheduled",
        "phase": "ingest",
        "metadata": {"operation_id": "synthetic"},
    }
    older = {
        "status": "ok",
        "phase": "discovery",
        "estimated_credits": 2,
        "metadata": {"operation_id": "synthetic"},
    }
    grouped = group_research_operations([newer, older, {"status": "ok"}])
    assert len(grouped) == 2 and grouped[0]["status"] == "retry_scheduled"
    assert grouped[0]["estimated_credits"] == 2
    assert [row["phase"] for row in grouped[0]["phases"]] == ["discovery", "ingest"]


def test_unrecognized_model_labels_cannot_claim_live_success():
    assert (
        model_run_outcome(SimpleNamespace(model_used="unexpected-new-label"))
        == "unclassified"
    )


async def test_usage_counters_reconcile_with_real_analysis_and_critique_records():
    engine = create_async_engine(settings.SQLALCHEMY_DATABASE_URI, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            async with AsyncSession(
                bind=connection,
                expire_on_commit=False,
                join_transaction_mode="create_savepoint",
            ) as session:
                before = await DashboardService(session)._llm_usage()
                rows = [
                    ReasoningRun(
                        run_type="analysis",
                        model_used=label,
                        input_tokens=3,
                        output_tokens=2,
                    )
                    for label in [
                        "nvidia_nim",
                        "cache_hit:previous_analysis",
                        "fallback:nvidia_nim_timeout",
                        "deferred:provider",
                        "failed:invalid_response",
                    ]
                ]
                session.add_all(rows)
                await session.flush()
                session.add(
                    CritiqueRun(
                        reasoning_run_id=rows[0].id,
                        model_used="nvidia_nim",
                        critique_text="Synthetic critique",
                        severity="minor",
                        input_tokens=2,
                        output_tokens=1,
                    )
                )
                await session.flush()
                after = await DashboardService(session)._llm_usage()
                assert after.analysis_runs_24h - before.analysis_runs_24h == 5
                for field in [
                    "live_analysis_runs_24h",
                    "cached_runs_24h",
                    "fallback_runs_24h",
                    "deferred_runs_24h",
                    "failed_runs_24h",
                    "critique_runs_24h",
                    "live_critique_runs_24h",
                ]:
                    assert getattr(after, field) - getattr(before, field) == 1
                assert after.verification_runs_24h == before.verification_runs_24h
            await transaction.rollback()
    finally:
        await engine.dispose()


async def test_provider_hold_preserves_extraction_budget_and_operational_receipts(
    monkeypatch,
):
    open_health()
    engine = create_async_engine(settings.SQLALCHEMY_DATABASE_URI, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            async with AsyncSession(
                bind=connection,
                expire_on_commit=False,
                join_transaction_mode="create_savepoint",
            ) as session:
                source = Source(
                    name="Synthetic operational source", source_type="manual"
                )
                session.add(source)
                await session.flush()
                old = RawEvidence(
                    source_id=source.id,
                    source_item_type="article",
                    title="Synthetic pending content",
                    raw_content_ref="synthetic.txt",
                    is_processed=False,
                )
                operational = RawEvidence(
                    source_id=source.id,
                    source_item_type="conversation_turn",
                    title="Synthetic operational receipt",
                    raw_content_ref="synthetic-conversation.txt",
                    is_processed=False,
                )
                session.add_all([old, operational])
                await session.flush()
                worker = ExtractionWorker(session)
                network = AsyncMock()
                monkeypatch.setattr(worker, "_extract_structured_data", network)
                detail = await worker.process_evidence(old.id)
                assert detail["outcome"] == "provider_deferred"
                state = await session.scalar(
                    sa.select(EvidenceProcessingState).where(
                        EvidenceProcessingState.raw_evidence_id == old.id
                    )
                )
                assert state.extraction_attempt_count == 0 and not old.is_processed
                assert (
                    await EvidenceProcessingService(session).next_due_evidence(
                        model_available=False
                    )
                ).id == operational.id
                skipped = await worker.process_evidence(operational.id)
                assert (
                    evidence_work_outcome(skipped) == "skipped"
                    and operational.is_processed
                )
                network.assert_not_awaited()
            await transaction.rollback()
    finally:
        await engine.dispose()


async def test_disabled_coordinator_is_not_reported_as_enabled(monkeypatch):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from investos.api.routes.automation import router

    monkeypatch.setattr(settings, "AUTOMATION_ENABLED", False)
    app = FastAPI()
    app.state.automation = AutomationCoordinator()
    app.include_router(router)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        status = await client.get("/automation/status")
        assert status.status_code == 200
        assert status.json()["automation_enabled"] is False
        assert (
            await client.post("/automation/run/strategist_cycle")
        ).status_code == 503


@pytest.mark.parametrize("error", [httpx.ReadTimeout, TimeoutError])
async def test_short_caller_timeout_preserves_capacity_for_longer_work_and_records_failure(
    monkeypatch, error
):
    monkeypatch.setattr(settings, "LLM_TIMEOUT_SECONDS", 60)
    request = AsyncMock(side_effect=[error("synthetic deadline"), {"ok": True}])
    monkeypatch.setattr("investos.core.llm._call_nvidia_json", request)
    with pytest.raises(error):
        await call_llm_json(
            "system", "short work", {"type": "object"}, timeout_seconds=8
        )
    health = ProviderHealthStore.read()
    assert health.state == "unobserved" and health.last_success_at is None
    assert health.failure_count == 0 and health.caller_timeout_count == 1
    assert health.last_caller_timeout_seconds == 8
    assert health.last_caller_timeout_at is not None
    assert health.next_probe_at is None
    assert await call_llm_json(
        "system", "longer work", {"type": "object"}, timeout_seconds=60
    ) == {"ok": True}
    assert ProviderHealthStore.read().state == "healthy"
    assert ProviderHealthStore.read().caller_timeout_count == 1
    assert request.await_count == 2


@pytest.mark.parametrize("budget", [None, 60, 120])
async def test_shared_budget_timeout_still_opens_health(monkeypatch, budget):
    monkeypatch.setattr(settings, "LLM_TIMEOUT_SECONDS", 60)
    monkeypatch.setattr(
        "investos.core.llm._call_nvidia_json",
        AsyncMock(side_effect=httpx.ReadTimeout("synthetic outage")),
    )
    with pytest.raises(httpx.ReadTimeout):
        await call_llm_json(
            "system", "work", {"type": "object"}, timeout_seconds=budget
        )
    record = ProviderHealthStore.read()
    assert record.state == "open" and record.failure_code == "provider_timeout"
    assert record.caller_timeout_count == 0


@pytest.mark.parametrize("status", [401, 429, 503])
async def test_short_deadline_does_not_hide_real_http_failures_or_retry_after(
    monkeypatch, status
):
    response = httpx.Response(
        status,
        headers={"Retry-After": "900"},
        request=httpx.Request("POST", "https://example.test"),
    )
    failure = httpx.HTTPStatusError(
        "synthetic failure", request=response.request, response=response
    )
    monkeypatch.setattr(
        "investos.core.llm._call_nvidia_json", AsyncMock(side_effect=failure)
    )
    with pytest.raises(httpx.HTTPStatusError):
        await call_llm_json("system", "work", {"type": "object"}, timeout_seconds=8)
    record = ProviderHealthStore.read()
    assert record.state == "open" and record.failure_code == f"http_{status}"
    assert record.caller_timeout_count == 0
    assert (record.next_probe_at - datetime.now(UTC)).total_seconds() > 899


async def test_short_connection_timeout_remains_shared_network_failure(monkeypatch):
    monkeypatch.setattr(
        "investos.core.llm._call_nvidia_json",
        AsyncMock(side_effect=httpx.ConnectTimeout("synthetic connection failure")),
    )
    with pytest.raises(httpx.ConnectTimeout):
        await call_llm_json("system", "work", {"type": "object"}, timeout_seconds=8)
    assert ProviderHealthStore.read().state == "open"
    assert ProviderHealthStore.read().caller_timeout_count == 0


async def test_short_recovery_trial_failure_releases_lease_without_fabricating_recovery(
    monkeypatch,
):
    key, _ = open_health()
    make_probe_due()
    _, _, token = ProviderHealthStore.claim_probe()
    assert ProviderHealthStore.complete_probe(key, token)
    monkeypatch.setattr(
        "investos.core.llm._call_nvidia_json",
        AsyncMock(side_effect=httpx.ReadTimeout("synthetic short trial")),
    )
    with pytest.raises(httpx.ReadTimeout):
        await call_llm_json("system", "trial", {"type": "object"}, timeout_seconds=8)
    record = ProviderHealthStore.read()
    assert record.state == "recovering" and record.last_success_at is None
    assert record.lease_until is None and record.failure_count == 1
    assert record.caller_timeout_count == 1


async def test_mixed_foreground_and_unattended_work_survives_a_short_http_read_deadline(
    monkeypatch, isolated_health
):
    import json

    from investos.core import llm

    received = []
    handlers = set()
    short_received = asyncio.Event()
    release_short = asyncio.Event()

    async def serve(reader, writer):
        task = asyncio.current_task()
        handlers.add(task)
        try:
            headers = await reader.readuntil(b"\r\n\r\n")
            length = int(
                next(
                    line.split(b":", 1)[1].strip()
                    for line in headers.split(b"\r\n")
                    if line.lower().startswith(b"content-length:")
                )
            )
            data = json.loads(await reader.readexactly(length))
            received.append(data["messages"][-1]["content"])
            if data["messages"][-1]["content"] == "short_background":
                short_received.set()
                await release_short.wait()
            else:
                await asyncio.sleep(0.08)
            body = json.dumps(
                {
                    "choices": [
                        {"message": {"content": '{"ok":true}'}, "finish_reason": "stop"}
                    ]
                }
            ).encode()
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
                + str(len(body)).encode()
                + b"\r\nConnection: close\r\n\r\n"
                + body
            )
            await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass
            handlers.discard(task)

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    isolated_health.llm.hosted_base_url = (
        f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
    )
    limiter = llm.AsyncRateLimiter(0, 2)
    monkeypatch.setattr(llm, "_get_rate_limiter", lambda: limiter)
    schema = {
        "type": "object",
        "properties": {"ok": {"type": "boolean"}},
        "required": ["ok"],
        "additionalProperties": False,
    }
    try:
        short = asyncio.create_task(
            call_llm_json(
                "Return JSON",
                "short_background",
                schema,
                timeout_seconds=1,
            )
        )
        # Observe delivery before testing a read deadline. A 10ms total HTTPX
        # timeout can instead fail during TCP setup on a loaded Windows runner.
        await asyncio.wait_for(short_received.wait(), timeout=10)
        with pytest.raises(httpx.ReadTimeout):
            await short
        release_short.set()
        kinds = [
            "foreground_a",
            "background_deep_a",
            "foreground_b",
            "background_deep_b",
        ]
        outputs = await asyncio.gather(
            *(
                call_llm_json("Return JSON", kind, schema, timeout_seconds=2)
                for kind in kinds
            )
        )
        assert outputs == [{"ok": True}] * 4
        assert sorted(received) == sorted(["short_background", *kinds])
        assert ProviderHealthStore.read().state == "healthy"
        assert ProviderHealthStore.read().caller_timeout_count == 1
    finally:
        release_short.set()
        server.close()
        await server.wait_closed()
        if handlers:
            await asyncio.gather(*handlers)
