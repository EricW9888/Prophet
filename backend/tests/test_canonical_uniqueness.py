from __future__ import annotations

import asyncio
import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy import delete, func, select, text

from investos.db import async_session_maker, engine
from investos.models.conclusion import ConclusionState
from investos.models.coverage import CoverageMap
from investos.models.graph import Edge
from investos.models.knowledge_mutation import KnowledgeMutation
from investos.models.reasoning import ReasoningRun
from investos.services.canonical_state import CanonicalStateService
from investos.services.graph_edge_state import GraphEdgeStateService

pytestmark = pytest.mark.asyncio(loop_scope="module")

EXPECTED_CONSTRAINTS = {
    "conclusion_states": {
        "uq_conclusion_states_subject": ("subject_type", "subject_id")
    },
    "coverage_maps": {"uq_coverage_maps_subject": ("subject_type", "subject_id")},
    "edges": {
        "uq_edges_relationship_tuple": (
            "source_type",
            "source_id",
            "target_type",
            "target_id",
            "relationship_type",
        )
    },
}


def _load_migration() -> ModuleType:
    path = (
        Path(__file__).parents[1]
        / "alembic"
        / "versions"
        / "c8f3e1d2a7b4_restore_canonical_uniqueness.py"
    )
    spec = importlib.util.spec_from_file_location(
        "canonical_uniqueness_migration", path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MIGRATION = _load_migration()


class _InitialMissBarrier:
    def __init__(self, participants: int = 2) -> None:
        self.participants = participants
        self.arrivals = 0
        self.lock = asyncio.Lock()
        self.ready = asyncio.Event()

    async def wait(self, value):
        if value is not None:
            return value
        async with self.lock:
            self.arrivals += 1
            if self.arrivals >= self.participants:
                self.ready.set()
        await asyncio.wait_for(self.ready.wait(), timeout=5)
        return value


async def _seed_reasoning_run() -> UUID:
    async with async_session_maker() as session:
        run = ReasoningRun(run_type="test", model_used="test")
        session.add(run)
        await session.commit()
        return run.id


async def _delete_subject_state(
    *, subject_type: str, subject_id: UUID, reasoning_run_id: UUID | None = None
) -> None:
    async with async_session_maker() as session:
        await session.execute(
            delete(ConclusionState).where(
                ConclusionState.subject_type == subject_type,
                ConclusionState.subject_id == subject_id,
            )
        )
        await session.execute(
            delete(CoverageMap).where(
                CoverageMap.subject_type == subject_type,
                CoverageMap.subject_id == subject_id,
            )
        )
        if reasoning_run_id is not None:
            await session.execute(
                delete(ReasoningRun).where(ReasoningRun.id == reasoning_run_id)
            )
        await session.commit()


async def test_migrated_schema_has_all_canonical_unique_constraints() -> None:
    def inspect_constraints(connection):
        inspector = sa.inspect(connection)
        return {
            table_name: {
                constraint["name"]: tuple(constraint["column_names"])
                for constraint in inspector.get_unique_constraints(table_name)
            }
            for table_name in EXPECTED_CONSTRAINTS
        }

    async with engine.connect() as connection:
        actual = await connection.run_sync(inspect_constraints)

    for table_name, expected in EXPECTED_CONSTRAINTS.items():
        for constraint_name, columns in expected.items():
            assert actual[table_name].get(constraint_name) == columns


async def test_canonical_state_ensure_is_idempotent() -> None:
    subject_type = "test"
    subject_id = uuid4()
    reasoning_run_id = await _seed_reasoning_run()
    conclusion_factory_calls = 0
    coverage_factory_calls = 0

    def create_conclusion() -> ConclusionState:
        nonlocal conclusion_factory_calls
        conclusion_factory_calls += 1
        return ConclusionState(
            subject_type=subject_type,
            subject_id=subject_id,
            current_thesis_summary="Test conclusion",
            current_stance="no_view",
            confidence_band="very_low",
            reasoning_run_id=reasoning_run_id,
        )

    def create_coverage() -> CoverageMap:
        nonlocal coverage_factory_calls
        coverage_factory_calls += 1
        return CoverageMap(
            subject_type=subject_type,
            subject_id=subject_id,
            evidence_class_coverage_json={},
        )

    try:
        async with async_session_maker() as session:
            service = CanonicalStateService(session)
            first_conclusion = await service.ensure_conclusion_state(
                subject_type=subject_type,
                subject_id=subject_id,
                create=create_conclusion,
            )
            second_conclusion = await service.ensure_conclusion_state(
                subject_type=subject_type,
                subject_id=subject_id,
                create=create_conclusion,
            )
            first_coverage = await service.ensure_coverage_map(
                subject_type=subject_type,
                subject_id=subject_id,
                create=create_coverage,
            )
            second_coverage = await service.ensure_coverage_map(
                subject_type=subject_type,
                subject_id=subject_id,
                create=create_coverage,
            )
            await session.commit()

        assert first_conclusion.id == second_conclusion.id
        assert first_coverage.id == second_coverage.id
        assert conclusion_factory_calls == 1
        assert coverage_factory_calls == 1
    finally:
        await _delete_subject_state(
            subject_type=subject_type,
            subject_id=subject_id,
            reasoning_run_id=reasoning_run_id,
        )


async def test_concurrent_canonical_state_creation_returns_one_row() -> None:
    subject_type = "test"
    subject_id = uuid4()
    reasoning_run_id = await _seed_reasoning_run()
    conclusion_barrier = _InitialMissBarrier()
    coverage_barrier = _InitialMissBarrier()

    async def create_conclusion() -> UUID:
        async with async_session_maker() as session:
            service = CanonicalStateService(session)
            get_current = service.get_conclusion_state

            async def synchronized_get(**kwargs):
                return await conclusion_barrier.wait(await get_current(**kwargs))

            service.get_conclusion_state = synchronized_get
            async with session.begin():
                state = await service.ensure_conclusion_state(
                    subject_type=subject_type,
                    subject_id=subject_id,
                    create=lambda: ConclusionState(
                        subject_type=subject_type,
                        subject_id=subject_id,
                        current_thesis_summary="Concurrent conclusion",
                        current_stance="no_view",
                        confidence_band="very_low",
                        reasoning_run_id=reasoning_run_id,
                    ),
                )
                return state.id

    async def create_coverage() -> UUID:
        async with async_session_maker() as session:
            service = CanonicalStateService(session)
            get_current = service.get_coverage_map

            async def synchronized_get(**kwargs):
                return await coverage_barrier.wait(await get_current(**kwargs))

            service.get_coverage_map = synchronized_get
            async with session.begin():
                coverage = await service.ensure_coverage_map(
                    subject_type=subject_type,
                    subject_id=subject_id,
                    create=lambda: CoverageMap(
                        subject_type=subject_type,
                        subject_id=subject_id,
                        evidence_class_coverage_json={},
                    ),
                )
                return coverage.id

    try:
        conclusion_ids = await asyncio.gather(create_conclusion(), create_conclusion())
        coverage_ids = await asyncio.gather(create_coverage(), create_coverage())

        async with async_session_maker() as session:
            conclusion_count = await session.scalar(
                select(func.count())
                .select_from(ConclusionState)
                .where(
                    ConclusionState.subject_type == subject_type,
                    ConclusionState.subject_id == subject_id,
                )
            )
            coverage_count = await session.scalar(
                select(func.count())
                .select_from(CoverageMap)
                .where(
                    CoverageMap.subject_type == subject_type,
                    CoverageMap.subject_id == subject_id,
                )
            )

        assert len(set(conclusion_ids)) == 1
        assert len(set(coverage_ids)) == 1
        assert conclusion_count == 1
        assert coverage_count == 1
    finally:
        await _delete_subject_state(
            subject_type=subject_type,
            subject_id=subject_id,
            reasoning_run_id=reasoning_run_id,
        )


async def test_concurrent_graph_edge_creation_is_idempotent() -> None:
    source_id = uuid4()
    target_id = uuid4()
    barrier = _InitialMissBarrier()

    async def create_edge(confidence: float) -> UUID:
        async with async_session_maker() as session:
            service = GraphEdgeStateService(session)
            get_current = service._get_edge

            async def synchronized_get(**kwargs):
                return await barrier.wait(await get_current(**kwargs))

            service._get_edge = synchronized_get
            async with session.begin():
                edge, _ = await service.ensure_edge(
                    source_type="test_source",
                    source_id=source_id,
                    target_type="test_target",
                    target_id=target_id,
                    relationship_type="test_relationship",
                    confidence=confidence,
                    reasoning=f"confidence {confidence}",
                )
                return edge.id

    try:
        edge_ids = await asyncio.gather(create_edge(0.4), create_edge(0.9))
        async with async_session_maker() as session:
            edges = list(
                (
                    await session.execute(
                        select(Edge).where(
                            Edge.source_type == "test_source",
                            Edge.source_id == source_id,
                            Edge.target_type == "test_target",
                            Edge.target_id == target_id,
                            Edge.relationship_type == "test_relationship",
                        )
                    )
                )
                .scalars()
                .all()
            )

        assert len(set(edge_ids)) == 1
        assert len(edges) == 1
        assert edges[0].confidence == pytest.approx(0.9)
    finally:
        async with async_session_maker() as session:
            edge_ids_to_delete = select(Edge.id).where(
                Edge.source_type == "test_source",
                Edge.source_id == source_id,
                Edge.target_type == "test_target",
                Edge.target_id == target_id,
                Edge.relationship_type == "test_relationship",
            )
            await session.execute(
                delete(KnowledgeMutation).where(
                    KnowledgeMutation.node_type == "edge",
                    KnowledgeMutation.node_id.in_(edge_ids_to_delete),
                )
            )
            await session.execute(
                delete(Edge).where(
                    Edge.source_type == "test_source",
                    Edge.source_id == source_id,
                    Edge.target_type == "test_target",
                    Edge.target_id == target_id,
                    Edge.relationship_type == "test_relationship",
                )
            )
            await session.commit()


async def test_migration_reconciles_existing_duplicates_and_references() -> None:
    schema = f"canonical_reconciliation_{uuid4().hex}"
    now = datetime.now(UTC)
    subject_id = uuid4()
    old_coverage_id, new_coverage_id = uuid4(), uuid4()
    old_conclusion_id, new_conclusion_id = uuid4(), uuid4()
    weak_edge_id, strong_edge_id = uuid4(), uuid4()

    async with engine.connect() as connection:
        transaction = await connection.begin()
        try:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            await connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
            table_definitions = (
                """
                CREATE TABLE coverage_maps (
                    id uuid PRIMARY KEY,
                    subject_type text NOT NULL,
                    subject_id uuid NOT NULL,
                    last_computed_at timestamptz
                )
                """,
                """
                CREATE TABLE missing_evidence_classes (
                    id uuid PRIMARY KEY,
                    coverage_map_id uuid NOT NULL REFERENCES coverage_maps(id)
                )
                """,
                """
                CREATE TABLE unresolved_questions (
                    id uuid PRIMARY KEY,
                    coverage_map_id uuid NOT NULL REFERENCES coverage_maps(id)
                )
                """,
                """
                CREATE TABLE conclusion_states (
                    id uuid PRIMARY KEY,
                    subject_type text NOT NULL,
                    subject_id uuid NOT NULL,
                    last_updated_at timestamptz,
                    last_verified_at timestamptz
                )
                """,
                """
                CREATE TABLE conclusion_revisions (
                    id uuid PRIMARY KEY,
                    conclusion_state_id uuid NOT NULL REFERENCES conclusion_states(id)
                )
                """,
                """
                CREATE TABLE verification_runs (
                    id uuid PRIMARY KEY,
                    conclusion_state_id uuid NOT NULL REFERENCES conclusion_states(id)
                )
                """,
                """
                CREATE TABLE decision_journals (
                    id uuid PRIMARY KEY,
                    conclusion_state_id uuid REFERENCES conclusion_states(id)
                )
                """,
                """
                CREATE TABLE theses (
                    id uuid PRIMARY KEY,
                    conclusion_state_id uuid REFERENCES conclusion_states(id)
                )
                """,
                """
                CREATE TABLE edges (
                    id uuid PRIMARY KEY,
                    source_type text NOT NULL,
                    source_id uuid NOT NULL,
                    target_type text NOT NULL,
                    target_id uuid NOT NULL,
                    relationship_type text NOT NULL,
                    confidence double precision,
                    created_at timestamptz
                )
                """,
            )
            for definition in table_definitions:
                await connection.execute(text(definition))
            await connection.execute(
                text("""
                    INSERT INTO coverage_maps
                        (id, subject_type, subject_id, last_computed_at)
                    VALUES
                        (:old_id, 'entity', :subject_id, :old_time),
                        (:new_id, 'entity', :subject_id, :new_time)
                    """),
                {
                    "old_id": old_coverage_id,
                    "new_id": new_coverage_id,
                    "subject_id": subject_id,
                    "old_time": now - timedelta(days=1),
                    "new_time": now,
                },
            )
            await connection.execute(
                text(
                    "INSERT INTO missing_evidence_classes (id, coverage_map_id) "
                    "VALUES (:id, :coverage_id)"
                ),
                {"id": uuid4(), "coverage_id": old_coverage_id},
            )
            await connection.execute(
                text(
                    "INSERT INTO unresolved_questions (id, coverage_map_id) "
                    "VALUES (:id, :coverage_id)"
                ),
                {"id": uuid4(), "coverage_id": old_coverage_id},
            )
            await connection.execute(
                text("""
                    INSERT INTO conclusion_states
                        (id, subject_type, subject_id, last_updated_at, last_verified_at)
                    VALUES
                        (:old_id, 'entity', :subject_id, :old_time, :old_time),
                        (:new_id, 'entity', :subject_id, :new_time, :new_time)
                    """),
                {
                    "old_id": old_conclusion_id,
                    "new_id": new_conclusion_id,
                    "subject_id": subject_id,
                    "old_time": now - timedelta(days=1),
                    "new_time": now,
                },
            )
            for table_name in (
                "conclusion_revisions",
                "verification_runs",
                "decision_journals",
                "theses",
            ):
                await connection.execute(
                    text(
                        f"INSERT INTO {table_name} (id, conclusion_state_id) "
                        "VALUES (:id, :conclusion_id)"
                    ),
                    {"id": uuid4(), "conclusion_id": old_conclusion_id},
                )
            await connection.execute(
                text("""
                    INSERT INTO edges
                        (id, source_type, source_id, target_type, target_id,
                         relationship_type, confidence, created_at)
                    VALUES
                        (:weak_id, 'claim', :subject_id, 'entity', :target_id,
                         'supports', 0.4, :new_time),
                        (:strong_id, 'claim', :subject_id, 'entity', :target_id,
                         'supports', 0.9, :old_time)
                    """),
                {
                    "weak_id": weak_edge_id,
                    "strong_id": strong_edge_id,
                    "subject_id": subject_id,
                    "target_id": uuid4(),
                    "old_time": now - timedelta(days=1),
                    "new_time": now,
                },
            )

            def reconcile(sync_connection) -> None:
                MIGRATION._reconcile_coverage_duplicates(sync_connection)
                MIGRATION._reconcile_conclusion_duplicates(sync_connection)
                MIGRATION._reconcile_edge_duplicates(sync_connection)

            await connection.run_sync(reconcile)

            assert (
                await connection.scalar(text("SELECT id FROM coverage_maps"))
            ) == new_coverage_id
            assert (
                await connection.scalar(
                    text("SELECT coverage_map_id FROM missing_evidence_classes")
                )
            ) == new_coverage_id
            assert (
                await connection.scalar(
                    text("SELECT coverage_map_id FROM unresolved_questions")
                )
            ) == new_coverage_id
            assert (
                await connection.scalar(text("SELECT id FROM conclusion_states"))
            ) == new_conclusion_id
            for table_name in (
                "conclusion_revisions",
                "verification_runs",
                "decision_journals",
                "theses",
            ):
                assert (
                    await connection.scalar(
                        text(f"SELECT conclusion_state_id FROM {table_name}")
                    )
                ) == new_conclusion_id
            assert (
                await connection.scalar(text("SELECT id FROM edges"))
            ) == strong_edge_id
        finally:
            await transaction.rollback()
