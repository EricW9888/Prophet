from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from investos.config import settings
from investos.services.automation import AutomationCoordinator, JobTelemetry
from investos.services.research import ResearchRunResult


def test_agent_result_error_is_not_reported_as_successful_automation():
    assert (
        AutomationCoordinator._result_telemetry_status(
            {"status": "error", "detail": "LLM strategy failed: ReadTimeout"}
        )
        == "error"
    )


def test_unknown_success_result_defaults_to_ok():
    assert (
        AutomationCoordinator._result_telemetry_status(
            {"status": "completed", "detail": "Finished"}
        )
        == "ok"
    )


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (ResearchRunResult(started=True, reason="ok"), "ok"),
        (
            ResearchRunResult(started=False, reason="research_provider_not_configured"),
            "waiting_for_config",
        ),
        (
            ResearchRunResult(started=False, reason="research_provider_limit_exceeded"),
            "warning",
        ),
        (
            ResearchRunResult(started=False, reason="duplicate_recent_research"),
            "ok",
        ),
    ],
)
def test_research_result_exposes_operator_truth(result, expected):
    assert result.telemetry_status == expected


def test_start_schedules_sequential_calibration_catchup_jobs():
    coordinator = AutomationCoordinator()
    coordinator._register_job = Mock()
    coordinator.scheduler.start = Mock()
    coordinator.sync_runtime_jobs = Mock()
    coordinator._schedule_startup_run = Mock()
    coordinator._schedule_startup_sequence = Mock()

    coordinator.start()

    coordinator._schedule_startup_sequence.assert_called_once()
    jobs = coordinator._schedule_startup_sequence.call_args.args[0]
    assert [name for name, _func in jobs] == [
        "source_claim_assessment",
        "market_setup_assessment",
        "fundamental_freshness",
        "investment_object_backfill",
        "pattern_discovery",
    ]
    assert (
        coordinator._schedule_startup_sequence.call_args.kwargs["delay_seconds"]
        == settings.AUTOMATION_STARTUP_CATCHUP_DELAY_SECONDS
    )


def test_runtime_sync_applies_live_opportunity_discovery_controls(monkeypatch):
    runtime = SimpleNamespace(
        market_data=SimpleNamespace(
            enabled=True,
            refresh_interval_seconds=60,
        ),
        gmail=SimpleNamespace(enabled=False),
        plaid=SimpleNamespace(enabled=False, access_token=None),
        opportunity_discovery=SimpleNamespace(
            enabled=False,
            interval_seconds=43200,
        ),
    )
    monkeypatch.setattr(
        "investos.services.automation.RuntimeSettingsStore.load",
        lambda: runtime,
    )
    coordinator = AutomationCoordinator()
    coordinator._sync_job = Mock()

    coordinator.sync_runtime_jobs()

    opportunity_call = next(
        call
        for call in coordinator._sync_job.call_args_list
        if call.args[0] == "opportunity_discovery"
    )
    assert opportunity_call.args[2:] == (43200, False)


@pytest.mark.asyncio
async def test_startup_catchup_waits_for_each_job_before_starting_next():
    coordinator = AutomationCoordinator()
    coordinator.telemetry = {
        "first": JobTelemetry(name="first", interval_seconds=60),
        "second": JobTelemetry(name="second", interval_seconds=60),
    }
    coordinator.scheduler.add_job = Mock()
    execution_order: list[str] = []
    first = AsyncMock(side_effect=lambda: execution_order.append("first"))
    second = AsyncMock(side_effect=lambda: execution_order.append("second"))

    coordinator._schedule_startup_sequence(
        [("first", first), ("second", second)],
        delay_seconds=10,
    )
    runner = coordinator.scheduler.add_job.call_args.args[0]
    await runner()

    assert execution_order == ["first", "second"]
