"""Real storage tests for deadline closure, family concurrency and retained runs."""

import importlib.util
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from alembic.migration import MigrationContext
from alembic.operations import Operations
from investos.config import settings
from investos.models.entity import Entity, Security
from investos.models.lesson import Lesson, LessonObservation
from investos.models.shadow import (
    ExperimentFamilyState,
    ExperimentResult,
    ShadowEvidenceEvent,
    ShadowExperiment,
    ShadowLifecycleEvent,
    ShadowOrder,
)
from investos.schemas.shadow import ShadowExperimentCreate
from investos.services.runtime_settings import RuntimeSettings, RuntimeSettingsStore
from investos.services.shadow import ShadowService
from investos.services.shadow_lifecycle import lifecycle_counts, lifecycle_state


@pytest.fixture
async def session():
    engine = create_async_engine(settings.SQLALCHEMY_DATABASE_URI, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            async with AsyncSession(
                bind=connection,
                expire_on_commit=False,
                join_transaction_mode="create_savepoint",
            ) as session:
                yield session
            await transaction.rollback()
    finally:
        await engine.dispose()


def snapshot(now, *, cash=100):
    return {
        "captured_at": now.isoformat(),
        "experiment_context": {"horizon_label": "short_term", "subject_refs": []},
        "snapshot_summary": {"total_market_value": 0, "remaining_buying_power": cash},
        "shadow_state": {"cash": cash, "cash_reserved": 0, "positions": []},
    }


def setup_service(session, monkeypatch, state):
    service = ShadowService(session)
    monkeypatch.setattr(
        service,
        "_portfolio_snapshot",
        AsyncMock(side_effect=lambda **kwargs: deepcopy(state)),
    )
    runtime = RuntimeSettings()
    runtime.paper_trading.enabled = False
    runtime.portfolio.remaining_buying_power = 100
    monkeypatch.setattr(RuntimeSettingsStore, "load", lambda: runtime)
    monkeypatch.setattr(
        "investos.services.shadow.ReviewService.refresh_queue", AsyncMock()
    )
    return service


async def test_expired_run_closes_without_provider_or_policy_consumption(
    session, monkeypatch
):
    now = datetime.now(UTC)
    state = snapshot(now - timedelta(days=20))
    state["shadow_state"]["cash_reserved"] = 20
    state["run_details"] = {
        "progress": {
            "step_count": 1,
            "target_steps": 3,
            "started_at": state["captured_at"],
            "next_checkpoint_at": (now + timedelta(days=30)).isoformat(),
        },
        "performance_observations": [
            {"captured_at": state["captured_at"], "equity": 100}
        ],
        "checkpoint_log": [
            {"step_index": 1, "summary": "Original observation retained"}
        ],
        "pending_evidence_events": [],
    }
    experiment = ShadowExperiment(
        name="Expired synthetic hypothesis",
        policy_description="Hold",
        start_point=now - timedelta(days=20),
        end_point=now - timedelta(days=10),
        initial_portfolio_state_json=snapshot(now - timedelta(days=20)),
        final_portfolio_state_json=state,
        run_status="running",
    )
    entity = Entity(name="Synthetic lifecycle issuer", entity_type="company")
    session.add_all([experiment, entity])
    await session.flush()
    security = Security(
        entity_id=entity.id,
        ticker=f"L{uuid4().hex[:6]}",
        asset_class="equity",
        instrument_type="common_stock",
    )
    session.add(security)
    await session.flush()
    receipt = ShadowEvidenceEvent(
        experiment_id=experiment.id,
        subject_type="security",
        subject_id=security.id,
        status="attached",
        trigger_reason="Synthetic pending receipt",
        idempotency_key=uuid4().hex,
    )
    order = ShadowOrder(
        experiment_id=experiment.id,
        security_id=security.id,
        ticker=security.ticker,
        client_order_id=uuid4().hex,
        side="buy",
        status="accepted",
        requested_quantity=1,
        reference_price=20,
        reserved_notional=20,
        submitted_at=now - timedelta(days=11),
        rationale="Retained order",
        checkpoint_index=1,
    )
    session.add_all([receipt, order])
    await session.flush()
    state = deepcopy(state)
    state["run_details"]["pending_evidence_events"] = [
        {"event_id": str(receipt.id), "queued_at": now.isoformat()}
    ]
    experiment.final_portfolio_state_json = state
    await session.flush()
    service = setup_service(session, monkeypatch, snapshot(now))
    monkeypatch.setattr(service, "_positions_with_stance", AsyncMock(return_value=[]))
    provider = AsyncMock(
        side_effect=AssertionError("A closed window cannot request a policy decision")
    )
    monkeypatch.setattr(service, "_resolve_guidance", provider)
    monkeypatch.setattr(service, "_build_checkpoint_plan", provider)
    monkeypatch.setattr(service, "_maybe_run_shadow_research", provider)
    assert ShadowService.next_actionable_experiment([experiment], now=now) is experiment
    await service.run_experiment(experiment.id)
    assert experiment.run_status == "completed"
    assert order.status == "canceled" and order.reserved_notional == 0
    assert receipt.status == "window_closed"
    details = experiment.final_portfolio_state_json["run_details"]
    assert details["progress"]["target_steps"] == 3
    assert details["progress"]["step_count"] == 2
    assert details["checkpoint_log"][0]["summary"] == "Original observation retained"
    assert details["pending_evidence_events"] == []
    assert (
        details["evidence_event_log"][0]["outcome"]
        == "window_closed_without_policy_consumption"
    )
    assert details["observation_window"]["late_by_seconds"] >= 10 * 86400
    assert (
        details["observation_window"]["final_captured_at"]
        != experiment.end_point.isoformat()
    )
    assert experiment.final_portfolio_state_json["shadow_state"]["cash_reserved"] == 0
    assert (
        await session.scalar(
            sa.select(sa.func.count())
            .select_from(LessonObservation)
            .join(ExperimentResult)
            .where(ExperimentResult.experiment_id == experiment.id)
        )
        == 0
    )
    result_id = await session.scalar(
        sa.select(ExperimentResult.id).where(
            ExperimentResult.experiment_id == experiment.id
        )
    )
    history_count = await session.scalar(
        sa.select(sa.func.count())
        .select_from(ShadowLifecycleEvent)
        .where(ShadowLifecycleEvent.experiment_id == experiment.id)
    )
    await service.run_experiment(experiment.id)
    assert (
        await session.scalar(
            sa.select(ExperimentResult.id).where(
                ExperimentResult.experiment_id == experiment.id
            )
        )
        == result_id
    )
    assert (
        await session.scalar(
            sa.select(sa.func.count())
            .select_from(ShadowLifecycleEvent)
            .where(ShadowLifecycleEvent.experiment_id == experiment.id)
        )
        == history_count
    )
    provider.assert_not_awaited()


async def test_semantic_family_reuses_paraphrased_proposals_but_keeps_changed_dimensions(
    session, monkeypatch
):
    now = datetime.now(UTC)
    service = setup_service(session, monkeypatch, snapshot(now))
    identity = {
        "mechanism": "supplier inventory pressure",
        "counterfactual": "hold allocation instead of reducing",
    }
    first = await service.create_experiment(
        ShadowExperimentCreate(
            name="First wording",
            policy_description="Cut exposure on inventory pressure",
            start_point=now,
            end_point=now + timedelta(days=30),
            family_identity=identity,
        )
    )
    family = await session.get(ExperimentFamilyState, first.family_id)
    second = await service.create_experiment(
        ShadowExperimentCreate(
            name="A paraphrased proposal",
            policy_description="Rebalance away as stock builds up",
            start_point=now,
            end_point=now + timedelta(days=30),
            family_identity={**identity, "existing_family_id": str(family.id)},
        )
    )
    assert second.id == first.id
    assert (
        await session.scalar(
            sa.select(sa.func.count())
            .select_from(ShadowExperiment)
            .where(ShadowExperiment.family_id == family.id)
        )
        == 1
    )
    with pytest.raises(ValueError, match="changed dimensions"):
        await service.create_experiment(
            ShadowExperimentCreate(
                name="Changed horizon",
                policy_description="Same policy",
                start_point=now,
                end_point=now + timedelta(days=60),
                family_identity={**identity, "existing_family_id": str(family.id)},
            )
        )
    changed = await service.create_experiment(
        ShadowExperimentCreate(
            name="Different mechanism",
            policy_description="Same name does not merge",
            start_point=now,
            end_point=now + timedelta(days=30),
            family_identity={**identity, "mechanism": "demand acceleration"},
        )
    )
    longer = await service.create_experiment(
        ShadowExperimentCreate(
            name="Different horizon",
            policy_description="Same policy",
            start_point=now,
            end_point=now + timedelta(days=60),
            family_identity=identity,
        )
    )
    assert len({first.family_id, changed.family_id, longer.family_id}) == 3
    # The database, not an in-process check, rejects a competing active insertion.
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            session.add(
                ShadowExperiment(
                    family_id=family.id,
                    name="Concurrent contender",
                    policy_description="Hold",
                    start_point=now,
                    end_point=now + timedelta(days=30),
                    initial_portfolio_state_json=snapshot(now),
                    run_status="queued",
                )
            )
            await session.flush()


async def test_retry_creates_new_run_without_rewriting_result_or_learning_links(
    session, monkeypatch
):
    now = datetime.now(UTC)
    service = setup_service(session, monkeypatch, snapshot(now))
    original = await service.create_experiment(
        ShadowExperimentCreate(
            name="Synthetic retry",
            policy_description="Hold",
            start_point=now - timedelta(days=60),
            end_point=now - timedelta(days=30),
            family_identity={"mechanism": "margin recovery", "counterfactual": "hold"},
        )
    )
    original.run_status = "completed"
    original.completed_at = now - timedelta(days=30)
    original.final_portfolio_state_json = {
        "run_details": {"checkpoint_log": [{"step_index": 3}]}
    }
    result = ExperimentResult(
        experiment_id=original.id,
        shadow_return=0.1,
        actual_return=0.05,
        alpha=0.05,
        reasoning="Original outcome",
    )
    session.add(result)
    await session.flush()
    lesson = Lesson(
        title="Original lesson",
        summary="Retained",
        lesson_type="shadow_policy_outcome",
        originating_experiment_result_id=result.id,
    )
    session.add(lesson)
    await session.commit()
    retried = await service.queue_experiment_run(original.id)
    assert retried.id != original.id and retried.previous_run_id == original.id
    assert original.run_status == "completed" and result.reasoning == "Original outcome"
    assert lesson.originating_experiment_result_id == result.id
    assert original.final_portfolio_state_json["run_details"]["checkpoint_log"] == [
        {"step_index": 3}
    ]
    assert retried.end_point - retried.start_point == timedelta(days=30)
    assert (await service.queue_experiment_run(original.id)).id == retried.id


async def test_retry_reuses_active_run_without_inventing_predecessor(
    session, monkeypatch
):
    now = datetime.now(UTC)
    service = setup_service(session, monkeypatch, snapshot(now))
    payload = ShadowExperimentCreate(
        name="Synthetic active lineage",
        policy_description="Hold",
        start_point=now,
        end_point=now + timedelta(days=30),
        family_identity={"mechanism": "inventory recovery", "counterfactual": "hold"},
    )
    original = await service.create_experiment(payload)
    original.run_status = "completed"
    await session.commit()
    active = await service.create_experiment(payload)
    assert active.previous_run_id is None
    assert (await service.queue_experiment_run(original.id)).id == active.id
    assert active.previous_run_id is None
    events = await service.lifecycle_history(active.id)
    assert all(event["event_type"] != "retry_created" for event in events)


async def test_partial_family_dimensions_are_rejected(session, monkeypatch):
    now = datetime.now(UTC)
    service = setup_service(session, monkeypatch, snapshot(now))
    with pytest.raises(ValueError, match="both mechanism and counterfactual"):
        await service.create_experiment(
            ShadowExperimentCreate(
                name="Incomplete dimensions",
                policy_description="Hold",
                family_identity={"mechanism": "margin recovery"},
            )
        )


async def test_explicit_legacy_equivalence_stops_work_and_retains_original_links(
    session, monkeypatch
):
    now = datetime.now(UTC)
    service = setup_service(session, monkeypatch, snapshot(now))
    first = await service.create_experiment(
        ShadowExperimentCreate(
            name="Legacy one",
            policy_description="Trim when inventories rise",
            start_point=now,
            end_point=now + timedelta(days=30),
        )
    )
    second = await service.create_experiment(
        ShadowExperimentCreate(
            name="Legacy paraphrase",
            policy_description="Reduce on inventory build",
            start_point=now,
            end_point=now + timedelta(days=30),
        )
    )
    original_family_id = first.family_id
    first.final_portfolio_state_json = {
        **snapshot(now),
        "run_details": {
            "checkpoint_log": [{"step_index": 1, "summary": "Prior checkpoint"}]
        },
    }
    await service.reconcile_family(
        first.id,
        second.id,
        reason="Same inventory mechanism and hold-versus-trim counterfactual, verified by operator.",
    )
    assert first.run_status == "superseded" and first.superseded_by_id == second.id
    assert first.family_id == original_family_id
    assert (
        first.final_portfolio_state_json["run_details"]["checkpoint_log"][0]["summary"]
        == "Prior checkpoint"
    )
    assert (
        await session.get(ExperimentFamilyState, original_family_id)
    ).superseded_by_id == second.family_id
    repeated = await service.create_experiment(
        ShadowExperimentCreate(
            name="Original wording retry",
            policy_description="Trim when inventories rise",
            start_point=now,
            end_point=now + timedelta(days=30),
        )
    )
    assert repeated.id == second.id
    history = await service.lifecycle_history(first.id)
    decision = next(
        event for event in history if event["event_type"] == "family_reconciled"
    )
    assert decision["detail"]["decision_basis"] == "explicit_owner_equivalence"


def test_shared_counts_distinguish_stored_state_from_actionable_state():
    now = datetime.now(UTC)

    def row(status, *, end=None, checkpoint=None, start=None):
        return SimpleNamespace(
            id=uuid4(),
            run_status=status,
            created_at=now,
            start_point=start or now - timedelta(days=1),
            end_point=end or now + timedelta(days=2),
            initial_portfolio_state_json={},
            final_portfolio_state_json={
                "run_details": {"progress": {"next_checkpoint_at": checkpoint}}
            },
        )

    rows = [
        row("queued"),
        row("pending", start=now + timedelta(days=1)),
        row("running", checkpoint=(now + timedelta(days=1)).isoformat()),
        row(
            "running",
            end=now - timedelta(days=1),
            checkpoint=(now + timedelta(days=1)).isoformat(),
        ),
        row("completed"),
        row("failed"),
        row("superseded"),
    ]
    counts = lifecycle_counts(rows, now=now)
    assert counts == {
        "total": 7,
        "active": 4,
        "queued": 2,
        "running": 2,
        "waiting": 2,
        "due": 1,
        "overdue": 1,
        "completed": 1,
        "failed": 1,
        "superseded": 1,
        "skipped": 0,
        "manual": 0,
    }
    assert (
        ShadowService.next_actionable_experiment([rows[2], rows[3]], now=now) is rows[3]
    )
    assert lifecycle_state(rows[1], now=now) == "waiting"


async def test_actual_migration_preserves_duplicate_snapshots_and_enforces_constraint(
    session,
):
    path = (
        Path(__file__).parents[1] / "alembic/versions/a6c9e2f5b831_shadow_lifecycle.py"
    )
    spec = importlib.util.spec_from_file_location("shadow_lifecycle_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    schema = f"shadow_migration_{uuid4().hex}"

    def run(connection):
        connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
        connection.exec_driver_sql(f'SET LOCAL search_path TO "{schema}", public')
        connection.exec_driver_sql(
            "CREATE TABLE experiment_family_states (id uuid PRIMARY KEY, family_name text, description text)"
        )
        connection.exec_driver_sql(
            "CREATE TABLE shadow_experiments (id uuid PRIMARY KEY, family_id uuid, run_status text, start_point timestamptz, end_point timestamptz, created_at timestamptz, completed_at timestamptz, skip_reason text, initial_portfolio_state_json jsonb, final_portfolio_state_json jsonb)"
        )
        connection.exec_driver_sql(
            "CREATE TABLE shadow_orders (id uuid PRIMARY KEY, experiment_id uuid, status text, canceled_at timestamptz, reserved_notional numeric)"
        )
        connection.exec_driver_sql(
            "CREATE TABLE shadow_evidence_events (id uuid PRIMARY KEY, experiment_id uuid, status text, processed_at timestamptz, processing_detail text)"
        )
        family, first, second = uuid4(), uuid4(), uuid4()
        connection.execute(
            sa.text(
                "INSERT INTO experiment_family_states VALUES (:id, 'exact family', 'Retained description')"
            ),
            {"id": family},
        )
        for identifier, steps in [(first, 3), (second, 1)]:
            connection.execute(
                sa.text(
                    "INSERT INTO shadow_experiments (id, family_id, run_status, created_at, initial_portfolio_state_json, final_portfolio_state_json) VALUES (:id, :family, 'running', now(), '{}'::jsonb, jsonb_build_object('run_details', jsonb_build_object('progress', jsonb_build_object('step_count', CAST(:steps AS integer))), 'shadow_state', jsonb_build_object('cash_reserved', 20)))"
                ),
                {"id": identifier, "family": family, "steps": steps},
            )
        connection.execute(
            sa.text(
                "INSERT INTO shadow_orders VALUES (:id, :run, 'accepted', NULL, 20)"
            ),
            {"id": uuid4(), "run": second},
        )
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            rows = (
                connection.execute(
                    sa.text(
                        "SELECT id, run_status, superseded_by_id, final_portfolio_state_json FROM shadow_experiments"
                    )
                )
                .mappings()
                .all()
            )
            by_id = {row["id"]: row for row in rows}
            assert by_id[first]["run_status"] == "running"
            assert by_id[second]["run_status"] == "superseded"
            assert by_id[second]["superseded_by_id"] == first
            assert (
                by_id[second]["final_portfolio_state_json"]["run_details"]["progress"][
                    "step_count"
                ]
                == 1
            )
            assert (
                connection.execute(
                    sa.text("SELECT status FROM shadow_orders")
                ).scalar_one()
                == "canceled"
            )
            assert (
                connection.execute(
                    sa.text("SELECT count(*) FROM shadow_lifecycle_events")
                ).scalar_one()
                == 3
            )
            assert (
                connection.execute(
                    sa.text(
                        "SELECT detail_json->'final_state'->'shadow_state'->>'cash_reserved' FROM shadow_lifecycle_events WHERE experiment_id=:id AND event_type='legacy_state_retained'"
                    ),
                    {"id": second},
                ).scalar_one()
                == "20"
            )
            with pytest.raises(RuntimeError, match="provenance cannot be discarded"):
                migration.downgrade()
        connection.exec_driver_sql("SET LOCAL search_path TO public")

    await (await session.connection()).run_sync(run)


async def test_fallback_cannot_extend_budget_or_prevent_deadline_completion(
    session, monkeypatch
):
    import investos.services.shadow as module

    now = datetime.now(UTC)
    clock = {"now": now}

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock["now"]

    monkeypatch.setattr(module, "datetime", Clock)
    state = snapshot(now - timedelta(days=1))
    state["run_details"] = {
        "progress": {
            "step_count": 2,
            "target_steps": 3,
            "started_at": state["captured_at"],
        },
        "pending_evidence_events": [
            {
                "queued_at": now.isoformat(),
                "trigger_reason": "Pending provider interpretation",
            }
        ],
    }
    row = ShadowExperiment(
        name="Fallback receipt",
        policy_description="Hold",
        start_point=now - timedelta(days=1),
        end_point=now + timedelta(days=1),
        initial_portfolio_state_json=snapshot(now - timedelta(days=1)),
        final_portfolio_state_json=state,
        run_status="running",
    )
    session.add(row)
    await session.flush()
    service = setup_service(session, monkeypatch, snapshot(now))
    monkeypatch.setattr(service, "_positions_with_stance", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        service,
        "_resolve_guidance",
        AsyncMock(return_value={"guidance_mode": "fallback"}),
    )
    planner = AsyncMock(
        return_value={"_checkpoint_source": "fallback", "decisions": []}
    )
    monkeypatch.setattr(service, "_build_checkpoint_plan", planner)
    monkeypatch.setattr(
        service, "_maybe_run_shadow_research", AsyncMock(return_value={})
    )
    await service.run_experiment(row.id)
    assert row.run_status == "running"
    assert (
        row.final_portfolio_state_json["run_details"]["progress"]["target_steps"] == 3
    )
    assert (
        len(row.final_portfolio_state_json["run_details"]["pending_evidence_events"])
        == 1
    )
    clock["now"] = row.end_point + timedelta(seconds=30)
    await service.run_experiment(row.id)
    assert row.run_status == "completed"
    assert (
        row.final_portfolio_state_json["run_details"]["progress"]["target_steps"] == 3
    )
    assert len(row.final_portfolio_state_json["run_details"]["evidence_event_log"]) == 1
    planner.assert_awaited_once()


async def test_slow_provider_that_crosses_deadline_cannot_submit_late_trade(
    session, monkeypatch
):
    import investos.services.shadow as module

    now = datetime.now(UTC)
    clock = {"now": now}

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock["now"]

    monkeypatch.setattr(module, "datetime", Clock)
    row = ShadowExperiment(
        name="Slow plan",
        policy_description="Buy on confirmation",
        start_point=now - timedelta(days=1),
        end_point=now + timedelta(seconds=10),
        initial_portfolio_state_json=snapshot(now - timedelta(days=1)),
        run_status="queued",
    )
    session.add(row)
    await session.flush()
    service = setup_service(session, monkeypatch, snapshot(now))
    position = SimpleNamespace(
        security_id=uuid4(),
        quantity=1,
        avg_cost_basis=100,
        current_price=100,
        market_value=100,
        list_type="holding",
    )
    monkeypatch.setattr(
        service, "_positions_with_stance", AsyncMock(return_value=[(position, None)])
    )
    monkeypatch.setattr(service, "_security_ticker", AsyncMock(return_value="SLOW"))
    monkeypatch.setattr(
        service, "_security_name", AsyncMock(return_value="Synthetic slow plan")
    )
    monkeypatch.setattr(service, "_resolve_guidance", AsyncMock(return_value={}))

    async def slow_plan(**kwargs):
        clock["now"] = row.end_point + timedelta(seconds=1)
        return {
            "_checkpoint_source": "provider",
            "decisions": [
                {"ticker": "SLOW", "action": "buy", "position_multiplier": 2}
            ],
        }

    monkeypatch.setattr(service, "_build_checkpoint_plan", slow_plan)
    research = AsyncMock(
        side_effect=AssertionError("Cannot launch research after closure")
    )
    monkeypatch.setattr(service, "_maybe_run_shadow_research", research)
    await service.run_experiment(row.id)
    assert row.run_status == "completed"
    assert (
        await session.scalar(
            sa.select(sa.func.count())
            .select_from(ShadowOrder)
            .where(ShadowOrder.experiment_id == row.id)
        )
        == 0
    )
    assert (
        row.final_portfolio_state_json["run_details"]["run_log"][0]["action"] == "hold"
    )
    research.assert_not_awaited()


async def test_new_work_cannot_use_a_superseded_family_even_if_inserted_directly(
    session, monkeypatch
):
    now = datetime.now(UTC)
    service = setup_service(session, monkeypatch, snapshot(now))
    first = await service.create_experiment(
        ShadowExperimentCreate(
            name="Exact legacy A",
            policy_description="A",
            start_point=now,
            end_point=now + timedelta(days=30),
        )
    )
    second = await service.create_experiment(
        ShadowExperimentCreate(
            name="Exact legacy B",
            policy_description="B",
            start_point=now,
            end_point=now + timedelta(days=30),
        )
    )
    await service.reconcile_family(
        first.id, second.id, reason="Owner verified same mechanism and counterfactual."
    )
    with pytest.raises(IntegrityError, match="canonical family"):
        async with session.begin_nested():
            session.add(
                ShadowExperiment(
                    family_id=first.family_id,
                    name="Bypass attempt",
                    policy_description="Hold",
                    start_point=now,
                    end_point=now + timedelta(days=30),
                    initial_portfolio_state_json=snapshot(now),
                    run_status="running",
                )
            )
            await session.flush()


async def test_dashboard_and_shadow_api_report_the_same_population(
    session, monkeypatch
):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from investos.api.routes.dashboard import router as dashboard_router
    from investos.api.routes.shadow import router as shadow_router
    from investos.db import get_session

    now = datetime.now(UTC)
    setup_service(session, monkeypatch, snapshot(now))
    for status, end in [
        ("queued", now + timedelta(days=1)),
        ("running", now - timedelta(days=5)),
        ("completed", now - timedelta(days=1)),
    ]:
        session.add(
            ShadowExperiment(
                name=f"Synthetic API {status}",
                policy_description="Hold",
                start_point=now - timedelta(days=10),
                end_point=end,
                initial_portfolio_state_json=snapshot(now - timedelta(days=10)),
                final_portfolio_state_json={
                    "run_details": {
                        "progress": {
                            "next_checkpoint_at": (now + timedelta(days=1)).isoformat()
                        }
                    }
                },
                run_status=status,
            )
        )
    await session.flush()
    app = FastAPI()
    app.include_router(dashboard_router)
    app.include_router(shadow_router)

    async def database():
        yield session

    app.dependency_overrides[get_session] = database
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        summary = await client.get("/dashboard/summary")
        lifecycle = await client.get("/shadow/lifecycle")
        assert summary.status_code == lifecycle.status_code == 200
        assert summary.json()["shadow_lifecycle"] == lifecycle.json()
        assert (
            summary.json()["pending_shadow_experiments_count"]
            == lifecycle.json()["active"]
        )
        assert lifecycle.json()["overdue"] == 1


async def test_superseding_a_family_requires_its_active_work_to_stop(
    session, monkeypatch
):
    now = datetime.now(UTC)
    service = setup_service(session, monkeypatch, snapshot(now))
    first = await service.create_experiment(
        ShadowExperimentCreate(
            name="Active old family",
            policy_description="A",
            start_point=now,
            end_point=now + timedelta(days=30),
        )
    )
    second = await service.create_experiment(
        ShadowExperimentCreate(
            name="Retained family",
            policy_description="B",
            start_point=now,
            end_point=now + timedelta(days=30),
        )
    )
    with pytest.raises(IntegrityError, match="cannot retain active work"):
        async with session.begin_nested():
            await session.execute(
                sa.update(ExperimentFamilyState)
                .where(ExperimentFamilyState.id == first.family_id)
                .values(superseded_by_id=second.family_id)
            )
            await session.execute(
                sa.text("SET CONSTRAINTS shadow_family_supersession_guard IMMEDIATE")
            )


async def test_concurrent_service_proposals_converge_to_one_active_run(monkeypatch):
    import asyncio

    engine = create_async_engine(settings.SQLALCHEMY_DATABASE_URI, poolclass=NullPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    now = datetime.now(UTC)
    identity = {
        "mechanism": f"synthetic-concurrency-{uuid4().hex}",
        "counterfactual": "hold versus trim",
    }
    payload = ShadowExperimentCreate(
        name="Concurrent proposal",
        policy_description="Hold",
        start_point=now,
        end_point=now + timedelta(days=30),
        family_identity=identity,
    )
    run_id = family_id = None
    try:

        async def propose():
            async with sessions() as database:
                service = setup_service(database, monkeypatch, snapshot(now))
                row = await service.create_experiment(payload)
                return row.id, row.family_id

        results = await asyncio.gather(propose(), propose())
        run_id, family_id = results[0]
        assert results[0] == results[1]
        async with sessions() as database:
            assert (
                await database.scalar(
                    sa.select(sa.func.count())
                    .select_from(ShadowExperiment)
                    .where(ShadowExperiment.family_id == family_id)
                )
                == 1
            )
    finally:
        if run_id is not None:
            async with sessions() as database:
                await database.execute(
                    sa.delete(ShadowLifecycleEvent).where(
                        ShadowLifecycleEvent.experiment_id == run_id
                    )
                )
                await database.execute(
                    sa.delete(ShadowExperiment).where(ShadowExperiment.id == run_id)
                )
                await database.execute(
                    sa.delete(ExperimentFamilyState).where(
                        ExperimentFamilyState.id == family_id
                    )
                )
                await database.commit()
        await engine.dispose()
