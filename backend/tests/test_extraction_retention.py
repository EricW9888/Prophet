from copy import deepcopy
from unittest.mock import AsyncMock
from uuid import uuid4

import fastjsonschema
import pytest
from sqlalchemy import select

from investos.models.fundamental import FundamentalMetric
from investos.models.graph import Edge
from investos.models.knowledge import Claim, Event, Fact
from investos.models.market_setup import MarketSetupSignal
from investos.workers.extraction import EXTRACTION_SCHEMA, ExtractionWorker
from tests.test_investor_workflows import workflow_session
from tests.test_source_relationships import source_fixture


def extraction_payload(subject):
    common = {
        "subject_name": subject,
        "subject_type": "entity",
        "entity_type": "company",
        "relationship_to_primary_subject": "direct",
        "event_time_raw": None,
        "target_horizon": "strategic",
        "horizon_reasoning": "Source observation",
    }
    observation = {
        **common,
        "confidence": 0.9,
        "tier": "source_fact",
        "importance": "medium",
        "contradiction_role": "neutral",
        "valid_until_raw": None,
    }
    metric = {
        "subject_name": subject,
        "ticker": None,
        "relationship_to_primary_subject": "direct",
        "value_text": "1 unit",
        "numeric_value": 1,
        "unit": "units",
        "currency": None,
        "period_label": None,
        "as_of_raw": None,
        "direction": None,
        "confidence": 0.9,
        "investment_relevance": "Reported operating measure",
        "next_test": "Compare the next source report",
    }
    return {
        "primary_subject": subject,
        "subject_type": "entity",
        "entity_type": "company",
        "summary": "Source reports multiple distinct operating observations.",
        "relevance_assessment": {
            "status": "relevant",
            "target_supported": True,
            "reason": "The source names this subject directly.",
            "supported_subjects": [subject],
        },
        "relationships": [],
        "events": [
            {
                **common,
                "title": f"Event {i}",
                "description": f"Event description {i}",
                "event_type": "operating_update",
            }
            for i in range(7)
        ],
        "facts": [
            {
                **observation,
                "statement": f"Reported operating observation {i}",
                "fact_type": "operating_update",
            }
            for i in range(11)
        ],
        "claims": [
            {
                **observation,
                "statement": f"Management expects operating change {i}",
                "claim_type": "management_expectation",
                "sentiment": None,
            }
            for i in range(9)
        ],
        "fundamental_metrics": [
            {
                **metric,
                "metric_name": f"Operating metric {i}",
                "metric_family": "operating",
            }
            for i in range(14)
        ],
        "market_setup_signals": [
            {
                **metric,
                "signal_name": f"Market expectation {i}",
                "signal_family": "expectations",
                "setup_context": f"Source reports market expectation {i}",
                "actual_context": None,
                "price_reaction": None,
            }
            for i in range(10)
        ],
    }


@pytest.mark.parametrize("investment_objects", [False, True])
async def test_extraction_retains_all_generated_objects_through_database(
    workflow_session, monkeypatch, investment_objects
):
    subject = f"Synthetic operating company {uuid4().hex}"
    payload = extraction_payload(subject)
    # Keep the persistence reproducer within the old provider schema; investment
    # object representation limits are exercised independently below.
    if not investment_objects:
        payload["fundamental_metrics"] = []
        payload["market_setup_signals"] = []
    fastjsonschema.validate(EXTRACTION_SCHEMA, payload)
    for target in [
        "CoverageWorker.audit_subject_coverage",
        "SourceLearningService.learn_from_source",
        "OperatingLoopService.refresh_subject",
        "WatcherService.evaluate_new_evidence",
    ]:
        monkeypatch.setattr(f"investos.workers.extraction.{target}", AsyncMock())
    monkeypatch.setattr(
        "investos.workers.extraction.call_llm_json",
        AsyncMock(return_value=deepcopy(payload)),
    )
    session = workflow_session
    evidence, item = await source_fixture(session, "Synthetic source observations")
    evidence.is_processed = False
    evidence.raw_content_ref = "synthetic/source.txt"
    item.processing_status = "extraction_pending"
    worker = ExtractionWorker(session)
    worker.storage.get_object = AsyncMock(return_value=b"Synthetic source observations")
    outcome = await worker.process_evidence(evidence.id)
    for model, field in [
        (Event, "events"),
        (Fact, "facts"),
        (Claim, "claims"),
        (FundamentalMetric, "fundamental_metrics"),
        (MarketSetupSignal, "market_setup_signals"),
    ]:
        if model is Event:
            statement = select(Event).where(
                Event.id.in_(
                    select(Edge.source_id).where(
                        Edge.source_type == "event",
                        Edge.target_type == "source_item",
                        Edge.target_id == item.id,
                    )
                )
            )
        else:
            statement = select(model).where(model.source_item_id == item.id)
        stored = list(await session.scalars(statement))
        assert len(stored) == len(payload[field]), (field, outcome)
        if model in {Fact, Claim}:
            assert all(row.promotion_eligible is False for row in stored)
    assert evidence.is_processed is True
    await session.rollback()


async def test_live_and_reindex_accept_all_generated_investment_objects(monkeypatch):
    payload = extraction_payload("Synthetic company")
    payload["relevance_assessment"]["supported_subjects"] = [
        f"Subject {i}" for i in range(15)
    ]

    async def model(**kwargs):
        candidate = (
            deepcopy(payload)
            if "primary_subject" in kwargs["schema"]["properties"]
            else {
                key: deepcopy(payload[key])
                for key in ("fundamental_metrics", "market_setup_signals")
            }
        )
        fastjsonschema.validate(kwargs["schema"], candidate)
        return candidate

    monkeypatch.setattr("investos.workers.extraction.call_llm_json", model)
    worker = ExtractionWorker(None)
    live = await worker._extract_structured_data(
        "Operating report", "Source observations"
    )
    reindex = await worker.extract_investment_objects(
        "Operating report", "Source observations"
    )
    for field in ("fundamental_metrics", "market_setup_signals"):
        assert live[field] == payload[field]
        assert reindex[field] == payload[field]
    assert (
        live["relevance_assessment"]["supported_subjects"]
        == payload["relevance_assessment"]["supported_subjects"]
    )
