import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from investos.config import settings
from investos.core.provider_health import ProviderHealthStore
from investos.core.work_signals import listen_for_work_signals
from investos.models.evidence import EvidenceProcessingState, RawEvidence
from investos.models.source import Source
from investos.services.automation import AutomationCoordinator, JobTelemetry
from investos.services.evidence_processing import EvidenceProcessingService
from investos.workers.extraction import ExtractionWorker


async def test_only_committed_evidence_wakes_the_existing_worker(monkeypatch):
    monkeypatch.setattr(settings, "AUTOMATION_ENABLED", True)
    coordinator = AutomationCoordinator()
    called = asyncio.Event()

    async def worker():
        called.set()

    coordinator._register_job("evidence_processing", worker, 600, True)
    coordinator.scheduler.start()
    receipts = asyncio.Queue()

    def signal_received(signal):
        receipts.put_nowait(signal)
        if signal != "queue_catchup":
            coordinator.signal_ready_work(signal)

    coordinator._signal_listener = asyncio.create_task(
        listen_for_work_signals(signal_received)
    )
    engine = create_async_engine(settings.SQLALCHEMY_DATABASE_URI, poolclass=NullPool)
    source_id = uuid4()
    try:
        assert await asyncio.wait_for(receipts.get(), timeout=5) == "queue_catchup"
        async with AsyncSession(engine) as session:
            session.add(
                Source(
                    id=source_id,
                    name="Synthetic work signal source",
                    source_type="manual",
                )
            )
            await session.commit()
            session.add(
                RawEvidence(
                    source_id=source_id,
                    source_item_type="manual_note",
                    title="Rolled back",
                )
            )
            await session.flush()
            await session.rollback()
            session.add(
                RawEvidence(
                    source_id=source_id,
                    source_item_type="conversation_turn",
                    title="Conversation only",
                )
            )
            await session.commit()
            assert receipts.empty()
            session.add_all(
                [
                    RawEvidence(
                        source_id=source_id,
                        source_item_type="manual_note",
                        title=f"Committed {index}",
                    )
                    for index in range(2)
                ]
            )
            await session.flush()
            assert not called.is_set()
            await session.commit()
            assert (
                await asyncio.wait_for(receipts.get(), timeout=5) == "evidence_arrived"
            )
            await asyncio.wait_for(called.wait(), timeout=5)
            assert (
                coordinator.telemetry["evidence_processing"].last_trigger
                == "signal:evidence_arrived"
            )
            assert receipts.empty()
    finally:
        await coordinator.shutdown()
        async with AsyncSession(engine) as session:
            await session.execute(
                delete(RawEvidence).where(RawEvidence.source_id == source_id)
            )
            await session.execute(delete(Source).where(Source.id == source_id))
            await session.commit()
        await engine.dispose()


async def test_work_signals_preserve_provider_recovery_and_paid_work_limits(
    monkeypatch,
):
    coordinator = AutomationCoordinator()
    coordinator.telemetry["provider_health_probe"] = JobTelemetry(
        "provider_health_probe", 30, last_status="deferred"
    )
    monkeypatch.setattr(ProviderHealthStore, "public_status", lambda: {"state": "open"})
    monkeypatch.setattr(
        "investos.services.automation.probe_llm_provider",
        AsyncMock(return_value={"state": "healthy"}),
    )
    signal = Mock()
    monkeypatch.setattr(coordinator, "signal_ready_work", signal)
    await coordinator._run_provider_health_probe()
    signal.assert_called_once_with("provider_recovered")
    assert coordinator.telemetry["provider_health_probe"].last_status == "ok"

    recent = datetime.now(UTC)
    coordinator._pending_signals["research_loop"] = "question_actionable"
    coordinator.telemetry["research_loop"] = JobTelemetry("research_loop", 300)
    coordinator._last_model_attempt_at["research_loop"] = recent
    monkeypatch.setattr(
        coordinator.scheduler,
        "get_job",
        lambda _: Mock(next_run_time=recent + timedelta(hours=1)),
    )
    modify = Mock()
    monkeypatch.setattr(coordinator.scheduler, "modify_job", modify)
    coordinator._expedite_pending_job("research_loop")
    modify.assert_called_once_with(
        "research_loop", next_run_time=recent + timedelta(minutes=5)
    )


async def test_extraction_drains_ready_work_after_one_wake_and_yields_on_deferral(
    monkeypatch,
):
    monkeypatch.setattr(settings, "AUTOMATION_ENABLED", True)
    monkeypatch.setattr(
        ProviderHealthStore, "public_status", lambda: {"state": "healthy"}
    )
    coordinator = AutomationCoordinator()
    first = SimpleNamespace(id=uuid4())
    second = SimpleNamespace(id=uuid4())
    waiting = [first, second]
    attempts = []
    deferred = asyncio.Event()

    async def next_due(_service, *, model_available, prefer_recent_publisher=False):
        assert model_available
        return waiting[0]

    async def extract(_worker, evidence_id):
        attempts.append(evidence_id)
        if evidence_id == first.id:
            waiting.pop(0)
            return {"processing": {"extraction_status": "completed"}}
        assert evidence_id == second.id
        deferred.set()
        return {"deferred": True, "processing": {"extraction_status": "pending"}}

    monkeypatch.setattr(EvidenceProcessingService, "next_due_evidence", next_due)
    monkeypatch.setattr(ExtractionWorker, "process_evidence", extract)
    monkeypatch.setattr(coordinator, "_log_job_action", Mock())
    coordinator._register_job(
        "evidence_processing", coordinator._run_evidence_processing, 120, True
    )
    coordinator.scheduler.start()
    try:
        coordinator.signal_ready_work("evidence_arrived")
        await asyncio.wait_for(deferred.wait(), timeout=10)
        telemetry = coordinator.telemetry["evidence_processing"]
        assert telemetry.last_trigger == "signal:evidence_backlog"
        assert telemetry.last_status == "deferred"
        assert attempts == [first.id, second.id]
        assert "evidence_processing" not in coordinator._pending_signals
    finally:
        await coordinator.shutdown()


@pytest.mark.parametrize("object_count", [0, 1])
async def test_committed_extraction_wakes_planner_only_when_it_retains_objects(
    monkeypatch, object_count
):
    monkeypatch.setattr(settings, "AUTOMATION_ENABLED", True)
    monkeypatch.setattr(
        ProviderHealthStore, "public_status", lambda: {"state": "healthy"}
    )
    monkeypatch.setattr(ProviderHealthStore, "can_admit_work", lambda health: True)
    coordinator = AutomationCoordinator()
    called = asyncio.Event()

    async def planner():
        called.set()

    coordinator._register_job("strategist_cycle", planner, 600, True)
    coordinator.scheduler.start()
    receipts = asyncio.Queue()

    def received(signal):
        receipts.put_nowait(signal)
        if signal != "queue_catchup":
            coordinator.signal_ready_work(signal)

    coordinator._signal_listener = asyncio.create_task(
        listen_for_work_signals(received)
    )
    engine = create_async_engine(settings.SQLALCHEMY_DATABASE_URI, poolclass=NullPool)
    source_id, evidence_id, state_id = uuid4(), uuid4(), uuid4()
    try:
        assert await asyncio.wait_for(receipts.get(), timeout=5) == "queue_catchup"
        async with AsyncSession(engine) as session:
            session.add(
                Source(id=source_id, name="Synthetic planner wake", source_type="news")
            )
            await session.flush()
            session.add(
                RawEvidence(
                    id=evidence_id,
                    source_id=source_id,
                    source_item_type="news",
                    is_processed=False,
                )
            )
            await session.flush()
            state = EvidenceProcessingState(
                id=state_id,
                raw_evidence_id=evidence_id,
                extraction_status="pending",
                persisted_object_count=0,
            )
            session.add(state)
            await session.commit()
            assert (
                await asyncio.wait_for(receipts.get(), timeout=5) == "evidence_arrived"
            )
            state.extraction_status = "completed"
            state.persisted_object_count = object_count
            await session.flush()
            assert not called.is_set()
            await session.rollback()
            state = await session.get(EvidenceProcessingState, state_id)
            state.extraction_status = "completed"
            state.persisted_object_count = object_count
            await session.commit()
            await session.execute(
                select(func.pg_notify("prophet_work_ready", "test_barrier"))
            )
            await session.commit()
            if object_count:
                assert (
                    await asyncio.wait_for(receipts.get(), timeout=5)
                    == "evidence_ready"
                )
                await asyncio.wait_for(called.wait(), timeout=5)
                assert (
                    coordinator.telemetry["strategist_cycle"].last_trigger
                    == "signal:evidence_ready"
                )
            assert await asyncio.wait_for(receipts.get(), timeout=5) == "test_barrier"
            if not object_count:
                assert not called.is_set()
                assert "strategist_cycle" not in coordinator._pending_signals
            else:
                called.clear()
                state.extraction_status = "completed"
                await session.commit()
                await session.execute(
                    select(func.pg_notify("prophet_work_ready", "test_barrier"))
                )
                await session.commit()
                assert (
                    await asyncio.wait_for(receipts.get(), timeout=5) == "test_barrier"
                )
                assert not called.is_set()
                assert "strategist_cycle" not in coordinator._pending_signals
    finally:
        await coordinator.shutdown()
        async with AsyncSession(engine) as session:
            await session.execute(
                delete(EvidenceProcessingState).where(
                    EvidenceProcessingState.id == state_id
                )
            )
            await session.execute(
                delete(RawEvidence).where(RawEvidence.id == evidence_id)
            )
            await session.execute(delete(Source).where(Source.id == source_id))
            await session.commit()
        await engine.dispose()
