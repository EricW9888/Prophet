"""Persistent degradation, actual probe leases and domain counter reconciliation."""

import asyncio
from datetime import UTC, datetime, timedelta
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
)
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


async def test_open_circuit_does_not_call_provider(monkeypatch):
    open_health()
    request = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr("investos.core.llm._call_nvidia_json", request)
    with pytest.raises(LLMProviderCooldownError):
        await call_llm_json("system", "user", {"type": "object"})
    request.assert_not_awaited()


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
    assert calls[0]["timeout_seconds"] == 15
    assert ProviderHealthStore.read().state == "healthy"
    assert ProviderHealthStore.require_available() >= 1


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
    assert ProviderHealthStore.read().state == "healthy"


def test_corrupt_health_storage_defers_instead_of_claiming_healthy():
    ProviderHealthStore._path().write_text("broken", encoding="utf-8")
    assert ProviderHealthStore.public_status()["state"] == "storage_unavailable"
    with pytest.raises(LLMProviderCooldownError):
        ProviderHealthStore.require_available()


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
