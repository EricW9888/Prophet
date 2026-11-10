import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from investos.config import settings
from investos.core.provider_health import ProviderHealthStore
from investos.core.work_signals import listen_for_work_signals
from investos.models.evidence import RawEvidence
from investos.models.source import Source
from investos.services.automation import AutomationCoordinator, JobTelemetry


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
