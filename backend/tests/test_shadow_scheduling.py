from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from investos.services.automation import AutomationCoordinator, JobTelemetry
from investos.services.shadow import ShadowService

NOW = datetime(2026, 9, 7, 12, tzinfo=UTC)


def experiment(*, status="running", checkpoint=None, days_old=1, events=None):
    return SimpleNamespace(
        id=uuid4(),
        name="Synthetic checkpoint",
        run_status=status,
        created_at=NOW - timedelta(days=days_old),
        initial_portfolio_state_json={},
        final_portfolio_state_json={
            "run_details": {
                "progress": {"next_checkpoint_at": checkpoint, "step_count": 1},
                "pending_evidence_events": events or [],
            }
        },
    )


def test_waiting_newest_experiment_does_not_block_due_older_experiment():
    waiting = experiment(checkpoint=(NOW + timedelta(days=1)).isoformat())
    due = experiment(checkpoint=(NOW - timedelta(hours=1)).isoformat(), days_old=2)

    assert ShadowService.next_actionable_experiment([waiting, due], now=NOW) is due


@pytest.mark.parametrize("status", ["queued", "pending", "running"])
def test_oldest_eligible_work_is_selected_independent_of_list_order(status):
    older = experiment(status=status, days_old=5)
    newer = experiment(status=status, days_old=2)

    for items in ([newer, older], [older, newer]):
        assert ShadowService.next_actionable_experiment(items, now=NOW) is older


@pytest.mark.parametrize("status", ["manual", "completed", "failed", "skipped"])
def test_terminal_and_manual_runs_are_not_automatically_restarted(status):
    assert (
        ShadowService.next_actionable_experiment([experiment(status=status)], now=NOW)
        is None
    )


async def test_active_run_lock_does_not_block_another_due_experiment():
    locked = experiment(days_old=5)
    due = experiment(days_old=2)
    async with ShadowService._experiment_run_lock(locked.id):
        assert ShadowService.next_actionable_experiment([locked, due], now=NOW) is due


def test_persisted_evidence_can_wake_an_experiment_after_scheduler_restart():
    awakened = experiment(
        checkpoint=(NOW + timedelta(days=1)).isoformat(),
        events=[{"event_id": str(uuid4()), "queued_at": NOW.isoformat()}],
    )
    assert ShadowService.next_actionable_experiment([awakened], now=NOW) is awakened


def test_repeated_evidence_attempt_rotates_behind_older_due_work():
    attempted = experiment(days_old=5, events=[{"event_id": str(uuid4())}])
    attempted.final_portfolio_state_json["run_details"]["progress"][
        "last_updated_at"
    ] = NOW.isoformat()
    older_due = experiment(days_old=2)
    assert (
        ShadowService.next_actionable_experiment([attempted, older_due], now=NOW)
        is older_due
    )


@pytest.mark.parametrize(
    "checkpoint",
    [None, "invalid date", "2026-09-07T12:00:00", "2026-09-07T07:00:00-05:00"],
)
def test_checkpoint_dates_use_the_execution_eligibility_boundary(checkpoint):
    due = experiment(checkpoint=checkpoint)
    assert ShadowService.next_actionable_experiment([due], now=NOW) is due


def test_old_evidence_wakeup_is_not_starved_by_new_queued_experiments():
    awakened = experiment(
        checkpoint=(NOW + timedelta(days=1)).isoformat(),
        events=[{"queued_at": (NOW - timedelta(hours=2)).isoformat()}],
    )
    newer = experiment(status="queued")
    newer.created_at = NOW - timedelta(hours=1)
    assert (
        ShadowService.next_actionable_experiment([newer, awakened], now=NOW) is awakened
    )


def test_equal_due_times_are_stable_across_reload_order():
    items = [experiment(), experiment()]
    assert ShadowService.next_actionable_experiment(items, now=NOW) is (
        ShadowService.next_actionable_experiment(list(reversed(items)), now=NOW)
    )


@pytest.mark.parametrize("has_due", [True, False])
async def test_automation_advances_due_work_or_reports_waiting(monkeypatch, has_due):
    waiting = experiment(checkpoint=(datetime.now(UTC) + timedelta(days=1)).isoformat())
    due = experiment(days_old=2)
    items = [waiting, due] if has_due else [waiting]
    coordinator = AutomationCoordinator()
    coordinator.telemetry["shadow_refresh"] = JobTelemetry(
        name="shadow_refresh", interval_seconds=300
    )
    coordinator._log_job_action = Mock()
    session = AsyncMock()
    session.__aenter__.return_value = session
    monkeypatch.setattr(
        "investos.services.automation.async_session_maker", lambda: session
    )
    setup_results = {
        "apply_portfolio_events_to_shadow_accounts": {
            "applied": 0,
            "recorded": 0,
            "reconciliation_required": 0,
            "timelines_rebuilt": 0,
            "timeline_rebuild_failures": 0,
        },
        "refresh_pending_paper_orders": 0,
        "refresh_paper_account_marks": 0,
        "reconcile_shadow_learning": {"reconciled": 0},
        "attach_queued_evidence_events": {
            "attached": 0,
            "skipped": 0,
            "experiment_ids": [],
        },
        "list_experiments": items,
    }
    for method, result in setup_results.items():
        monkeypatch.setattr(ShadowService, method, AsyncMock(return_value=result))

    async def advance(experiment_id):
        assert experiment_id == due.id
        due.final_portfolio_state_json["run_details"]["progress"]["step_count"] += 1
        return due

    run = AsyncMock(side_effect=advance)
    monkeypatch.setattr(ShadowService, "run_experiment", run)
    await coordinator._run_shadow_refresh()

    telemetry = coordinator.telemetry["shadow_refresh"]
    assert telemetry.last_status == "ok"
    if has_due:
        run.assert_awaited_once_with(due.id)
        assert "advanced=" in telemetry.detail
        coordinator._log_job_action.assert_called_once()
    else:
        run.assert_not_awaited()
        assert "no_due_experiments active_experiments=1" in telemetry.detail
        coordinator._log_job_action.assert_not_called()
