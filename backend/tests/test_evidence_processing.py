from __future__ import annotations

import asyncio
import importlib.util
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy import delete, func, select, text

from investos.config import settings
from investos.db import async_session_maker, engine
from investos.models.evidence import EvidenceProcessingState, RawEvidence, SourceItem
from investos.models.graph import Edge
from investos.models.source import Source
from investos.services.evidence_processing import EvidenceProcessingService
from investos.workers.extraction import ExtractionWorker

pytestmark = pytest.mark.asyncio(loop_scope="session")


def _load_migration() -> ModuleType:
    path = (
        Path(__file__).parents[1]
        / "alembic"
        / "versions"
        / "4b8d2e6f9a10_add_evidence_processing_states.py"
    )
    spec = importlib.util.spec_from_file_location("evidence_processing_migration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MIGRATION = _load_migration()


async def test_migrated_schema_owns_evidence_processing_lifecycle() -> None:
    def inspect_schema(connection):
        inspector = sa.inspect(connection)
        columns = {
            column["name"]
            for column in inspector.get_columns("evidence_processing_states")
        }
        constraints = {
            constraint["name"]: tuple(constraint["column_names"])
            for constraint in inspector.get_unique_constraints(
                "evidence_processing_states"
            )
        }
        indexes = {
            index["name"]
            for index in inspector.get_indexes("evidence_processing_states")
        }
        return columns, constraints, indexes

    async with engine.connect() as connection:
        columns, constraints, indexes = await connection.run_sync(inspect_schema)

    assert {
        "raw_evidence_id",
        "content_status",
        "transcript_status",
        "extraction_status",
        "investment_object_status",
        "cleanup_status",
        "extraction_attempt_count",
        "next_extraction_attempt_at",
        "last_extraction_attempt_at",
        "extraction_completed_at",
        "persisted_object_count",
        "last_error",
        "history_json",
    } <= columns
    assert constraints["uq_evidence_processing_states_raw_evidence"] == (
        "raw_evidence_id",
    )
    assert {
        "ix_evidence_processing_states_extraction_status",
        "ix_evidence_processing_states_next_extraction_attempt_at",
    } <= indexes


async def test_migration_backfills_completed_and_legacy_degraded_evidence() -> None:
    schema = f"evidence_processing_{uuid4().hex}"
    legacy_id = uuid4()
    completed_id = uuid4()
    missing_id = uuid4()
    historical_missing_id = uuid4()
    operational_id = uuid4()

    async with engine.connect() as connection:
        transaction = await connection.begin()
        try:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            await connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
            await connection.execute(text("""
                    CREATE TABLE raw_evidence (
                        id uuid PRIMARY KEY,
                        source_item_type text NOT NULL,
                        raw_content_ref text,
                        metadata_json jsonb,
                        is_processed boolean NOT NULL,
                        created_at timestamptz NOT NULL DEFAULT now()
                    )
                    """))
            await connection.execute(text("""
                    CREATE TABLE source_items (
                        id uuid PRIMARY KEY,
                        raw_evidence_id uuid NOT NULL,
                        processing_status text NOT NULL
                    )
                    """))
            await connection.execute(text("""
                    CREATE TABLE evidence_processing_states (
                        id uuid PRIMARY KEY,
                        raw_evidence_id uuid NOT NULL,
                        content_status text NOT NULL,
                        transcript_status text NOT NULL,
                        extraction_status text NOT NULL,
                        investment_object_status text NOT NULL,
                        cleanup_status text NOT NULL,
                        extraction_attempt_count integer NOT NULL,
                        next_extraction_attempt_at timestamptz,
                        last_extraction_attempt_at timestamptz,
                        extraction_completed_at timestamptz,
                        persisted_object_count integer NOT NULL,
                        last_error text,
                        history_json jsonb NOT NULL,
                        created_at timestamptz NOT NULL,
                        updated_at timestamptz NOT NULL
                    )
                    """))
            await connection.execute(
                text("""
                    INSERT INTO raw_evidence
                        (id, source_item_type, raw_content_ref, metadata_json,
                         is_processed)
                    VALUES
                        (:legacy_id, 'video_audio_transcript', 'media/transcript.txt',
                         CAST(:legacy_metadata AS jsonb), true),
                        (:completed_id, 'web_research', 'web/report.txt', '{}'::jsonb,
                         true),
                        (:missing_id, 'web_research', 'web/missing.txt',
                         '{"storage_missing": true}'::jsonb, true),
                        (:historical_missing_id, 'web_research', 'web/historical.txt',
                         '{"storage_missing": true}'::jsonb, false),
                        (:operational_id, 'email', 'mail/receipt.txt',
                         '{"skip_extraction": true}'::jsonb, true)
                    """),
                {
                    "legacy_id": legacy_id,
                    "completed_id": completed_id,
                    "missing_id": missing_id,
                    "historical_missing_id": historical_missing_id,
                    "operational_id": operational_id,
                    "legacy_metadata": json.dumps(
                        {
                            "extraction_degraded": True,
                            "raw_media_persisted": False,
                            "representation": "audio_transcript",
                        }
                    ),
                },
            )
            await connection.execute(
                text("""
                    INSERT INTO source_items
                        (id, raw_evidence_id, processing_status)
                    VALUES
                        (:legacy_item_id, :legacy_id, 'processed_with_fallback'),
                        (:completed_item_id, :completed_id, 'processed'),
                        (:missing_item_id, :missing_id, 'missing_raw_content'),
                        (:historical_missing_item_id, :historical_missing_id,
                         'rejected_irrelevant')
                    """),
                {
                    "legacy_item_id": uuid4(),
                    "legacy_id": legacy_id,
                    "completed_item_id": uuid4(),
                    "completed_id": completed_id,
                    "missing_item_id": uuid4(),
                    "missing_id": missing_id,
                    "historical_missing_item_id": uuid4(),
                    "historical_missing_id": historical_missing_id,
                },
            )

            await connection.run_sync(MIGRATION._backfill_existing_evidence)

            rows = (await connection.execute(text("""
                        SELECT raw_evidence_id, content_status, transcript_status, extraction_status,
                               investment_object_status, cleanup_status,
                               extraction_attempt_count, next_extraction_attempt_at,
                               extraction_completed_at
                        FROM evidence_processing_states
                        """))).mappings()
            states = {row["raw_evidence_id"]: row for row in rows}
            legacy_processed = await connection.scalar(
                text("SELECT is_processed FROM raw_evidence WHERE id = :id"),
                {"id": legacy_id},
            )
            missing_processed = await connection.scalar(
                text("SELECT is_processed FROM raw_evidence WHERE id = :id"),
                {"id": missing_id},
            )
            historical_missing_processed = await connection.scalar(
                text("SELECT is_processed FROM raw_evidence WHERE id = :id"),
                {"id": historical_missing_id},
            )
            legacy_source_status = await connection.scalar(
                text(
                    "SELECT processing_status FROM source_items "
                    "WHERE raw_evidence_id = :id"
                ),
                {"id": legacy_id},
            )
            missing_source_status = await connection.scalar(
                text(
                    "SELECT processing_status FROM source_items "
                    "WHERE raw_evidence_id = :id"
                ),
                {"id": missing_id},
            )

            assert legacy_processed is False
            assert states[legacy_id]["transcript_status"] == "completed"
            assert states[legacy_id]["extraction_status"] == "retry_scheduled"
            assert states[legacy_id]["investment_object_status"] == "pending"
            assert states[legacy_id]["cleanup_status"] == "completed"
            assert states[legacy_id]["extraction_attempt_count"] == 1
            assert states[legacy_id]["next_extraction_attempt_at"] is not None
            assert states[legacy_id]["extraction_completed_at"] is None
            assert legacy_source_status == "extraction_retry_scheduled"
            assert states[completed_id]["extraction_status"] == "completed"
            assert states[completed_id]["investment_object_status"] == "completed"
            assert states[completed_id]["extraction_completed_at"] is not None
            assert missing_processed is False
            assert states[missing_id]["content_status"] == "missing"
            assert states[missing_id]["extraction_status"] == "blocked_missing_content"
            assert states[missing_id]["investment_object_status"] == "blocked"
            assert states[missing_id]["next_extraction_attempt_at"] is None
            assert missing_source_status == "missing_raw_content"
            assert states[historical_missing_id]["content_status"] == "missing"
            assert states[historical_missing_id]["extraction_status"] == "completed"
            assert historical_missing_processed is True
            assert states[historical_missing_id]["investment_object_status"] == (
                "quarantined"
            )
            assert states[operational_id]["extraction_status"] == "not_applicable"
            assert states[operational_id]["investment_object_status"] == (
                "not_applicable"
            )
        finally:
            await transaction.rollback()


async def test_concurrent_extraction_claim_runs_provider_once() -> None:
    source_id: UUID | None = None
    evidence_id: UUID | None = None
    extraction_started = asyncio.Event()
    release_extraction = asyncio.Event()

    async def successful_extraction(*_args, **_kwargs):
        extraction_started.set()
        await asyncio.wait_for(release_extraction.wait(), timeout=5)
        return {
            "relevance_assessment": {
                "status": "irrelevant",
                "target_supported": False,
                "reason": "Synthetic evidence does not support the target.",
                "supported_subjects": [],
            },
            "primary_subject": "Example Co.",
            "subject_type": "entity",
            "entity_type": "company",
            "summary": "Synthetic extraction result.",
            "events": [],
            "facts": [],
            "claims": [],
            "fundamental_metrics": [],
            "market_setup_signals": [],
        }

    try:
        async with async_session_maker() as session:
            source = Source(
                name=f"Concurrent extraction {uuid4().hex}",
                source_type="web_research",
            )
            session.add(source)
            await session.flush()
            evidence = RawEvidence(
                source_id=source.id,
                source_item_type="web_research",
                title="Concurrent extraction evidence",
                raw_content_ref="unused/concurrent.txt",
                metadata_json={"subject_name": "Example Co."},
            )
            session.add(evidence)
            await session.flush()
            await EvidenceProcessingService(session).ensure_for_evidence(evidence)
            await session.commit()
            source_id = source.id
            evidence_id = evidence.id

        async with async_session_maker() as first_session:
            first_worker = ExtractionWorker(first_session)
            first_worker.storage.get_object = AsyncMock(return_value=b"Evidence body")
            first_worker._extract_structured_data = AsyncMock(
                side_effect=successful_extraction
            )
            first_task = asyncio.create_task(first_worker.process_evidence(evidence_id))
            await asyncio.wait_for(extraction_started.wait(), timeout=5)

            async with async_session_maker() as second_session:
                second_worker = ExtractionWorker(second_session)
                second_worker.storage.get_object = AsyncMock(
                    return_value=b"Evidence body"
                )
                second_worker._extract_structured_data = AsyncMock(
                    side_effect=AssertionError(
                        "a concurrent claimant must not invoke the provider"
                    )
                )
                second_result = await second_worker.process_evidence(evidence_id)

            release_extraction.set()
            first_result = await first_task

        async with async_session_maker() as session:
            state = (
                await session.execute(
                    select(EvidenceProcessingState).where(
                        EvidenceProcessingState.raw_evidence_id == evidence_id
                    )
                )
            ).scalar_one()
            source_item_count = await session.scalar(
                select(func.count())
                .select_from(SourceItem)
                .where(SourceItem.raw_evidence_id == evidence_id)
            )
            edge_count = await session.scalar(
                select(func.count())
                .select_from(Edge)
                .where(
                    Edge.source_type == "raw_evidence",
                    Edge.source_id == evidence_id,
                    Edge.relationship_type == "processed_into",
                )
            )

        assert second_result is not None
        assert second_result["deferred"] is True
        assert second_result["processing"]["extraction_status"] == "running"
        assert first_result is not None and first_result["quarantined"] is True
        first_worker._extract_structured_data.assert_awaited_once()
        second_worker._extract_structured_data.assert_not_awaited()
        assert state.extraction_status == "completed"
        assert state.extraction_attempt_count == 1
        assert source_item_count == 1
        assert edge_count == 1
    finally:
        if source_id is not None and evidence_id is not None:
            async with async_session_maker() as session:
                await session.execute(
                    delete(Edge).where(
                        Edge.source_type == "raw_evidence",
                        Edge.source_id == evidence_id,
                    )
                )
                await session.execute(
                    delete(SourceItem).where(SourceItem.raw_evidence_id == evidence_id)
                )
                await session.execute(
                    delete(RawEvidence).where(RawEvidence.id == evidence_id)
                )
                await session.execute(delete(Source).where(Source.id == source_id))
                await session.commit()


async def test_next_due_evidence_skips_deferred_and_blocked_work() -> None:
    source_id: UUID | None = None
    evidence_ids: list[UUID] = []
    try:
        async with async_session_maker() as session:
            source = Source(
                name=f"Due evidence selection {uuid4().hex}",
                source_type="web_research",
            )
            session.add(source)
            await session.flush()
            source_id = source.id
            service = EvidenceProcessingService(session)

            future = RawEvidence(
                source_id=source.id,
                source_item_type="web_research",
                title="Future retry",
                raw_content_ref="unused/future.txt",
                created_at=datetime(1999, 1, 1, tzinfo=UTC),
            )
            blocked = RawEvidence(
                source_id=source.id,
                source_item_type="web_research",
                title="Blocked content",
                raw_content_ref="unused/blocked.txt",
                created_at=datetime(1999, 1, 2, tzinfo=UTC),
            )
            due_retry = RawEvidence(
                source_id=source.id,
                source_item_type="web_research",
                title="Due legacy retry",
                raw_content_ref="unused/legacy-retry.txt",
                created_at=datetime(1999, 1, 2, 12, tzinfo=UTC),
            )
            due = RawEvidence(
                source_id=source.id,
                source_item_type="web_research",
                title="Due extraction",
                raw_content_ref="unused/due.txt",
                created_at=datetime(1999, 1, 3, tzinfo=UTC),
            )
            session.add_all([future, blocked, due_retry, due])
            await session.flush()
            evidence_ids = [future.id, blocked.id, due_retry.id, due.id]

            future_state = await service.ensure_for_evidence(future)
            future_state.extraction_status = "retry_scheduled"
            future_state.next_extraction_attempt_at = datetime.now(UTC) + timedelta(
                hours=1
            )
            blocked_state = await service.ensure_for_evidence(blocked)
            service.record_missing_content(
                blocked_state,
                error="Synthetic missing content",
            )
            due_retry_state = await service.ensure_for_evidence(due_retry)
            due_retry_state.extraction_status = "retry_scheduled"
            due_retry_state.next_extraction_attempt_at = datetime.now(UTC) - timedelta(
                minutes=1
            )
            await service.ensure_for_evidence(due)
            await session.commit()

            selected = await service.next_due_evidence()

            assert selected is not None
            assert selected.id == due.id
    finally:
        if source_id is not None:
            async with async_session_maker() as session:
                await session.execute(
                    delete(RawEvidence).where(RawEvidence.id.in_(evidence_ids))
                )
                await session.execute(delete(Source).where(Source.id == source_id))
                await session.commit()


async def test_operator_retry_cannot_reset_running_extraction() -> None:
    source_id: UUID | None = None
    evidence_id: UUID | None = None
    try:
        async with async_session_maker() as session:
            source = Source(
                name=f"Running retry guard {uuid4().hex}",
                source_type="web_research",
            )
            session.add(source)
            await session.flush()
            evidence = RawEvidence(
                source_id=source.id,
                source_item_type="web_research",
                title="Running extraction",
                raw_content_ref="unused/running.txt",
            )
            session.add(evidence)
            await session.flush()
            processing = EvidenceProcessingService(session)
            await processing.ensure_for_evidence(evidence)
            claim = await processing.claim_extraction(evidence)
            assert claim.claimed is True
            await session.commit()
            source_id = source.id
            evidence_id = evidence.id

        async with async_session_maker() as session:
            result = await EvidenceProcessingService(session).schedule_operator_retry(
                evidence_id
            )

        assert result is not None
        assert result["scheduled"] is False
        assert result["reason"] == "already_running"
        assert result["extraction_status"] == "running"
        assert result["extraction_attempt_count"] == 1
    finally:
        if source_id is not None and evidence_id is not None:
            async with async_session_maker() as session:
                await session.execute(
                    delete(RawEvidence).where(RawEvidence.id == evidence_id)
                )
                await session.execute(delete(Source).where(Source.id == source_id))
                await session.commit()


async def test_extraction_failures_back_off_and_exhaust_the_bounded_cycle(
    monkeypatch,
) -> None:
    monkeypatch.setattr(settings, "EVIDENCE_EXTRACTION_MAX_ATTEMPTS", 3)
    monkeypatch.setattr(settings, "EVIDENCE_EXTRACTION_RETRY_BASE_SECONDS", 5)
    monkeypatch.setattr(settings, "EVIDENCE_EXTRACTION_RETRY_MAX_SECONDS", 8)
    state = EvidenceProcessingState(
        raw_evidence_id=uuid4(),
        content_status="completed",
        transcript_status="completed",
        extraction_status="running",
        investment_object_status="pending",
        cleanup_status="completed",
        extraction_attempt_count=1,
        persisted_object_count=0,
        history_json=[],
    )
    service = EvidenceProcessingService(AsyncMock())

    first_recorded_at = datetime.now(UTC)
    assert (
        service.record_extraction_failure(state, error="temporary timeout")
        == "retry_scheduled"
    )
    assert state.next_extraction_attempt_at is not None
    assert timedelta(seconds=4) <= state.next_extraction_attempt_at - first_recorded_at
    assert state.next_extraction_attempt_at - first_recorded_at <= timedelta(seconds=6)

    state.extraction_status = "running"
    state.extraction_attempt_count = 2
    capped_recorded_at = datetime.now(UTC)
    service.record_extraction_failure(state, error="provider unavailable")
    assert state.next_extraction_attempt_at is not None
    assert timedelta(seconds=7) <= state.next_extraction_attempt_at - capped_recorded_at
    assert state.next_extraction_attempt_at - capped_recorded_at <= timedelta(seconds=9)

    state.extraction_status = "running"
    state.extraction_attempt_count = 3
    assert (
        service.record_extraction_failure(state, error="attempts exhausted")
        == "retry_exhausted"
    )
    assert state.next_extraction_attempt_at is None
    assert state.last_error == "attempts exhausted"
    assert [event["status"] for event in state.history_json] == [
        "retry_scheduled",
        "retry_scheduled",
        "retry_exhausted",
    ]


async def test_missing_stored_content_is_blocked_not_processed() -> None:
    source_id: UUID | None = None
    evidence_id: UUID | None = None
    try:
        async with async_session_maker() as session:
            source = Source(
                name=f"Missing content {uuid4().hex}",
                source_type="web_research",
            )
            session.add(source)
            await session.flush()
            evidence = RawEvidence(
                source_id=source.id,
                source_item_type="web_research",
                title="Missing stored content",
                raw_content_ref="missing/source.txt",
            )
            session.add(evidence)
            await session.commit()
            source_id = source.id
            evidence_id = evidence.id

            worker = ExtractionWorker(session)
            worker.storage.get_object = AsyncMock(side_effect=FileNotFoundError)
            result = await worker.process_evidence(evidence.id)

            await session.refresh(evidence)
            state = (
                await session.execute(
                    select(EvidenceProcessingState).where(
                        EvidenceProcessingState.raw_evidence_id == evidence.id
                    )
                )
            ).scalar_one()
            source_item = (
                await session.execute(
                    select(SourceItem).where(SourceItem.raw_evidence_id == evidence.id)
                )
            ).scalar_one()

            assert result is not None and result["missing_raw_content"] is True
            assert evidence.is_processed is False
            assert evidence.metadata_json["storage_missing"] is True
            assert source_item.processing_status == "missing_raw_content"
            assert state.content_status == "missing"
            assert state.extraction_status == "blocked_missing_content"
            assert state.investment_object_status == "blocked"
            assert state.next_extraction_attempt_at is None

            retry = await EvidenceProcessingService(session).schedule_operator_retry(
                evidence.id
            )
            worker.storage.get_object = AsyncMock(return_value=b"Recovered content")
            worker._extract_structured_data = AsyncMock(
                return_value={
                    "relevance_assessment": {
                        "status": "irrelevant",
                        "target_supported": False,
                        "reason": "The restored source does not support the target.",
                        "supported_subjects": [],
                    },
                    "primary_subject": "Example Co.",
                    "subject_type": "entity",
                    "entity_type": "company",
                    "summary": "Recovered source content.",
                    "events": [],
                    "facts": [],
                    "claims": [],
                    "fundamental_metrics": [],
                    "market_setup_signals": [],
                }
            )
            recovered = await worker.process_evidence(evidence.id)
            await session.refresh(evidence)
            await session.refresh(state)

            assert retry is not None and retry["scheduled"] is True
            assert recovered is not None and recovered["quarantined"] is True
            assert evidence.is_processed is True
            assert "storage_missing" not in evidence.metadata_json
            assert state.content_status == "completed"
            assert state.extraction_status == "completed"
            assert any(
                event.get("stage") == "content" and event.get("status") == "completed"
                for event in state.history_json
            )
    finally:
        if source_id is not None and evidence_id is not None:
            async with async_session_maker() as session:
                await session.execute(
                    delete(Edge).where(
                        Edge.source_type == "raw_evidence",
                        Edge.source_id == evidence_id,
                    )
                )
                await session.execute(
                    delete(SourceItem).where(SourceItem.raw_evidence_id == evidence_id)
                )
                await session.execute(
                    delete(RawEvidence).where(RawEvidence.id == evidence_id)
                )
                await session.execute(delete(Source).where(Source.id == source_id))
                await session.commit()
