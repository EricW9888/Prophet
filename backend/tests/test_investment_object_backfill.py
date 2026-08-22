from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from investos.models.market_setup import MarketSetupSignal
from investos.services.investment_object_backfill import (
    BACKFILL_EXTRACTOR_VERSION,
    InvestmentObjectBackfillService,
)
from investos.workers.extraction import (
    INVESTMENT_OBJECT_SCHEMA,
    MAX_FUNDAMENTAL_METRICS,
    MAX_MARKET_SETUP_SIGNALS,
    ExtractionWorker,
)


def test_backfill_contract_is_narrow_and_versioned():
    assert BACKFILL_EXTRACTOR_VERSION >= 1
    assert INVESTMENT_OBJECT_SCHEMA["required"] == [
        "fundamental_metrics",
        "market_setup_signals",
    ]
    assert set(INVESTMENT_OBJECT_SCHEMA["properties"]) == {
        "fundamental_metrics",
        "market_setup_signals",
    }
    assert (
        INVESTMENT_OBJECT_SCHEMA["properties"]["fundamental_metrics"]["maxItems"]
        == MAX_FUNDAMENTAL_METRICS
    )


def test_recurring_backfill_window_excludes_completed_and_structured_evidence():
    statement = InvestmentObjectBackfillService._candidate_statement(
        evidence_id=None,
        retry_completed=False,
    )
    compiled = statement.compile(dialect=postgresql.dialect())
    sql = str(compiled)

    assert "investment_object_backfill" in compiled.params.values()
    assert "fundamental_metrics" in sql
    assert "market_setup_signals" in sql
    assert sql.count("NOT (EXISTS") == 2


def test_targeted_backfill_keeps_completed_item_visible_for_auditing():
    statement = InvestmentObjectBackfillService._candidate_statement(
        evidence_id=uuid4(),
        retry_completed=False,
    )
    sql = str(statement.compile(dialect=postgresql.dialect()))

    assert "raw_evidence.id" in sql
    assert "investment_object_backfill" not in sql
    assert "fundamental_metrics" not in sql


def test_terminal_skip_checkpoint_is_versioned_and_auditable():
    evidence = SimpleNamespace(metadata_json={"existing": "preserved"})

    InvestmentObjectBackfillService._mark_checkpoint(
        evidence,
        status="skipped_unresolved_subject",
    )

    assert evidence.metadata_json["existing"] == "preserved"
    checkpoint = evidence.metadata_json["investment_object_backfill"]
    assert checkpoint["extractor_version"] == BACKFILL_EXTRACTOR_VERSION
    assert checkpoint["status"] == "skipped_unresolved_subject"
    assert checkpoint["completed_at"]
    assert (
        INVESTMENT_OBJECT_SCHEMA["properties"]["market_setup_signals"]["maxItems"]
        == MAX_MARKET_SETUP_SIGNALS
    )


def test_quality_gates_require_evidence_value_relevance_and_next_test():
    metric = {
        "metric_name": "Net debt leverage",
        "value_text": "3.8x EBITDA",
        "numeric_value": 3.8,
        "confidence": 0.82,
        "investment_relevance": "Refinancing pressure can compress equity value.",
        "next_test": "Compare maturities with free cash flow.",
    }
    signal = {
        "signal_name": "Expected-overperformance hurdle",
        "setup_context": "Investors expected a larger revenue beat.",
        "confidence": 0.81,
        "investment_relevance": "The hurdle controls the post-print reaction.",
        "next_test": "Compare the whisper with reported guidance.",
    }

    assert InvestmentObjectBackfillService._qualified_metric(metric, 0.8) is True
    assert InvestmentObjectBackfillService._qualified_signal(signal, 0.8) is True
    assert (
        InvestmentObjectBackfillService._qualified_metric(
            metric | {"next_test": ""}, 0.8
        )
        is False
    )
    assert (
        InvestmentObjectBackfillService._qualified_signal(
            signal | {"confidence": 0.79}, 0.8
        )
        is False
    )


@pytest.mark.asyncio
async def test_reindex_extraction_reuses_structured_contract_without_other_objects(
    monkeypatch,
):
    captured = {}

    async def fake_call_llm_json(*, system_prompt, user_prompt, schema, **_kwargs):
        captured.update(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            schema=schema,
        )
        return {
            "fundamental_metrics": [
                {
                    "subject_name": "Example Corp",
                    "ticker": "EXM",
                    "relationship_to_primary_subject": "direct",
                    "metric_name": f"Metric {index}",
                    "metric_family": "custom",
                    "value_text": str(index),
                    "numeric_value": index,
                    "unit": None,
                    "currency": None,
                    "period_label": "FY2026",
                    "as_of_raw": "2026-07-01",
                    "direction": None,
                    "confidence": 0.9,
                    "investment_relevance": "Material to the investment case.",
                    "next_test": "Check the next filing.",
                }
                for index in range(MAX_FUNDAMENTAL_METRICS + 2)
            ],
            "market_setup_signals": [
                {
                    "subject_name": "Example Corp",
                    "ticker": "EXM",
                    "relationship_to_primary_subject": "direct",
                    "signal_name": f"Signal {index}",
                    "signal_family": "custom",
                    "setup_context": "The market expected more.",
                    "actual_context": None,
                    "price_reaction": None,
                    "value_text": None,
                    "numeric_value": None,
                    "unit": None,
                    "currency": None,
                    "period_label": None,
                    "as_of_raw": "2026-07-01",
                    "direction": None,
                    "confidence": 0.9,
                    "investment_relevance": "Changes the hurdle.",
                    "next_test": "Check estimates.",
                }
                for index in range(MAX_MARKET_SETUP_SIGNALS + 2)
            ],
        }

    monkeypatch.setattr("investos.workers.extraction.call_llm_json", fake_call_llm_json)
    result = await ExtractionWorker(session=None).extract_investment_objects(
        "Example earnings",
        "The source reported a dated metric and the market expected a larger beat.",
    )

    assert captured["schema"] is INVESTMENT_OBJECT_SCHEMA
    assert "return only fundamental_metrics" in captured["system_prompt"]
    assert "Example earnings" in captured["user_prompt"]
    assert len(result["fundamental_metrics"]) == MAX_FUNDAMENTAL_METRICS
    assert len(result["market_setup_signals"]) == MAX_MARKET_SETUP_SIGNALS
    assert set(result) == {"fundamental_metrics", "market_setup_signals"}


@pytest.mark.asyncio
async def test_unresolved_peer_or_index_object_does_not_inherit_primary_company():
    class EmptyResult:
        def first(self):
            return None

        def scalar_one_or_none(self):
            return None

    session = SimpleNamespace(execute=AsyncMock(return_value=EmptyResult()))
    primary_id = uuid4()

    resolved = await ExtractionWorker(session).resolve_investment_object_subject(
        {
            "subject_name": "VanEck Semiconductor ETF",
            "ticker": "SMH",
            "relationship_to_primary_subject": "sector read-through",
        },
        default_subject_type="entity",
        default_subject_id=primary_id,
    )

    assert resolved["subject_type"] == "portfolio"
    assert resolved["subject_id"] is None
    assert resolved["entity_id"] is None
    assert resolved["ticker"] == "SMH"
    assert resolved["relationship"] == "sector read-through"


@pytest.mark.asyncio
async def test_peer_or_sector_object_keeps_explicit_context_edge_to_primary_subject():
    worker = ExtractionWorker(session=SimpleNamespace())
    worker.edge_state.ensure_edge = AsyncMock()
    primary_id = uuid4()
    object_id = uuid4()

    await worker.link_investment_object_context(
        object_type="market_setup_signal",
        object_id=object_id,
        object_subject={
            "subject_type": "portfolio",
            "subject_id": None,
            "subject_name": "VanEck Semiconductor ETF",
            "ticker": "SMH",
        },
        primary_subject_type="entity",
        primary_subject_id=primary_id,
        payload={
            "confidence": 0.9,
            "investment_relevance": "Semiconductor risk transmits to the holding.",
            "relationship_to_primary_subject": "sector read-through",
        },
    )

    worker.edge_state.ensure_edge.assert_awaited_once()
    kwargs = worker.edge_state.ensure_edge.await_args.kwargs
    assert kwargs["source_type"] == "market_setup_signal"
    assert kwargs["target_type"] == "entity"
    assert kwargs["target_id"] == primary_id
    assert kwargs["relationship_type"] == "context_for"
    assert kwargs["properties"]["object_ticker"] == "SMH"


def test_dated_value_preserves_explicit_source_date():
    evidence = type(
        "Evidence",
        (),
        {
            "public_time": datetime(2026, 7, 5, tzinfo=timezone.utc),
            "ingest_time": datetime(2026, 7, 6, tzinfo=timezone.utc),
            "event_time": None,
        },
    )()

    value = ExtractionWorker.dated_value("2026-06-30", evidence)

    assert value is not None
    assert value.date().isoformat() == "2026-06-30"


def test_undated_evidence_requires_an_explicit_object_date():
    evidence = type(
        "Evidence",
        (),
        {"public_time": None, "event_time": None, "ingest_time": None},
    )()

    assert (
        InvestmentObjectBackfillService._has_source_date(
            {"as_of_raw": "2026-06-30"}, evidence
        )
        is True
    )
    assert (
        InvestmentObjectBackfillService._has_source_date({"as_of_raw": None}, evidence)
        is False
    )


def test_exact_signal_deduplication_is_subject_and_setup_aware():
    first = MarketSetupSignal(
        ticker="MEMB",
        signal_name="HBM Supply Sold Out",
        setup_context="HBM supply remains sold out into 2026",
    )
    relabeled_family = MarketSetupSignal(
        ticker="MEMB",
        signal_name="Different label for the same setup",
        setup_context="  HBM supply remains sold out into 2026  ",
    )
    other_subject = MarketSetupSignal(
        ticker="MEMA",
        signal_name="HBM Supply Sold Out",
        setup_context="HBM supply remains sold out into 2026",
    )

    key = InvestmentObjectBackfillService._exact_signal_key
    assert key(first) == key(relabeled_family)
    assert key(first) != key(other_subject)
