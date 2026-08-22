import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from investos.core.dates import (
    lookahead_calendar_datetime,
    parse_explicit_calendar_datetime,
)
from investos.services.automation import AutomationCoordinator, JobTelemetry
from investos.workers.extraction import EXTRACTION_SCHEMA, ExtractionWorker


def test_shared_calendar_parser_requires_explicit_year():
    now = datetime(2026, 6, 23, 12, tzinfo=UTC)

    parsed = parse_explicit_calendar_datetime(
        "Memory Beta reports on June 24, 2026", reference_time=now
    )

    assert parsed == datetime(2026, 6, 24, 20, tzinfo=UTC)
    assert (
        parse_explicit_calendar_datetime(
            "Memory Beta reports Wednesday", reference_time=now
        )
        is None
    )


def test_lookahead_calendar_parser_respects_horizon():
    now = datetime(2026, 6, 23, 12, tzinfo=UTC)
    horizon = now + timedelta(days=7)

    assert lookahead_calendar_datetime(
        "MEMB earnings June 24, 2026", now=now, horizon=horizon
    )
    assert (
        lookahead_calendar_datetime(
            "MEMB earnings July 24, 2026", now=now, horizon=horizon
        )
        is None
    )


def test_extraction_event_time_uses_stated_event_date_not_publication_time():
    evidence = SimpleNamespace(
        public_time=datetime(2026, 6, 22, 9, tzinfo=UTC),
        ingest_time=datetime(2026, 6, 22, 10, tzinfo=UTC),
    )

    event_time = ExtractionWorker._event_time_from_payload(
        {"event_time_raw": "June 24, 2026"},
        evidence,
    )

    assert event_time == datetime(2026, 6, 24, 20, tzinfo=UTC)
    assert (
        ExtractionWorker._event_time_from_payload({"event_time_raw": None}, evidence)
        is None
    )


def test_extraction_schema_requires_all_strict_object_properties():
    event_items = EXTRACTION_SCHEMA["properties"]["events"]["items"]
    claim_items = EXTRACTION_SCHEMA["properties"]["claims"]["items"]

    assert set(event_items["required"]) == set(event_items["properties"])
    assert set(claim_items["required"]) == set(claim_items["properties"])


def test_scheduler_defaults_coalesce_stale_interval_runs():
    coordinator = AutomationCoordinator()

    assert coordinator.scheduler._job_defaults["coalesce"] is True
    assert coordinator.scheduler._job_defaults["max_instances"] == 1
    assert coordinator.scheduler._job_defaults["misfire_grace_time"] == 60


@pytest.mark.asyncio
async def test_scheduled_job_marks_shutdown_cancellation_as_lifecycle_state():
    coordinator = AutomationCoordinator()
    coordinator.telemetry["research_loop"] = JobTelemetry(
        "research_loop", interval_seconds=60
    )
    coordinator._shutting_down = True

    async def cancelled_job():
        raise asyncio.CancelledError

    await coordinator._scheduled_job("research_loop", cancelled_job)()

    assert coordinator.telemetry["research_loop"].last_status == "cancelled"
    assert coordinator.telemetry["research_loop"].detail == "shutdown_cancelled"


@pytest.mark.asyncio
async def test_scheduled_job_reraises_non_shutdown_cancellation():
    coordinator = AutomationCoordinator()
    coordinator.telemetry["research_loop"] = JobTelemetry(
        "research_loop", interval_seconds=60
    )

    async def cancelled_job():
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await coordinator._scheduled_job("research_loop", cancelled_job)()
