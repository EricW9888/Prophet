"""An interrupted provider must not consume or replace a bounded scan plan."""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select

from investos.core.provider_health import LLMProviderCooldownError
from investos.models.opportunity import (
    OpportunityDiscoveryRun,
    OpportunityUniverseMember,
)
from investos.services.opportunity import OpportunityDiscoveryService
from investos.services.runtime_settings import RuntimeSettingsStore
from investos.services.security_catalog import SecurityCatalogService
from tests.test_investor_workflows import workflow_session


async def test_occupied_discovery_lease_reports_when_the_same_run_can_resume(
    monkeypatch,
):
    service = OpportunityDiscoveryService(None)
    run = OpportunityDiscoveryRun(
        id=uuid4(), status="running", heartbeat_at=datetime.now(UTC)
    )
    monkeypatch.setattr(service, "_acquire_run", AsyncMock(return_value=(run, False)))
    response = await service.run_discovery()
    assert response["retry_at"] == run.heartbeat_at + service.ACTIVE_RUN_STALE_AFTER
    assert response["status"] == "busy"


async def test_resume_of_completed_discovery_never_creates_another_run(
    workflow_session, monkeypatch
):
    run = OpportunityDiscoveryRun(
        status="completed",
        captured_at=datetime.now(UTC),
        started_at=datetime.now(UTC),
        heartbeat_at=datetime.now(UTC),
    )
    workflow_session.add(run)
    await workflow_session.commit()
    service = OpportunityDiscoveryService(workflow_session)
    create = AsyncMock(
        side_effect=AssertionError("Resuming must not create a new scan")
    )
    monkeypatch.setattr(service, "_acquire_run", create)
    response = await service.run_discovery(resume_run_id=run.id)
    assert response["status"] == "skipped"
    assert response["detail"] == "discovery_run_no_longer_pending"
    create.assert_not_awaited()


@pytest.mark.parametrize(
    "interruption", ["query", "http_429", "evaluation", "pause", "unrelated_error"]
)
async def test_deferred_scan_resumes_its_plan_and_evaluates_all_completed_subjects(
    workflow_session, monkeypatch, interruption
):
    session = workflow_session
    runtime = RuntimeSettingsStore.load()
    runtime.opportunity_discovery.universe_scope = "manual"
    runtime.opportunity_discovery.max_subjects_per_run = 2
    monkeypatch.setattr(RuntimeSettingsStore, "load", lambda: runtime)
    for ticker in ("SYNREC1", "SYNREC2"):
        security = await SecurityCatalogService(session).resolve_or_create_equity(
            ticker=ticker, entity_name=f"Synthetic recovery {ticker}"
        )
        session.add(
            OpportunityUniverseMember(
                security_id=security.id,
                entity_id=security.entity_id,
                enabled=True,
                priority=0.5,
                source="manual",
            )
        )
    await session.commit()
    service = OpportunityDiscoveryService(session)
    service.ACTIVE_KEY = f"synthetic-recovery-{uuid4()}"
    calls, evaluations, clocks = [], [], []
    interrupted = provider_open = False

    async def query(*, security, captured_at, **_):
        nonlocal interrupted, provider_open
        calls.append(security.ticker)
        clocks.append(captured_at)
        if not interrupted and len(calls) == 2 and interruption != "evaluation":
            interrupted = True
            if interruption == "unrelated_error":
                provider_open = True
                raise ValueError("Synthetic application failure")
            if interruption == "http_429":
                provider_open = True
                request = httpx.Request("POST", "https://example.test/generate")
                raise httpx.HTTPStatusError(
                    "Rate limited",
                    request=request,
                    response=httpx.Response(429, request=request),
                )
            raise LLMProviderCooldownError("Synthetic shared provider cooldown")
        return "Synthetic query", "Synthetic research goal"

    def require_available(*_args, **_kwargs):
        if provider_open:
            raise LLMProviderCooldownError("Synthetic shared provider cooldown")

    async def evaluate(*, run, members):
        nonlocal interrupted
        if interruption == "evaluation" and not interrupted:
            interrupted = True
            raise LLMProviderCooldownError("Synthetic evaluation cooldown")
        evaluations.append({security.ticker for _, security, _ in members})

    monkeypatch.setattr(service, "_research_query", query)
    monkeypatch.setattr(service, "_evaluate_run", evaluate)
    monkeypatch.setattr(service, "evaluate_due_outcomes", AsyncMock(return_value={}))
    monkeypatch.setattr(service, "_refresh_provider_telemetry", AsyncMock())
    monkeypatch.setattr(
        "investos.core.provider_health.ProviderHealthStore.require_available",
        require_available,
    )
    monkeypatch.setattr(
        "investos.services.opportunity.ResearchService.run_ad_hoc_request",
        AsyncMock(return_value=SimpleNamespace(reason="research_completed")),
    )

    first = await service.run_discovery()
    assert clocks and all(clock == first["captured_at"] for clock in clocks)
    if interruption == "unrelated_error":
        assert first["status"] == "partial" and first["failed_count"] == 1
        assert first["completed_at"] is not None
        assert "Synthetic application failure" in first["failures"][0]["error"]
        return
    assert first["status"] == "deferred" and first["completed_at"] is None
    assert first["planned_count"] == 2 and first["failed_count"] == 0
    assert first["inspected_count"] == (2 if interruption == "evaluation" else 1)
    assert len(first["remaining_member_ids"]) == (
        0 if interruption == "evaluation" else 1
    )
    run = await session.get(OpportunityDiscoveryRun, first["id"])
    assert run.active_key == service.ACTIVE_KEY and run.owner_token is None
    if interruption == "pause":
        member = await session.scalar(
            select(OpportunityUniverseMember).where(
                OpportunityUniverseMember.id.in_(run.remaining_member_ids_json)
            )
        )
        member.enabled = False
        await session.commit()
    provider_open = False
    second = await service.run_discovery()
    assert all(clock == first["captured_at"] for clock in clocks)
    assert second["id"] == first["id"] and second["captured_at"] == first["captured_at"]
    assert second["status"] == "completed" and second["remaining_member_ids"] == []
    assert second["planned_count"] == 2 and second["failed_count"] == 0
    assert second["inspected_count"] == (1 if interruption == "pause" else 2)
    assert calls.count("SYNREC1") == 1
    assert evaluations == [
        {"SYNREC1"} if interruption == "pause" else {"SYNREC1", "SYNREC2"}
    ]
    assert run.active_key is None and second["completed_at"] is not None


async def test_cancelled_inspection_preserves_the_unfinished_subject(
    workflow_session, monkeypatch
):
    session = workflow_session
    service = OpportunityDiscoveryService(session)
    run = OpportunityDiscoveryRun(
        status="running",
        captured_at=datetime.now(UTC),
        started_at=datetime.now(UTC),
        heartbeat_at=datetime.now(UTC),
        remaining_member_ids_json=[str(uuid4())],
    )
    session.add(run)
    await session.flush()
    member = SimpleNamespace(id=run.remaining_member_ids_json[0])
    monkeypatch.setattr(
        service, "_research_query", AsyncMock(side_effect=asyncio.CancelledError())
    )
    monkeypatch.setattr(service, "_refresh_provider_telemetry", AsyncMock())
    with pytest.raises(asyncio.CancelledError):
        await service._inspect_member(
            run=run, member=member, security=None, entity=None
        )
    assert run.remaining_member_ids_json == [member.id]
    assert run.inspected_count == run.failed_count == 0
