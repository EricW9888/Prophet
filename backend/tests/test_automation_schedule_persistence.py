from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

from investos.config import settings
from investos.services.automation import AutomationCoordinator


def _coordinator(monkeypatch, tmp_path) -> AutomationCoordinator:
    monkeypatch.setattr(settings, "STORAGE_DIR", str(tmp_path))
    coordinator = AutomationCoordinator()
    coordinator.scheduler.add_job = Mock()
    return coordinator


def _registered_first_run(coordinator: AutomationCoordinator, name: str) -> datetime:
    calls = [
        call
        for call in coordinator.scheduler.add_job.call_args_list
        if call.kwargs.get("id") == name
    ]
    assert calls, f"{name} was not scheduled"
    return calls[-1].kwargs["next_run_time"]


def test_restart_resumes_a_daily_job_from_its_last_completed_run(monkeypatch, tmp_path):
    first = _coordinator(monkeypatch, tmp_path)
    finished = datetime.now(UTC) - timedelta(hours=23)
    first._record_completed_run("database_backup", finished)

    # A process restart: a new coordinator reads the saved state.
    restarted = _coordinator(monkeypatch, tmp_path)
    restarted._completed_runs = restarted._load_completed_runs()
    restarted._register_job("database_backup", Mock(), 86400, True)

    # Before the fix the schedule restarted at now + 24h, so a process that
    # restarts more often than daily never backed up.
    assert _registered_first_run(restarted, "database_backup") == finished + timedelta(
        days=1
    )


def test_overdue_or_never_completed_jobs_catch_up_after_the_startup_delay(
    monkeypatch, tmp_path
):
    coordinator = _coordinator(monkeypatch, tmp_path)
    coordinator._completed_runs = {
        "entity_hygiene": datetime.now(UTC) - timedelta(days=3)
    }
    before = datetime.now(UTC)

    coordinator._register_job("entity_hygiene", Mock(), 21600, True)
    coordinator._register_job("database_backup", Mock(), 86400, True)

    delay = timedelta(seconds=settings.AUTOMATION_STARTUP_CATCHUP_DELAY_SECONDS)
    stagger = timedelta(seconds=AutomationCoordinator.OVERDUE_STAGGER_SECONDS)
    overdue = _registered_first_run(coordinator, "entity_hygiene")
    never_run = _registered_first_run(coordinator, "database_backup")
    assert before + delay < overdue < datetime.now(UTC) + delay + 2 * stagger
    assert never_run > overdue
    assert never_run < datetime.now(UTC) + timedelta(hours=1)


async def test_only_a_completed_run_is_recorded(monkeypatch, tmp_path):
    coordinator = _coordinator(monkeypatch, tmp_path)
    coordinator._register_job("database_backup", Mock(), 86400, True)
    coordinator._register_job("integrity_audit", Mock(), 3600, True)

    async def succeed():
        coordinator.telemetry["database_backup"].last_status = "ok"

    async def fail():
        coordinator.telemetry["integrity_audit"].last_status = "error"

    await coordinator._dispatch_job("database_backup", succeed, trigger="interval")
    await coordinator._dispatch_job("integrity_audit", fail, trigger="interval")

    saved = coordinator._load_completed_runs()
    assert "database_backup" in saved
    assert "integrity_audit" not in saved


def test_unreadable_schedule_state_treats_jobs_as_overdue(monkeypatch, tmp_path):
    coordinator = _coordinator(monkeypatch, tmp_path)
    path = coordinator._schedule_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")

    assert coordinator._load_completed_runs() == {}


def test_status_after_a_restart_shows_the_recorded_last_run(monkeypatch, tmp_path):
    first = _coordinator(monkeypatch, tmp_path)
    finished = datetime.now(UTC) - timedelta(hours=22)
    first._record_completed_run("database_backup", finished)

    restarted = _coordinator(monkeypatch, tmp_path)
    restarted._completed_runs = restarted._load_completed_runs()
    restarted._register_job("database_backup", Mock(), 86400, True)
    restarted._register_job("integrity_audit", Mock(), 3600, True)

    # Before the fix Status read "Not run yet" for a backup that ran 22 hours ago.
    assert restarted.telemetry["database_backup"].last_run_at == finished
    assert restarted.telemetry["integrity_audit"].last_run_at is None


async def test_review_queue_rebuilds_on_its_own_schedule_without_a_model(
    monkeypatch, tmp_path
):
    from contextlib import asynccontextmanager

    from investos.services import automation as module

    coordinator = _coordinator(monkeypatch, tmp_path)
    coordinator._register_job(
        "review_queue_refresh", coordinator._run_review_queue_refresh, 900, True
    )

    calls = []

    class FakeReview:
        def __init__(self, session):
            pass

        async def refresh_queue(self):
            calls.append(True)
            return [object(), object(), object()]

    @asynccontextmanager
    async def fake_session():
        yield Mock()

    monkeypatch.setattr(module, "ReviewService", FakeReview)
    monkeypatch.setattr(module, "async_session_maker", fake_session)
    await coordinator._run_review_queue_refresh()
    telemetry = coordinator.telemetry["review_queue_refresh"]
    assert calls == [True]
    assert telemetry.last_status == "ok" and telemetry.detail == "3 items need review."
