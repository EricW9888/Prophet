from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from investos.config import settings
from investos.services.agent import AgentService
from investos.services.agent_action_log import AgentActionLogService
from investos.services.automation import AutomationCoordinator, JobTelemetry
from investos.services.research import ResearchRunResult


def test_activity_focus_filters_routine_success_before_the_feed_limit(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(
        AgentActionLogService,
        "_log_path",
        staticmethod(lambda: tmp_path / "actions.jsonl"),
    )
    for source, action, status in [
        ("research", "research_attempt", "ok"),
        ("automation", "research_loop", "deferred"),
        ("automation", "market_data_refresh", "error"),
        *[("automation", "market_data_refresh", "ok")] * 12,
        ("automation", "risk_refresh", "ok"),
    ]:
        AgentActionLogService.append(
            source=source, action_type=action, status=status, summary=action
        )
    visible = AgentActionLogService.recent(limit=3, include_routine=False)
    assert [item["status"] for item in visible] == ["error", "deferred", "ok"]
    assert [item["action_type"] for item in visible] == [
        "market_data_refresh",
        "research_loop",
        "research_attempt",
    ]
    full = AgentActionLogService.recent(limit=2)
    assert [item["action_type"] for item in full] == [
        "risk_refresh",
        "market_data_refresh",
    ]


@pytest.mark.parametrize(
    "result_status,expected",
    [("ok", "ok"), ("partial", "warning"), ("busy", "warning"), ("error", "error")],
)
async def test_mailbox_automation_preserves_incomplete_status(
    monkeypatch, result_status, expected
):
    from investos.services.mailbox import GmailMailboxService
    from investos.services.runtime_settings import RuntimeSettingsStore

    coordinator = AutomationCoordinator()
    coordinator.telemetry["gmail_sync"] = JobTelemetry(
        name="gmail_sync", interval_seconds=86400, enabled=True
    )
    coordinator._log_job_action = Mock()
    monkeypatch.setattr(
        RuntimeSettingsStore,
        "load",
        lambda: SimpleNamespace(gmail=SimpleNamespace(enabled=True)),
    )
    session = AsyncMock()
    session.__aenter__.return_value = session
    monkeypatch.setattr(
        "investos.services.automation.async_session_maker", lambda: session
    )
    monkeypatch.setattr(
        GmailMailboxService,
        "sync_recent_messages",
        AsyncMock(return_value={"status": result_status, "detail": "synthetic result"}),
    )
    await coordinator._run_gmail_sync()
    assert coordinator.telemetry["gmail_sync"].last_status == expected


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


@pytest.mark.asyncio
async def test_scheduled_investment_review_reaches_evidence_planning():
    from uuid import uuid4

    class ReachedEvidencePlanning(Exception):
        pass

    subject_id = uuid4()
    service = AgentService(None)
    service._select_autonomous_candidate = AsyncMock(
        return_value=(
            None,
            SimpleNamespace(ticker="SYN", entity_id=subject_id),
            SimpleNamespace(trigger_reason="Test a material evidence gap"),
        )
    )
    service._classify_turn_intent = AsyncMock(return_value={"route": "operate"})
    service._maybe_operating_context_answer = AsyncMock(
        side_effect=AssertionError(
            "A scheduled review cannot return a status snapshot."
        )
    )
    service.conversation_history = AsyncMock(return_value=SimpleNamespace(entries=[]))
    service.resolve_context = AsyncMock(
        return_value=SimpleNamespace(
            subject_id=subject_id,
            subject_type="entity",
            subject_name="Synthetic issuer",
            candidates=[],
        )
    )
    service._plan_question_research = AsyncMock(side_effect=ReachedEvidencePlanning)

    with pytest.raises(ReachedEvidencePlanning):
        await service.run_reflection_cycle()

    service._classify_turn_intent.assert_not_awaited()
    service._maybe_operating_context_answer.assert_not_awaited()
    assert service.resolve_context.await_args.kwargs["subject_id"] == subject_id
    assert (
        service._plan_question_research.await_args.kwargs["subject_name"]
        == "Synthetic issuer"
    )


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (ResearchRunResult(started=True, reason="ok"), "no_new_evidence"),
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
            "skipped",
        ),
    ],
)
def test_research_result_exposes_operator_truth(result, expected):
    assert result.telemetry_status == expected


async def test_start_schedules_sequential_calibration_catchup_jobs(monkeypatch):
    monkeypatch.setattr(settings, "AUTOMATION_ENABLED", True)
    listener = AsyncMock()
    monkeypatch.setattr(
        "investos.services.automation.listen_for_work_signals", listener
    )
    coordinator = AutomationCoordinator()
    coordinator._register_job = Mock()
    coordinator.scheduler.start = Mock()
    coordinator.sync_runtime_jobs = Mock()
    coordinator._schedule_startup_run = Mock()
    coordinator._schedule_startup_sequence = Mock()

    coordinator.start()
    await coordinator._signal_listener
    listener.assert_awaited_once_with(coordinator.signal_ready_work)
    await coordinator.shutdown()

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
    monkeypatch.setattr(settings, "AUTOMATION_ENABLED", True)
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
            universe_scope="manual",
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
    directory_call = next(
        call
        for call in coordinator._sync_job.call_args_list
        if call.args[0] == "listing_directory"
    )
    assert directory_call.args[3] is False
    runtime.opportunity_discovery.universe_scope = "us_listed"
    coordinator._sync_job.reset_mock()
    coordinator.sync_runtime_jobs()
    directory_call = next(
        call
        for call in coordinator._sync_job.call_args_list
        if call.args[0] == "listing_directory"
    )
    assert directory_call.args[3] is True


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
