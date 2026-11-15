import asyncio
from types import SimpleNamespace

import pytest

from investos.api.routes.automation import get_automation_status
from investos.core.provider_health import ProviderHealthStore
from investos.services.automation import AutomationCoordinator, JobTelemetry


@pytest.mark.parametrize("cancelled", [False, True])
async def test_current_dispatch_membership_survives_stale_outcomes_and_cleanup(
    monkeypatch, cancelled
):
    coordinator = AutomationCoordinator()
    coordinator.telemetry["synthetic"] = JobTelemetry(
        name="synthetic", interval_seconds=60, last_status="running"
    )
    monkeypatch.setattr(coordinator, "sync_runtime_jobs", lambda: None)
    monkeypatch.setattr(ProviderHealthStore, "public_status", lambda: {})
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(automation=coordinator))
    )
    before = await get_automation_status(request)
    assert before.jobs[0].last_status == "running"
    assert not before.jobs[0].is_running
    started, release = asyncio.Event(), asyncio.Event()

    async def worker():
        started.set()
        await release.wait()

    task = asyncio.create_task(
        coordinator._dispatch_job("synthetic", worker, trigger="manual")
    )
    try:
        await asyncio.wait_for(started.wait(), 1)
        during = await get_automation_status(request)
        assert during.jobs[0].is_running
        assert during.model_dump()["jobs"][0]["is_running"] is True
        assert not before.jobs[0].is_running  # Earlier snapshots stay unchanged.
        if cancelled:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            release.set()
            await task
        after = await get_automation_status(request)
        assert after.jobs[0].last_status == "running"
        assert not after.jobs[0].is_running
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
