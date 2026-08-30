from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from investos.config import settings
from investos.services.agent import AgentService
from investos.services.agent_action_log import AgentActionLogService
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


def test_internal_automation_state_does_not_displace_visible_activity(
    monkeypatch, tmp_path
):
    log_path = tmp_path / "agent-actions.jsonl"
    monkeypatch.setattr(
        AgentActionLogService,
        "_log_path",
        staticmethod(lambda: log_path),
    )
    AgentActionLogService.append(
        source="automation",
        action_type="research_loop",
        status="ok",
        summary="Material evidence was added.",
    )
    for index in range(5):
        AgentActionLogService.append(
            source="automation",
            action_type="strategist_cycle",
            status="idle",
            summary=f"No-op checkpoint {index}",
            metadata={"internal_state": True},
        )

    visible = AgentActionLogService.recent(limit=2)
    all_entries = AgentActionLogService.recent(limit=10, include_internal=True)

    assert [entry["summary"] for entry in visible] == ["Material evidence was added."]
    assert len(all_entries) == 6


def test_strategist_fingerprint_reads_hidden_checkpoint(monkeypatch, tmp_path):
    log_path = tmp_path / "agent-actions.jsonl"
    monkeypatch.setattr(
        AgentActionLogService,
        "_log_path",
        staticmethod(lambda: log_path),
    )
    AgentActionLogService.append(
        source="automation",
        action_type="strategist_cycle",
        status="idle",
        summary="Unchanged operating state.",
        metadata={
            "internal_state": True,
            "decision_fingerprint": "packet-123",
        },
    )

    assert AgentActionLogService.has_recent_fingerprint(
        "packet-123",
        action_type="strategist_cycle",
        within_seconds=60,
    )
    assert AgentActionLogService.recent(limit=5) == []


def test_strategist_planning_signal_is_not_triggered_by_inventory_alone():
    quiet_packet = {
        "review_queue": [],
        "priority_monitor": [],
        "recent_research_ids": [],
        "recent_lessons": [],
        "active_themes": ["AI infrastructure"],
        "tracked_tickers": ["MEMA"],
    }
    pressured_packet = {
        **quiet_packet,
        "review_queue": [{"item_type": "position", "priority_score": 82.0}],
    }

    assert not AgentService._has_strategic_planning_signal(quiet_packet)
    assert AgentService._has_strategic_planning_signal(pressured_packet)
    assert AgentService._strategic_planning_fingerprint(quiet_packet) == (
        AgentService._strategic_planning_fingerprint(dict(quiet_packet))
    )


@pytest.mark.asyncio
async def test_reflection_without_review_pressure_does_not_start_agent_turn():
    service = AgentService.__new__(AgentService)
    service._select_autonomous_candidate = AsyncMock(return_value=None)
    service.handle_turn = AsyncMock()

    result = await service.run_reflection_cycle()

    assert result == {
        "status": "idle",
        "detail": "no_autonomous_candidate",
        "actions": 0,
    }
    service.handle_turn.assert_not_awaited()


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

    registered_names = [
        call.args[0] for call in coordinator._register_job.call_args_list
    ]
    assert "youtube_channel_review" in registered_names
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
