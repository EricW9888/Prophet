import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from investos.api.routes.timeline import _display_time
from investos.core.dates import (
    lookahead_calendar_datetime,
    parse_explicit_calendar_datetime,
)
from investos.models.knowledge import Fact
from investos.services.automation import AutomationCoordinator, JobTelemetry
from investos.services.knowledge_time import (
    assess_knowledge_time,
    infer_expired_forecast_time,
    is_legacy_synthetic_event_time,
)
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
    assert "event_time_raw" in event_items["properties"]
    assert (
        "event_time_raw"
        in EXTRACTION_SCHEMA["properties"]["facts"]["items"]["properties"]
    )
    assert "event_time_raw" in claim_items["properties"]


def test_historical_claim_ingestion_time_is_not_presented_as_event_time():
    ingested = datetime(2026, 8, 17, tzinfo=UTC)

    assert is_legacy_synthetic_event_time(
        ingested,
        public_time=None,
        ingest_time=ingested,
    )
    temporal = assess_knowledge_time(
        "FSD profitability will bring billions to TSLA in 2021.",
        event_time=None,
        public_time=None,
        ingest_time=ingested,
        item_type="claim",
        now=datetime(2026, 8, 29, tzinfo=UTC),
    )

    assert temporal.status == "outcome_due"
    assert temporal.novelty == "historical"
    assert temporal.referenced_years == (2021,)


def test_historical_fact_without_exact_date_stays_undated_history():
    temporal = assess_knowledge_time(
        "The metric was reported during the Q1 2020 earnings call.",
        event_time=None,
        public_time=None,
        ingest_time=datetime(2026, 8, 17, tzinfo=UTC),
        item_type="fact",
        now=datetime(2026, 8, 29, tzinfo=UTC),
    )

    assert temporal.status == "historical"
    assert temporal.novelty == "historical"
    assert "2020" in temporal.explanation


def test_old_source_is_historical_even_when_event_and_publication_dates_match():
    published = datetime(2020, 4, 29, tzinfo=UTC)

    temporal = assess_knowledge_time(
        "The metric was reported during the Q1 2020 earnings call.",
        event_time=published,
        public_time=published,
        ingest_time=datetime(2026, 8, 17, tzinfo=UTC),
        item_type="fact",
        now=datetime(2026, 8, 29, tzinfo=UTC),
    )

    assert temporal.status == "historical"
    assert temporal.novelty == "historical"


def test_old_undated_source_is_historical_without_a_year_in_the_statement():
    temporal = assess_knowledge_time(
        "Management described the product as strategically important.",
        event_time=None,
        public_time=datetime(2020, 4, 29, tzinfo=UTC),
        ingest_time=datetime(2026, 8, 17, tzinfo=UTC),
        item_type="fact",
        now=datetime(2026, 8, 29, tzinfo=UTC),
    )

    assert temporal.status == "historical"
    assert temporal.novelty == "historical"


def test_expired_forecast_infers_target_end_without_inventing_claim_date():
    reference = datetime(2026, 8, 17, tzinfo=UTC)

    target = infer_expired_forecast_time(
        "Revenue will exceed $2 billion in 2021.",
        reference_time=reference,
    )

    assert target == datetime(2021, 12, 31, 23, 59, 59, tzinfo=UTC)


def test_timeline_uses_ingested_label_for_legacy_synthetic_fact_time():
    ingested = datetime(2026, 8, 17, tzinfo=UTC)
    item = Fact(
        event_time=ingested,
        public_time=None,
        ingest_time=ingested,
        created_at=ingested,
    )

    assert _display_time(item) == (
        ingested,
        "ingested",
    )


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
