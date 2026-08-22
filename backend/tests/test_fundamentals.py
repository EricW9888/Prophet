from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from investos.models.fundamental import FundamentalMetric
from investos.services.fundamentals import FundamentalMetricService
from investos.services.reasoning import ReasoningService
from investos.workers.extraction import EXTRACTION_SCHEMA, ExtractionWorker


def test_fundamental_metric_family_inference_is_open_ended():
    assert FundamentalMetricService._family_from_metric("forward P/E") == "valuation"
    assert FundamentalMetricService._family_from_metric("ROE") == "profitability"
    assert (
        FundamentalMetricService._family_from_metric("net debt leverage")
        == "balance_sheet"
    )
    assert FundamentalMetricService._family_from_metric("HBM ASP trend") == "sector_kpi"
    assert (
        FundamentalMetricService._family_from_metric("custom platform attach metric")
        == "fundamental_metric"
    )


def test_fundamental_metric_default_staleness_depends_on_family():
    as_of = datetime(2026, 7, 1, tzinfo=timezone.utc)

    assert (
        FundamentalMetricService._default_stale_after("valuation", as_of) - as_of
    ).days == 45
    assert (
        FundamentalMetricService._default_stale_after("balance_sheet", as_of) - as_of
    ).days == 120
    assert (
        FundamentalMetricService._default_stale_after("sector_kpi", as_of) - as_of
    ).days == 180


@pytest.mark.asyncio
async def test_metric_deduplication_includes_the_measured_subject():
    result = SimpleNamespace(scalar_one_or_none=lambda: None)
    session = SimpleNamespace(execute=AsyncMock(return_value=result))
    evidence_id = uuid4()
    metric = FundamentalMetric(
        metric_name="CY26 Revenue Estimate",
        metric_family="growth",
        period_label="CY26",
        ticker="MEMB",
        raw_evidence_id=evidence_id,
    )

    await FundamentalMetricService(session)._existing_metric(metric)

    statement = session.execute.await_args.args[0]
    compiled = statement.compile()
    assert "fundamental_metrics.ticker" in str(compiled)
    assert "MEMB" in compiled.params.values()


def test_fundamental_metrics_make_packet_non_thin_and_fallback_concrete():
    service = ReasoningService(None)
    packet = {
        "query_text": "does debt matter for this holding?",
        "subject_name": "Example Corp",
        "direct_evidence": [],
        "connected_evidence": [],
        "historical_evidence": [],
        "contradiction_evidence": [],
        "lessons": [],
        "portfolio_context": {
            "subject_fundamental_metrics": [
                {
                    "id": str(uuid4()),
                    "ticker": "EXM",
                    "metric_name": "Net debt leverage",
                    "metric_family": "balance_sheet",
                    "value_text": "3.8x EBITDA",
                    "period_label": "FY2026 Q2",
                    "investment_relevance": "Higher leverage narrows refinancing flexibility if rates stay elevated.",
                    "next_test": "Check maturity schedule and interest coverage against free cash flow.",
                }
            ]
        },
    }

    assert service._is_thin_packet(packet) is False
    fallback = service._fallback_fundamental_metric_context(packet)

    assert fallback is not None
    assert "Net debt leverage" in fallback
    assert "3.8x EBITDA" in fallback
    assert "refinancing flexibility" in fallback


def test_analysis_prompt_hydrates_fundamentals_without_closed_checklist():
    prompt = ReasoningService(None)._analysis_system_prompt().lower()

    assert "fundamental_metrics" in prompt
    assert "subject_fundamental_metrics" in prompt
    assert "source-dated financial/operating evidence" in prompt
    assert "open ontology rather than a closed checklist" in prompt


def test_extraction_contract_persists_open_ended_investment_objects():
    assert "fundamental_metrics" in EXTRACTION_SCHEMA["required"]
    assert "market_setup_signals" in EXTRACTION_SCHEMA["required"]

    metric_schema = EXTRACTION_SCHEMA["properties"]["fundamental_metrics"]["items"]
    signal_schema = EXTRACTION_SCHEMA["properties"]["market_setup_signals"]["items"]
    assert metric_schema["properties"]["metric_family"] == {"type": "string"}
    assert signal_schema["properties"]["signal_family"] == {"type": "string"}
    assert "subject_name" in metric_schema["required"]
    assert "ticker" in metric_schema["required"]
    assert "relationship_to_primary_subject" in signal_schema["required"]
    assert "currency" in signal_schema["required"]

    prompt = ExtractionWorker._structured_extraction_system_prompt().lower()
    assert "first-class source-dated records" in prompt
    assert "open-ended labels, not fixed enums" in prompt
    assert "difference between what the market expected and what happened" in prompt
    assert "do not attach a peer, index, sector, or macro measurement" in prompt
