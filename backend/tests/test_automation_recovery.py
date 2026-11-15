import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock

import pytest
from apscheduler.schedulers.base import STATE_RUNNING

from investos.config import settings
from investos.core.provider_health import LLMProviderCooldownError, ProviderHealthStore
from investos.services.automation import AutomationCoordinator, JobTelemetry
from investos.services.pattern_discovery import PatternDiscoveryService
from tests.test_pattern_discovery import _registry, _result


@pytest.fixture
def coordinator(monkeypatch):
    monkeypatch.setattr(settings, "AUTOMATION_ENABLED", True)
    value = AutomationCoordinator()
    monkeypatch.setattr(value.scheduler, "state", STATE_RUNNING)
    monkeypatch.setattr(value, "_log_job_action", Mock())
    return value


@pytest.mark.parametrize(
    "name", sorted(AutomationCoordinator.LLM_DEPENDENT_JOBS | {"future_research"})
)
def test_recovery_covers_every_declared_model_dependency(
    coordinator, monkeypatch, name
):
    monkeypatch.setattr(
        coordinator,
        "LLM_DEPENDENT_JOBS",
        coordinator.LLM_DEPENDENT_JOBS | {"future_research"},
    )
    coordinator.telemetry[name] = JobTelemetry(name, 21600, last_status="deferred")
    future = datetime.now(UTC) + timedelta(hours=6)
    monkeypatch.setattr(
        coordinator.scheduler, "get_job", lambda _: Mock(next_run_time=future)
    )
    modified = Mock()
    monkeypatch.setattr(coordinator.scheduler, "modify_job", modified)
    coordinator.signal_ready_work("provider_recovered")
    modified.assert_called_once()
    assert modified.call_args.args == (name,)
    assert modified.call_args.kwargs["next_run_time"] < future - timedelta(hours=5)
    assert coordinator.telemetry[name].last_status == "deferred"


@pytest.mark.parametrize(
    "status", ["ok", "idle", "error", "cancelled", "waiting_for_config"]
)
def test_recovery_does_not_repeat_non_deferred_model_work(
    coordinator, monkeypatch, status
):
    for name in coordinator.LLM_DEPENDENT_JOBS:
        coordinator.telemetry[name] = JobTelemetry(name, 3600, last_status=status)
    expedite = Mock()
    monkeypatch.setattr(coordinator, "_expedite_pending_job", expedite)
    coordinator.signal_ready_work("provider_recovered")
    expedite.assert_not_called()
    assert not coordinator._pending_signals


def test_recovery_retains_spacing_pause_and_shutdown(coordinator, monkeypatch):
    now = datetime.now(UTC)
    name = "agent_reflection"
    coordinator.telemetry[name] = JobTelemetry(name, 900, last_status="deferred")
    coordinator._last_model_attempt_at[name] = now
    monkeypatch.setattr(
        coordinator.scheduler,
        "get_job",
        lambda _: Mock(next_run_time=now + timedelta(hours=1)),
    )
    modified = Mock()
    monkeypatch.setattr(coordinator.scheduler, "modify_job", modified)
    coordinator.signal_ready_work("provider_recovered")
    assert modified.call_args.kwargs["next_run_time"] == now + timedelta(minutes=15)
    coordinator._pending_signals.clear()
    modified.reset_mock()
    coordinator.telemetry[name].enabled = False
    coordinator.signal_ready_work("provider_recovered")
    modified.assert_not_called()
    coordinator.telemetry[name].enabled = True
    coordinator._shutting_down = True
    coordinator.signal_ready_work("provider_recovered")
    modified.assert_not_called()


async def test_active_recovery_does_not_replay_completed_work_or_drop_new_evidence(
    coordinator, monkeypatch
):
    name = "agent_reflection"
    coordinator.telemetry[name] = JobTelemetry(name, 900, last_status="deferred")
    monkeypatch.setattr(
        ProviderHealthStore, "public_status", lambda: {"state": "healthy"}
    )
    monkeypatch.setattr(
        coordinator.scheduler,
        "get_job",
        lambda _: Mock(next_run_time=datetime.now(UTC) + timedelta(hours=1)),
    )
    modified = Mock()
    monkeypatch.setattr(coordinator.scheduler, "modify_job", modified)

    async def worker():
        coordinator.signal_ready_work("provider_recovered")
        coordinator.telemetry[name].last_status = "ok"

    await coordinator._dispatch_job(name, worker, trigger="interval")
    modified.assert_not_called()
    assert name not in coordinator._pending_signals

    async def worker_with_new_evidence():
        coordinator.telemetry[name].last_status = "deferred"
        coordinator.signal_ready_work("provider_recovered")
        coordinator.signal_ready_work("evidence_ready")
        coordinator.telemetry[name].last_status = "ok"

    await coordinator._dispatch_job(name, worker_with_new_evidence, trigger="interval")
    assert coordinator._pending_signals[name] == "evidence_ready"
    assert name in [call.args[0] for call in modified.call_args_list]


async def test_real_scheduler_resumes_pattern_work_and_rechecks_provider(monkeypatch):
    monkeypatch.setattr(settings, "AUTOMATION_ENABLED", True)
    value = AutomationCoordinator()
    monkeypatch.setattr(value, "_log_job_action", Mock())
    health = {"state": "open"}
    monkeypatch.setattr(ProviderHealthStore, "public_status", lambda: health)
    service = PatternDiscoveryService(None)
    for method, result in {
        "_tracked_universe": {"MEMA": {"ticker": "MEMA"}, "MEMB": {"ticker": "MEMB"}},
        "_recent_evidence_registry": _registry(),
        "_historical_episodes": [{"id": "episode-one"}],
        "_current_regime": None,
        "_find_duplicate": None,
    }.items():
        monkeypatch.setattr(service, method, AsyncMock(return_value=result))
    monkeypatch.setattr(
        "investos.services.pattern_discovery.PortfolioPeerContextService.peer_exposures",
        AsyncMock(return_value=[]),
    )
    model = AsyncMock(return_value=_result())
    monkeypatch.setattr("investos.services.pattern_discovery.call_llm_json", model)
    completed = asyncio.Event()
    results = []

    async def worker():
        results.append(await service.discover(apply=False))
        value.telemetry["pattern_discovery"].last_status = "ok"
        completed.set()

    value._register_job("pattern_discovery", worker, 21600, True)
    value.telemetry["provider_health_probe"] = JobTelemetry(
        "provider_health_probe", 30, last_status="ok"
    )
    value.scheduler.start()
    try:
        await value._dispatch_job("pattern_discovery", worker, trigger="startup")
        model.assert_not_awaited()
        assert value.telemetry["pattern_discovery"].last_status == "deferred"
        health["state"] = "healthy"
        # A substantive retry already recovered before the probe could observe
        # the outage. Readiness must still wake the retained deferred work.
        monkeypatch.setattr(
            "investos.services.automation.probe_llm_provider",
            AsyncMock(return_value=health),
        )
        await value._run_provider_health_probe()
        await asyncio.wait_for(completed.wait(), 5)
        model.assert_awaited_once()
        assert results[0]["detail"] == "validated_pattern_preview"
        assert results[0]["hypothesis"]["falsifier"]
        assert results[0]["hypothesis"]["next_test"]
        assert (
            value.telemetry["pattern_discovery"].last_trigger
            == "signal:provider_recovered"
        )
        await value._run_provider_health_probe()
        assert "pattern_discovery" not in value._pending_signals
    finally:
        await value.shutdown()


@pytest.mark.parametrize("provider_deferred", [False, True])
async def test_healthy_probe_only_reconciles_actual_provider_deferral(
    coordinator, monkeypatch, provider_deferred
):
    coordinator.telemetry["provider_health_probe"] = JobTelemetry(
        "provider_health_probe", 30, last_status="ok"
    )
    coordinator.telemetry["pattern_discovery"] = JobTelemetry(
        "pattern_discovery", 21600, last_status="deferred"
    )
    coordinator.telemetry["pattern_discovery"].provider_deferred = provider_deferred
    monkeypatch.setattr(
        ProviderHealthStore, "public_status", lambda: {"state": "healthy"}
    )
    monkeypatch.setattr(
        "investos.services.automation.probe_llm_provider",
        AsyncMock(return_value={"state": "healthy"}),
    )
    signal = Mock()
    monkeypatch.setattr(coordinator, "signal_ready_work", signal)
    await coordinator._run_provider_health_probe()
    if provider_deferred:
        signal.assert_called_once_with("provider_recovered")
    else:
        signal.assert_not_called()


async def test_provider_marker_survives_hold_and_clears_only_on_admission(
    coordinator, monkeypatch
):
    name = "pattern_discovery"
    coordinator.telemetry[name] = JobTelemetry(name, 21600)
    health = {"state": "open"}
    monkeypatch.setattr(ProviderHealthStore, "public_status", lambda: health)
    worker = AsyncMock()
    await coordinator._dispatch_job(name, worker, trigger="interval")
    assert coordinator.telemetry[name].provider_deferred
    health.update(
        state="recovering",
        lease_until=(datetime.now(UTC) + timedelta(seconds=30)).isoformat(),
    )
    await coordinator._dispatch_job(name, worker, trigger="interval")
    worker.assert_not_awaited()
    assert coordinator.telemetry[name].provider_deferred
    health["state"] = "healthy"
    await coordinator._dispatch_job(name, worker, trigger="interval")
    worker.assert_awaited_once()
    assert not coordinator.telemetry[name].provider_deferred
    coordinator._record_job_failure(
        job_name=name,
        error=LLMProviderCooldownError("Synthetic hold"),
        summary="Deferred",
    )
    assert coordinator.telemetry[name].provider_deferred
    coordinator._record_job_failure(
        job_name=name, error=ValueError("Synthetic input error"), summary="Input error"
    )
    assert not coordinator.telemetry[name].provider_deferred
