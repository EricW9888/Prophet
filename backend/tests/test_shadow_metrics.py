import importlib.util
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from math import sqrt
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from alembic.migration import MigrationContext
from alembic.operations import Operations
from investos.config import settings
from investos.models.entity import Entity, Security
from investos.models.lesson import Lesson, LessonObservation
from investos.models.portfolio import Transaction
from investos.models.shadow import (
    ExperimentResult,
    ShadowAccountEvent,
    ShadowExperiment,
)
from investos.schemas.shadow import ExperimentResultResponse
from investos.services.graph import GraphService
from investos.services.runtime_settings import RuntimeSettings, RuntimeSettingsStore
from investos.services.shadow import ShadowService
from investos.services.shadow_metrics import calculate_shadow_metrics, result_metrics


def observations(values, *, intervals=None, flow=0.0):
    timestamp = datetime(2026, 7, 1, tzinfo=UTC)
    result = []
    for index, value in enumerate(values):
        if index:
            timestamp += timedelta(
                seconds=(intervals or [86400] * len(values))[index - 1]
            )
        result.append(
            {
                "captured_at": timestamp.isoformat(),
                "equity": value,
                "net_external_cash_flow": flow,
            }
        )
    return result


def test_positive_terminal_return_keeps_intervening_drawdown_and_real_sharpe():
    metrics = calculate_shadow_metrics(observations([100, 120, 90, 110]))
    returns = [0.2, -0.25, 110 / 90 - 1]
    average = sum(returns) / 3
    deviation = sqrt(sum((value - average) ** 2 for value in returns) / 2)
    assert metrics["max_drawdown"] == pytest.approx(-0.25)
    assert metrics["sharpe_ratio"] == pytest.approx(average / deviation)
    assert metrics["sharpe_ratio"] != pytest.approx(0.1)
    assert metrics["observation_count"] == 4
    assert metrics["return_count"] == 3
    assert metrics["cadence_seconds"] == 86400
    assert metrics["annualization"] == "none_per_observed_interval"
    assert len(metrics["observations_sha256"]) == 64


@pytest.mark.parametrize(
    "values,reason",
    [
        ([], "insufficient_observations"),
        ([100], "insufficient_observations"),
        ([100, 110], "insufficient_returns"),
        ([100, 100, 100], "zero_return_variance"),
        ([100, 110, 121], "zero_return_variance"),
        ([0, 1, 2], "nonpositive_starting_equity"),
        ([100, float("nan"), 110], "invalid_observation"),
        ([100, float("inf"), 110], "invalid_observation"),
        ([100, -1, 110], "invalid_observation"),
    ],
)
def test_unavailable_sharpe_is_not_zero_or_alpha(values, reason):
    metrics = calculate_shadow_metrics(observations(values))
    assert metrics["sharpe_ratio"] is None
    assert metrics["sharpe_status"] == "unavailable"
    assert metrics["sharpe_reason"] == reason


def test_irregular_cadence_preserves_observed_drawdown_without_sharpe():
    metrics = calculate_shadow_metrics(
        observations([100, 120, 90, 110], intervals=[86400, 43200, 86400])
    )
    assert metrics["max_drawdown"] == pytest.approx(-0.25)
    assert metrics["sharpe_ratio"] is None
    assert metrics["sharpe_reason"] == "irregular_observation_cadence"


def test_subsecond_intervals_are_not_admitted_by_one_second_tolerance():
    metrics = calculate_shadow_metrics(observations([100, 90, 110], intervals=[0.1, 1]))
    assert metrics["sharpe_ratio"] is None
    assert metrics["sharpe_reason"] == "irregular_observation_cadence"


@pytest.mark.parametrize("flow", [None, 100, -100, float("nan")])
def test_external_flows_are_explicitly_unsupported(flow):
    metrics = calculate_shadow_metrics(observations([100, 200, 210], flow=flow))
    assert metrics["max_drawdown"] is None
    assert metrics["sharpe_ratio"] is None
    assert metrics["drawdown_reason"] == "unsupported_external_cash_flows"


@pytest.mark.parametrize("intervals", [[0, 86400], [-1, 86400]])
def test_duplicate_or_reversed_dates_are_not_silently_repaired(intervals):
    metrics = calculate_shadow_metrics(
        observations([100, 90, 110], intervals=intervals)
    )
    assert metrics["drawdown_reason"] == "nonincreasing_timestamps"


def test_missing_or_naive_dates_remain_unavailable():
    items = observations([100, 90, 110])
    items[1]["captured_at"] = "2026-07-02T00:00:00"
    assert calculate_shadow_metrics(items)["drawdown_reason"] == "invalid_observation"
    del items[1]["captured_at"]
    assert calculate_shadow_metrics(items)["drawdown_reason"] == "invalid_observation"


def test_bankrupt_path_has_full_drawdown_but_no_return_after_zero():
    metrics = calculate_shadow_metrics(observations([100, 0, 0]))
    assert metrics["max_drawdown"] == -1
    assert metrics["sharpe_ratio"] is None
    assert metrics["sharpe_reason"] == "nonpositive_return_denominator"


def result(**extra):
    return SimpleNamespace(
        id=uuid4(),
        experiment_id=uuid4(),
        shadow_return=0.1,
        actual_return=0.0,
        alpha=0.1,
        max_drawdown=0.0,
        sharpe_ratio=0.1,
        reasoning="Synthetic paper outcome.",
        **extra,
    )


def test_legacy_api_does_not_publish_proxy_numbers():
    legacy = result()
    payload = ExperimentResultResponse.model_validate(legacy)
    assert payload.max_drawdown is None
    assert payload.sharpe_ratio is None
    assert (
        payload.performance_metrics_json["sharpe_reason"] == "legacy_unverified_metrics"
    )


@pytest.mark.asyncio
async def test_api_report_and_graph_agree_on_measured_and_legacy_values():
    experiment = SimpleNamespace(
        policy_description="Synthetic bounded policy",
        initial_portfolio_state_json={"experiment_context": {}, "snapshot_summary": {}},
        final_portfolio_state_json={"run_details": {}},
    )
    for item in [
        result(),
        result(
            performance_metrics_json=calculate_shadow_metrics(
                observations([100, 120, 90, 110])
            )
        ),
    ]:
        metrics = result_metrics(item)
        payload = ExperimentResultResponse.model_validate(item)
        report = ShadowService(None)._build_experiment_report(
            experiment=experiment, result=item, run_log=[]
        )
        properties = await GraphService(None)._node_properties(
            "experiment_result", str(item.id), item
        )
        assert (
            payload.max_drawdown
            == metrics["max_drawdown"]
            == report["outcome_summary"]["max_drawdown"]
            == properties.get("max_drawdown")
        )
        assert (
            payload.sharpe_ratio
            == metrics["sharpe_ratio"]
            == report["outcome_summary"]["sharpe_ratio"]
            == properties.get("sharpe_ratio")
        )


def test_interim_result_requires_the_same_measurements():
    metrics = calculate_shadow_metrics(observations([100, 120, 90, 110]))
    item = ShadowService(None)._transient_result(uuid4(), 0.1, 0.0, 0.1, metrics)
    assert item.max_drawdown == pytest.approx(-0.25)
    assert item.sharpe_ratio == metrics["sharpe_ratio"]
    empty = ShadowService(None)._transient_result(uuid4(), 0.1, 0.0, 0.1)
    assert empty.max_drawdown is None
    assert empty.sharpe_ratio is None


@pytest.fixture
async def metrics_session():
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


async def test_migration_archives_proxies_and_preserves_raw_history(metrics_session):
    path = (
        Path(__file__).parents[1]
        / "alembic/versions/7d2f9a6b3c18_measure_shadow_metrics.py"
    )
    spec = importlib.util.spec_from_file_location("shadow_metrics_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    def run_migration(connection):
        # The temporary table shadows only this test connection's real table.
        connection.execute(sa.text("""
            CREATE TEMP TABLE experiment_results (
                id integer PRIMARY KEY,
                max_drawdown double precision NOT NULL,
                sharpe_ratio double precision,
                raw_history jsonb NOT NULL
            ) ON COMMIT DROP
        """))
        connection.execute(sa.text("""
            INSERT INTO experiment_results VALUES
                (1, -0.12, 0.07, '{"checkpoint_log":[100,88,107]}'),
                (2, 0.0, NULL, '{"checkpoint_log":[]}')
        """))
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        rows = (
            connection.execute(sa.text("SELECT * FROM experiment_results ORDER BY id"))
            .mappings()
            .all()
        )
        assert rows[0]["raw_history"] == {"checkpoint_log": [100, 88, 107]}
        assert rows[1]["raw_history"] == {"checkpoint_log": []}
        for row, old_drawdown, old_sharpe in zip(rows, [-0.12, 0.0], [0.07, None]):
            assert row["max_drawdown"] is None
            assert row["sharpe_ratio"] is None
            metadata = row["performance_metrics_json"]
            assert metadata["drawdown_reason"] == "legacy_unverified_metrics"
            assert metadata["legacy_proxy_values"] == {
                "max_drawdown": old_drawdown,
                "sharpe_ratio": old_sharpe,
            }
        with pytest.raises(RuntimeError, match="provenance cannot be discarded"):
            migration.downgrade()

    await (await metrics_session.connection()).run_sync(run_migration)


async def test_real_checkpoints_persist_path_metrics_and_learning_provenance(
    metrics_session, monkeypatch
):
    import investos.services.shadow as shadow_module

    start = datetime(2026, 7, 1, tzinfo=UTC)
    clock = {"now": start}

    class ControlledDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock["now"]

    monkeypatch.setattr(shadow_module, "datetime", ControlledDatetime)
    runtime = RuntimeSettings()
    runtime.paper_trading.enabled = False
    runtime.portfolio.remaining_buying_power = 0
    monkeypatch.setattr(RuntimeSettingsStore, "load", lambda: runtime)
    monkeypatch.setattr(shadow_module.ReviewService, "refresh_queue", AsyncMock())
    security_id = uuid4()
    position = SimpleNamespace(
        security_id=security_id,
        quantity=1,
        avg_cost_basis=100,
        current_price=100,
        market_value=100,
        list_type="holding",
    )
    snapshot = {
        "captured_at": start.isoformat(),
        "experiment_context": {"horizon_label": "short_term"},
        "snapshot_summary": {"total_market_value": 100, "remaining_buying_power": 0},
        "shadow_state": {
            "cash": 0,
            "positions": [
                {
                    "security_id": str(security_id),
                    "quantity": 1,
                    "avg_cost_basis": 100,
                    "current_price": 100,
                }
            ],
        },
    }
    experiment = ShadowExperiment(
        name="Synthetic measured path",
        policy_description="Hold existing paper position",
        start_point=start,
        end_point=start + timedelta(days=2),
        initial_portfolio_state_json=deepcopy(snapshot),
        run_status="queued",
    )
    metrics_session.add(experiment)
    await metrics_session.flush()
    service = ShadowService(metrics_session)
    monkeypatch.setattr(
        service, "_positions_with_stance", AsyncMock(return_value=[(position, None)])
    )
    monkeypatch.setattr(service, "_security_ticker", AsyncMock(return_value="SYNTH"))
    monkeypatch.setattr(
        service, "_security_name", AsyncMock(return_value="Synthetic security")
    )
    monkeypatch.setattr(service, "_resolve_guidance", AsyncMock(return_value={}))
    monkeypatch.setattr(service, "_build_checkpoint_plan", AsyncMock(return_value={}))
    monkeypatch.setattr(
        service, "_maybe_run_shadow_research", AsyncMock(return_value={})
    )
    monkeypatch.setattr(
        service,
        "_portfolio_snapshot",
        AsyncMock(side_effect=lambda **kwargs: deepcopy(snapshot)),
    )

    for day, equity in enumerate([120, 90, 110], start=1):
        clock["now"] = start + timedelta(days=day)
        position.current_price = position.market_value = equity
        await service.run_experiment(experiment.id)

    await metrics_session.refresh(experiment)
    item = await metrics_session.scalar(
        sa.select(ExperimentResult).where(
            ExperimentResult.experiment_id == experiment.id
        )
    )
    assert experiment.run_status == "completed"
    assert item.alpha == 0
    assert item.max_drawdown == pytest.approx(-0.25)
    expected = calculate_shadow_metrics(observations([100, 120, 90, 110]))
    assert item.sharpe_ratio == pytest.approx(expected["sharpe_ratio"])
    stored = experiment.final_portfolio_state_json["run_details"][
        "performance_observations"
    ]
    assert [entry["equity"] for entry in stored] == [100, 120, 90, 110]
    assert (
        item.performance_metrics_json["observations_sha256"]
        == expected["observations_sha256"]
    )
    lesson = await metrics_session.scalar(
        sa.select(Lesson).where(Lesson.originating_experiment_result_id == item.id)
    )
    observation = await metrics_session.scalar(
        sa.select(LessonObservation).where(
            LessonObservation.experiment_result_id == item.id
        )
    )
    assert lesson.metadata_json["latest_performance_metrics"]["max_drawdown"] == -0.25
    assert (
        observation.evidence_json["performance_metrics"]["sharpe_ratio"]
        == item.sharpe_ratio
    )
    assert (
        lesson.metadata_json["calibration_basis"]
        == "terminal_alpha_not_risk_adjusted_or_causal_validation"
    )

    # A corrected source event can also invalidate an already completed result.
    entity = Entity(name="Synthetic event issuer", entity_type="company")
    metrics_session.add(entity)
    await metrics_session.flush()
    security = Security(
        id=security_id,
        entity_id=entity.id,
        ticker="SYNTH",
        asset_class="equity",
        instrument_type="common_stock",
    )
    corrected_transaction = Transaction(
        action="dividend",
        quantity=0,
        price=1,
        status="corrected",
        executed_at=start + timedelta(days=1),
    )
    metrics_session.add_all([security, corrected_transaction])
    await metrics_session.flush()
    event = ShadowAccountEvent(
        experiment_id=experiment.id,
        source_transaction_id=corrected_transaction.id,
        security_id=security_id,
        ticker="SYNTH",
        event_type="dividend",
        status="needs_reconciliation",
        occurred_at=start + timedelta(days=1),
        quantity_before=1,
        quantity_after=1,
        cash_before=0,
        cash_after=1,
        amount=1,
        derivation="Synthetic corrected source",
    )
    metrics_session.add(event)
    await metrics_session.flush()
    rebuilt = await service.rebuild_shadow_account_timelines(
        experiment_ids={experiment.id}
    )
    assert rebuilt == {"attempted": 1, "rebuilt": 1, "failed": 0}
    await metrics_session.refresh(item)
    await metrics_session.refresh(lesson)
    await metrics_session.refresh(observation)
    assert item.max_drawdown is None
    assert item.sharpe_ratio is None
    assert item.performance_metrics_json["sharpe_reason"] == "account_history_rebuilt"
    assert (
        item.performance_metrics_json["superseded_measurement"]["observations_sha256"]
        == expected["observations_sha256"]
    )
    assert (
        lesson.metadata_json["latest_performance_metrics"]["sharpe_reason"]
        == "account_history_rebuilt"
    )
    assert (
        observation.evidence_json["performance_metrics"]["sharpe_reason"]
        == "account_history_rebuilt"
    )
    assert (
        experiment.final_portfolio_state_json["run_details"]["performance_observations"]
        == stored
    )
