"""Synthetic uncertainties exercise the actual PostgreSQL work lifecycle."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from investos.api.routes.automation import (
    QuestionDecision,
    question_backlog,
    question_decision,
    question_history,
)
from investos.config import settings
from investos.models.coverage import (
    CoverageMap,
    QuestionHistory,
    Resolution,
    UnresolvedQuestion,
)
from investos.models.entity import Entity, Security
from investos.models.evidence import EvidenceProcessingState, RawEvidence, SourceItem
from investos.models.portfolio import Position
from investos.models.source import Source
from investos.services.automation import AutomationCoordinator
from investos.services.question_lifecycle import QuestionLifecycleService
from investos.services.research import ResearchRunResult, ResearchService
from investos.workers.coverage import CoverageWorker


@pytest.fixture
async def question_session(monkeypatch):
    monkeypatch.setattr(
        ResearchService, "_log_research_action", staticmethod(lambda **kwargs: None)
    )
    engine = create_async_engine(settings.SQLALCHEMY_DATABASE_URI, poolclass=NullPool)
    async with engine.connect() as connection:
        transaction = await connection.begin()
        async with AsyncSession(
            bind=connection,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        ) as session:
            yield session
        await transaction.rollback()
    await engine.dispose()


async def test_backlog_pages_eligible_work_separately_from_retained_history(
    question_session,
):
    session = question_session
    coverage, _ = await tracked_scope(session)
    service = QuestionLifecycleService(session)
    questions = []
    for index in range(25):
        questions.append(
            await service.ensure_question(
                coverage, prompt(mechanism=f"Synthetic distinct mechanism {index}"), []
            )
        )
    first = await question_backlog(view="active", offset=0, session=session)
    second = await question_backlog(view="active", offset=20, session=session)
    assert first["total"] == second["total"] == 25
    assert len(first["items"]) == 20 and len(second["items"]) == 5
    assert not {row["id"] for row in first["items"]} & {
        row["id"] for row in second["items"]
    }
    await question_decision(
        questions[0].id,
        QuestionDecision(action="retire", reason="Owner selected retirement"),
        session,
    )
    historical = await question_backlog(view="history", offset=0, session=session)
    assert historical["total"] == 1 and historical["items"][0]["id"] == str(
        questions[0].id
    )
    assert historical["eligible_active"] == 24 and historical["historical"] == 25


async def test_database_rejects_a_second_root_for_one_uncertainty(question_session):
    from sqlalchemy.exc import IntegrityError

    session = question_session
    coverage, _ = await tracked_scope(session)
    question = await QuestionLifecycleService(session).ensure_question(
        coverage, prompt(), []
    )
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            session.add(
                UnresolvedQuestion(
                    coverage_map_id=coverage.id,
                    question_text="Another paraphrase",
                    semantic_key=question.semantic_key,
                )
            )
            await session.flush()


async def tracked_scope(session, subject_type="entity"):
    entity = Entity(name="Synthetic Memory Company", entity_type="company")
    session.add(entity)
    await session.flush()
    security = Security(
        entity_id=entity.id,
        ticker=f"Q{uuid4().hex[:8]}",
        asset_class="equity",
        instrument_type="common_stock",
    )
    session.add(security)
    await session.flush()
    position = Position(
        security_id=security.id, direction="long", list_type="watchlist"
    )
    session.add(position)
    await session.flush()
    ids = dict(entity=entity.id, security=security.id, position=position.id)
    coverage = CoverageMap(
        subject_type=subject_type,
        subject_id=ids[subject_type],
        evidence_class_coverage_json={},
    )
    session.add(coverage)
    await session.flush()
    return coverage, ids


def prompt(**changes):
    return {
        "question_text": "What capacity is committed through 2027?",
        "urgency": 4,
        "mechanism": "committed memory capacity",
        "time_horizon": "calendar 2027",
        "decision_relevance": "supply outlook",
        **changes,
    }


async def evidence(session, status="processed", *, state=None):
    source = Source(name="Synthetic official source", source_type="official")
    session.add(source)
    await session.flush()
    raw = RawEvidence(
        source_id=source.id,
        source_item_type="web_research",
        title="Synthetic capacity disclosure",
        is_processed=True,
    )
    session.add(raw)
    await session.flush()
    item = SourceItem(
        raw_evidence_id=raw.id,
        source_id=source.id,
        processing_status=status,
        extracted_text="Management disclosed a collaboration. The announcement gives no committed capacity, price, or commercial terms.",
    )
    session.add(item)
    if state:
        session.add(
            EvidenceProcessingState(raw_evidence_id=raw.id, extraction_status=state)
        )
    await session.flush()
    return raw, item


async def test_irrelevant_fetched_source_returns_to_due_research_without_resolution(
    question_session, monkeypatch
):
    session = question_session
    coverage, _ = await tracked_scope(session, "security")
    question = await QuestionLifecycleService(session).ensure_question(
        coverage, prompt(), []
    )
    raw, item = await evidence(session, "rejected_irrelevant")
    service = ResearchService(session)
    monkeypatch.setattr(
        service,
        "_search_and_ingest",
        AsyncMock(return_value=ResearchRunResult(True, "ok", raw.id, True)),
    )
    assessment = AsyncMock(
        side_effect=AssertionError("Irrelevant extraction must not resolve a question")
    )
    monkeypatch.setattr("investos.services.research.call_llm_json", assessment)
    await service.run_targeted_question(question)
    assert question.status == "needs_research"
    assert question.last_material_outcome == "irrelevant_evidence"
    assert (
        question.next_action == "research"
        and question.next_review_at > datetime.now(UTC)
    )
    assert question.research_attempt_count == 1
    assert item.processing_status == "rejected_irrelevant"
    assert not await session.scalar(
        select(Resolution).where(Resolution.unresolved_question_id == question.id)
    )
    assert not await AutomationCoordinator()._due_question(session)
    assert not await AutomationCoordinator()._select_resolution_question(session)
    history = await question_history(question.id, offset=0, session=session)
    assert any(
        row["detail"].get("evidence_id") == str(raw.id) for row in history["items"]
    )


async def test_partial_answer_and_provider_failure_have_durable_different_next_actions(
    question_session, monkeypatch
):
    session = question_session
    coverage, _ = await tracked_scope(session)
    question = await QuestionLifecycleService(session).ensure_question(
        coverage, prompt(), []
    )
    raw, _ = await evidence(session)
    question.originating_evidence_id = raw.id
    monkeypatch.setattr(
        "investos.services.research.call_llm_json",
        AsyncMock(side_effect=TimeoutError("synthetic provider outage")),
    )
    assert not await ResearchService(session)._maybe_resolve_question(question, raw.id)
    assert (
        question.status == "awaiting_provider"
        and question.next_action == "assess_evidence"
    )
    assert question.resolution_attempt_count == 0
    monkeypatch.setattr(
        "investos.services.research.call_llm_json",
        AsyncMock(
            return_value={
                "answered": False,
                "outcome": "partial_answer",
                "summary": "The collaboration exists.",
                "remaining_uncertainty": "Capacity is undisclosed.",
            }
        ),
    )
    assert not await ResearchService(session)._maybe_resolve_question(question, raw.id)
    assert question.status == "partial_answer" and question.next_action == "research"
    assert question.resolution_attempt_count == 1
    rows = (
        await session.scalars(
            select(QuestionHistory).where(QuestionHistory.question_id == question.id)
        )
    ).all()
    assert any(
        row.detail_json.get("remaining_uncertainty") == "Capacity is undisclosed."
        for row in rows
    )


@pytest.mark.parametrize(
    "status,state",
    [
        ("processed_with_fallback", "retry_scheduled"),
        ("extraction_retry_exhausted", "retry_exhausted"),
    ],
)
async def test_degraded_extraction_is_never_presented_as_an_answer(
    question_session, monkeypatch, status, state
):
    session = question_session
    coverage, _ = await tracked_scope(session)
    question = await QuestionLifecycleService(session).ensure_question(
        coverage, prompt(), []
    )
    raw, _ = await evidence(session, status, state=state)
    monkeypatch.setattr(
        "investos.services.research.call_llm_json",
        AsyncMock(side_effect=AssertionError("Fallback cannot answer")),
    )
    assert not await ResearchService(session)._maybe_resolve_question(question, raw.id)
    assert question.status == (
        "awaiting_extraction" if state == "retry_scheduled" else "needs_research"
    )
    assert question.next_review_at is not None
    assert question.resolution_attempt_count == 0


async def test_successful_direct_answer_has_resolution_and_immutable_history(
    question_session, monkeypatch
):
    session = question_session
    coverage, _ = await tracked_scope(session)
    question = await QuestionLifecycleService(session).ensure_question(
        coverage, prompt(), []
    )
    raw, _ = await evidence(session)
    monkeypatch.setattr(
        "investos.services.research.call_llm_json",
        AsyncMock(
            return_value={
                "answered": True,
                "outcome": "answered",
                "summary": "Capacity is committed through 2027.",
                "remaining_uncertainty": "Execution risk remains.",
            }
        ),
    )
    assert await ResearchService(session)._maybe_resolve_question(question, raw.id)
    assert question.status == "answered" and question.owner == "history"
    assert question.next_action is None and question.next_review_at is None
    resolution = await session.scalar(
        select(Resolution).where(Resolution.unresolved_question_id == question.id)
    )
    assert resolution.resolving_evidence_ids == [raw.id]
    assert "Execution risk" in resolution.summary


async def test_empty_search_budget_exhausts_and_only_explicit_owner_renewal_reopens(
    question_session, monkeypatch
):
    session = question_session
    coverage, _ = await tracked_scope(session)
    lifecycle = QuestionLifecycleService(session)
    question = await lifecycle.ensure_question(coverage, prompt(), [])
    service = ResearchService(session)
    search = AsyncMock(return_value=ResearchRunResult(False, "no_result"))
    monkeypatch.setattr(service, "_search_and_ingest", search)
    for _ in range(4):
        await service.run_targeted_question(question)
    assert search.await_count == 3
    assert question.status == "abandoned" and question.next_action is None
    assert (
        await lifecycle.ensure_question(coverage, prompt(), [question])
    ).status == "abandoned"
    await question_decision(
        question.id,
        QuestionDecision(action="renew", reason="New disclosure expected"),
        session,
    )
    assert question.status == "needs_research" and question.research_attempt_count == 0
    assert (await AutomationCoordinator()._due_question(session)).id == question.id
    await question_decision(
        question.id,
        QuestionDecision(action="retire", reason="No longer actionable"),
        session,
    )
    assert question.status == "archived"
    assert (
        await session.scalar(
            select(func.count())
            .select_from(QuestionHistory)
            .where(QuestionHistory.question_id == question.id)
        )
        >= 8
    )


async def test_semantic_paraphrases_share_canonical_subject_but_keep_distinct_horizons(
    question_session,
):
    session = question_session
    coverage, ids = await tracked_scope(session)
    lifecycle = QuestionLifecycleService(session)
    first = await lifecycle.ensure_question(coverage, prompt(), [])
    position_coverage = CoverageMap(
        subject_type="position",
        subject_id=ids["position"],
        evidence_class_coverage_json={},
    )
    session.add(position_coverage)
    await session.flush()
    paraphrase = await lifecycle.ensure_question(
        position_coverage,
        prompt(question_text="How much 2027 memory supply is already contracted?"),
        [],
    )
    assert paraphrase.id == first.id
    different = await lifecycle.ensure_question(
        coverage,
        prompt(time_horizon="calendar 2028", existing_question_id=str(first.id)),
        [first],
    )
    assert different.id != first.id
    mechanism = await lifecycle.ensure_question(
        coverage,
        prompt(mechanism="factory yields", existing_question_id=str(first.id)),
        [first],
    )
    assert mechanism.id != first.id
    assert first.question_text == prompt()["question_text"]
    history = await question_history(first.id, offset=0, session=session)
    assert any(
        "contracted" in row["detail"].get("question_text", "")
        for row in history["items"]
    )


async def test_legacy_semantic_merge_preserves_old_resolution_links(question_session):
    session = question_session
    coverage, _ = await tracked_scope(session)
    lifecycle = QuestionLifecycleService(session)
    canonical = await lifecycle.ensure_question(coverage, prompt(), [])
    legacy = UnresolvedQuestion(
        coverage_map_id=coverage.id, question_text="Legacy paraphrase", status="open"
    )
    session.add(legacy)
    await session.flush()
    resolution = Resolution(
        unresolved_question_id=legacy.id, summary="Historical assessment"
    )
    session.add(resolution)
    await session.flush()
    merged = await lifecycle.ensure_question(
        coverage, prompt(existing_question_id=str(legacy.id)), [legacy]
    )
    assert merged.id == canonical.id
    assert legacy.status == "superseded" and legacy.superseded_by_id == canonical.id
    assert resolution.unresolved_question_id == legacy.id


async def test_eligible_due_selection_survives_large_unrelated_backlog(
    question_session,
):
    session = question_session
    coverage, _ = await tracked_scope(session, "security")
    unrelated = CoverageMap(
        subject_type="entity", subject_id=uuid4(), evidence_class_coverage_json={}
    )
    session.add(unrelated)
    await session.flush()
    for index in range(75):
        session.add(
            UnresolvedQuestion(
                coverage_map_id=unrelated.id,
                question_text=f"Historical unrelated question {index}",
                urgency=5,
            )
        )
    target = await QuestionLifecycleService(session).ensure_question(
        coverage, prompt(urgency=1), []
    )
    await session.flush()
    selected = await AutomationCoordinator()._due_question(session)
    assert selected.id == target.id
    retired_count = await session.scalar(
        select(func.count())
        .select_from(UnresolvedQuestion)
        .where(
            UnresolvedQuestion.coverage_map_id == unrelated.id,
            UnresolvedQuestion.status == "archived",
        )
    )
    assert retired_count == 75
    snapshot = await QuestionLifecycleService(session).snapshot()
    assert snapshot["eligible_due"] == snapshot["eligible_active"] == 1
    assert snapshot["historical"] == 76


async def test_coverage_refresh_preserves_answers_and_archives_omitted_work(
    question_session,
):
    session = question_session
    coverage, _ = await tracked_scope(session)
    lifecycle = QuestionLifecycleService(session)
    answered = await lifecycle.ensure_question(coverage, prompt(), [])
    lifecycle.transition(answered, status="answered", outcome="direct_answer")
    stale = await lifecycle.ensure_question(
        coverage, prompt(mechanism="unprioritized uncertainty"), []
    )
    await CoverageWorker(session)._reconcile_unresolved_questions(
        coverage.id,
        [
            prompt(
                question_text="What supply is contracted in 2027?",
                existing_question_id=str(answered.id),
            )
        ],
    )
    assert answered.status == "answered"
    assert (
        stale.status == "archived"
        and stale.last_material_outcome == "coverage_no_longer_prioritized"
    )


async def test_extraction_wait_has_an_explicit_end(question_session, monkeypatch):
    session = question_session
    coverage, _ = await tracked_scope(session)
    question = await QuestionLifecycleService(session).ensure_question(
        coverage, prompt(), []
    )
    raw, _ = await evidence(session, "processed_with_fallback", state="retry_scheduled")
    state = await session.scalar(
        select(EvidenceProcessingState).where(
            EvidenceProcessingState.raw_evidence_id == raw.id
        )
    )
    state.created_at = datetime.now(UTC) - timedelta(days=8)
    monkeypatch.setattr(
        "investos.services.research.call_llm_json",
        AsyncMock(side_effect=AssertionError("Expired fallback cannot answer")),
    )
    assert not await ResearchService(session)._maybe_resolve_question(question, raw.id)
    assert (
        question.status == "needs_research"
        and question.last_material_outcome == "extraction_unavailable"
    )


async def test_legacy_fallback_gets_durable_state_and_closed_holding_is_retired(
    question_session,
):
    session = question_session
    coverage, ids = await tracked_scope(session)
    question = await QuestionLifecycleService(session).ensure_question(
        coverage, prompt(), []
    )
    raw, _ = await evidence(session, "processed_with_fallback")
    assert not await ResearchService(session)._maybe_resolve_question(question, raw.id)
    state = await session.scalar(
        select(EvidenceProcessingState).where(
            EvidenceProcessingState.raw_evidence_id == raw.id
        )
    )
    assert state is not None and state.extraction_status == "retry_scheduled"
    assert question.status == "awaiting_extraction"
    position = await session.get(Position, ids["position"])
    position.list_type = "holding"
    position.quantity = 0
    await session.flush()
    assert await AutomationCoordinator()._select_resolution_question(session) is None
    assert (
        question.status == "archived"
        and question.last_material_outcome == "outside_active_portfolio"
    )


async def test_actual_legacy_migration_preserves_prompts_and_old_resolution_links(
    question_session,
):
    import importlib.util
    from pathlib import Path

    import sqlalchemy as sa

    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    path = (
        Path(__file__).parents[1]
        / "alembic/versions/9f4b8d2e5c30_question_lifecycle.py"
    )
    spec = importlib.util.spec_from_file_location("question_lifecycle_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    def run(connection):
        schema = "test_question_migration_" + uuid4().hex
        connection.execute(sa.text(f'CREATE SCHEMA "{schema}"'))
        connection.execute(sa.text(f'SET LOCAL search_path TO "{schema}", public'))
        connection.execute(sa.text("""CREATE TABLE unresolved_questions (
            id uuid PRIMARY KEY, question_text text, status text,
            originating_evidence_id uuid, created_at timestamptz)"""))
        old_id, fetched_id, answered_id, evidence_id = (
            uuid4(),
            uuid4(),
            uuid4(),
            uuid4(),
        )
        connection.execute(
            sa.text(
                """INSERT INTO unresolved_questions VALUES
            (:old_id, 'Original stale prompt', 'open', NULL, now()-interval '40 days'),
            (:fetched_id, 'Fetched source needs assessment', 'investigating', :evidence, now()-interval '40 days'),
            (:answered_id, 'Original resolved prompt', 'answered', :evidence, now())"""
            ),
            dict(
                old_id=old_id,
                fetched_id=fetched_id,
                answered_id=answered_id,
                evidence=evidence_id,
            ),
        )
        connection.execute(
            sa.text(
                "CREATE TABLE preserved_resolutions (question_id uuid REFERENCES unresolved_questions(id), summary text)"
            )
        )
        connection.execute(
            sa.text(
                "INSERT INTO preserved_resolutions VALUES (:id, 'Original answer retained')"
            ),
            dict(id=answered_id),
        )
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        rows = {
            row["id"]: row
            for row in connection.execute(
                sa.text("SELECT * FROM unresolved_questions")
            ).mappings()
        }
        assert (
            rows[old_id]["status"] == "archived"
            and rows[old_id]["next_review_at"] is None
        )
        assert rows[old_id]["question_text"] == "Original stale prompt"
        assert (
            rows[fetched_id]["status"] == "awaiting_resolution"
            and rows[fetched_id]["next_action"] == "assess_evidence"
        )
        assert rows[fetched_id]["originating_evidence_id"] == evidence_id
        assert rows[answered_id]["status"] == "answered"
        assert all(row["semantic_key"] is None for row in rows.values())
        assert (
            connection.execute(
                sa.text("SELECT count(*) FROM question_history")
            ).scalar_one()
            == 3
        )
        assert (
            connection.execute(
                sa.text("SELECT summary FROM preserved_resolutions")
            ).scalar_one()
            == "Original answer retained"
        )
        with pytest.raises(RuntimeError, match="cannot be discarded"):
            migration.downgrade()

    await (await question_session.connection()).run_sync(run)
