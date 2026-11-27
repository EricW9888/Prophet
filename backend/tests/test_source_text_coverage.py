import asyncio
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import select

from investos.core.prompting import compact_packet_context
from investos.core.provider_health import ProviderHealthStore
from investos.models.entity import Entity, Security
from investos.models.evidence import EvidenceProcessingState, SourceItem
from investos.models.fundamental import FundamentalMetric
from investos.models.knowledge import Fact
from investos.models.market_setup import MarketSetupSignal
from investos.models.source import Source
from investos.services.evidence_processing import EvidenceProcessingService
from investos.services.fundamentals import FundamentalMetricService
from investos.services.investment_object_backfill import InvestmentObjectBackfillService
from investos.services.retrieval import RetrievalService
from investos.services.source_text import (
    SourceTextChanged,
    complete_source_window,
    next_source_window,
    prepare_source_text,
    retain_new_outputs,
)
from investos.workers.extraction import ExtractionWorker
from tests.test_extraction_retention import extraction_payload
from tests.test_investor_workflows import workflow_session
from tests.test_source_relationships import relationship, source_fixture

INTRO = "The company announced a new delivery program."
MIDDLE = "The delivery program depends on an operating license."
TAIL = "The operating license has not been granted."
DOCUMENT = (
    INTRO
    + "\n\n"
    + ("Routine administrative disclosure remains unchanged.\n" * 300)
    + MIDDLE
    + "\n\n"
    + ("The report retains its previous accounting policies.\n" * 140)
    + TAIL
)


@pytest.fixture
def isolated_processing(monkeypatch):
    callbacks = {}
    for target in (
        "CoverageWorker.audit_subject_coverage",
        "SourceLearningService.learn_from_source",
        "OperatingLoopService.refresh_subject",
        "WatcherService.evaluate_new_evidence",
    ):
        callback = AsyncMock(return_value={})
        callbacks[target] = callback
        monkeypatch.setattr(f"investos.workers.extraction.{target}", callback)
    monkeypatch.setattr(ProviderHealthStore, "require_available", lambda: None)
    return callbacks


async def setup_source(session):
    subject = f"Synthetic delivery company {uuid4().hex}"
    evidence, item = await source_fixture(session, DOCUMENT)
    evidence.is_processed = False
    evidence.raw_content_ref = "synthetic/full-source.txt"
    evidence.public_time = datetime(2026, 11, 1, tzinfo=UTC)
    evidence.metadata_json = {
        "subject_name": subject,
        "subject_type": "entity",
        "research_question": "What changed for this company?",
    }
    item.processing_status = "extraction_pending"
    await session.flush()
    return evidence, item, subject


def model_for(subject, seen):
    template = extraction_payload(subject)

    async def model(**kwargs):
        text = kwargs["user_prompt"].split("Evidence text:\n", 1)[1]
        seen.append(text)
        result = deepcopy(template)
        for key in (
            "events",
            "facts",
            "claims",
            "fundamental_metrics",
            "market_setup_signals",
        ):
            result[key] = []
        for statement in (INTRO, MIDDLE, TAIL):
            if statement in text:
                result["facts"].append({**template["facts"][0], "statement": statement})
        result["summary"] = (
            " ".join(fact["statement"] for fact in result["facts"])
            or "The window contains routine disclosure."
        )
        return result

    return model


def worker_for(session, text=DOCUMENT):
    worker = ExtractionWorker(session)
    worker.storage.get_object = AsyncMock(return_value=text.encode())
    return worker


async def test_normal_extraction_continues_into_missing_middle_and_tail(
    workflow_session, monkeypatch, isolated_processing
):
    session = workflow_session
    evidence, item, subject = await setup_source(session)
    seen = []
    monkeypatch.setattr(
        "investos.workers.extraction.call_llm_json", model_for(subject, seen)
    )
    first = await worker_for(session).process_evidence(evidence.id)
    assert evidence.is_processed is False
    assert first["processing"]["source_text_coverage"]["status"] == "partial"
    assert item.extracted_text == DOCUMENT
    isolated_processing["OperatingLoopService.refresh_subject"].assert_not_awaited()
    isolated_processing["WatcherService.evaluate_new_evidence"].assert_not_awaited()
    outcomes = []
    for _ in range(10):
        if evidence.is_processed:
            break
        # Construct a fresh worker on every continuation, as after a restart.
        outcomes.append(await worker_for(session).process_evidence(evidence.id))
    assert evidence.is_processed, outcomes
    facts = list(
        await session.scalars(select(Fact).where(Fact.source_item_id == item.id))
    )
    assert {fact.statement for fact in facts} == {INTRO, MIDDLE, TAIL}
    assert len(facts) == 3
    assert all(fact.promotion_eligible is False for fact in facts)
    state = await session.scalar(
        select(EvidenceProcessingState).where(
            EvidenceProcessingState.raw_evidence_id == evidence.id
        )
    )
    coverage = EvidenceProcessingService.summary(state)["source_text_coverage"]
    await session.refresh(state)
    assert state.source_text_state_json["next_offset"] == len(DOCUMENT)
    assert coverage["status"] == "all_text_submitted"
    assert coverage["submitted_chars"] == len(DOCUMENT)
    assert coverage["remaining_chars"] == 0
    assert len(coverage["windows"]) == len(seen)
    assert all(
        b["start"] == a["end"]
        for a, b in zip(coverage["windows"], coverage["windows"][1:])
    )
    assert evidence.public_time == datetime(2026, 11, 1, tzinfo=UTC)
    isolated_processing["OperatingLoopService.refresh_subject"].assert_awaited_once()
    context = RetrievalService._source_context_from_evidence(
        evidence, await session.get(Source, evidence.source_id)
    )
    assert context["source_text_coverage"]["status"] == "all_text_submitted"
    compacted = compact_packet_context(
        {
            "direct_evidence": [
                {
                    "id": str(facts[0].id),
                    "type": "fact",
                    "text": INTRO,
                    "source": context,
                }
            ]
        }
    )
    assert compacted["direct_evidence"][0]["source"]["source_text_coverage"][
        "submitted_chars"
    ] == len(DOCUMENT)


async def test_interrupted_window_reuses_committed_progress_and_checks_source_identity(
    workflow_session, monkeypatch, isolated_processing
):
    session = workflow_session
    evidence, item, subject = await setup_source(session)
    seen = []
    normal_model = model_for(subject, seen)
    monkeypatch.setattr("investos.workers.extraction.call_llm_json", normal_model)
    await worker_for(session).process_evidence(evidence.id)
    state = await session.scalar(
        select(EvidenceProcessingState).where(
            EvidenceProcessingState.raw_evidence_id == evidence.id
        )
    )
    saved_offset = state.source_text_state_json["next_offset"]
    entered = asyncio.Event()

    async def interrupted(**kwargs):
        entered.set()
        await asyncio.Future()

    monkeypatch.setattr("investos.workers.extraction.call_llm_json", interrupted)
    task = asyncio.create_task(worker_for(session).process_evidence(evidence.id))
    await asyncio.wait_for(entered.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert state.source_text_state_json["next_offset"] == saved_offset
    state.last_extraction_attempt_at = datetime.now(UTC) - timedelta(hours=1)
    await session.commit()
    monkeypatch.setattr("investos.workers.extraction.call_llm_json", normal_model)
    changed = await worker_for(session, DOCUMENT + " changed").process_evidence(
        evidence.id
    )
    assert changed["source_changed"] is True
    assert state.source_text_state_json["next_offset"] == saved_offset
    assert len(seen) == 1
    await EvidenceProcessingService(session).schedule_operator_retry(evidence.id)
    for _ in range(10):
        if evidence.is_processed:
            break
        await worker_for(session).process_evidence(evidence.id)
    assert evidence.is_processed
    assert len(seen) == len(state.source_text_state_json["windows"])
    assert (
        len(
            list(
                await session.scalars(
                    select(Fact).where(Fact.source_item_id == item.id)
                )
            )
        )
        == 3
    )


async def test_window_objects_and_progress_roll_back_together(
    workflow_session, monkeypatch, isolated_processing
):
    session = workflow_session
    evidence, item, subject = await setup_source(session)
    payload = extraction_payload(subject)
    evidence_id, item_id = evidence.id, item.id
    monkeypatch.setattr(
        "investos.workers.extraction.call_llm_json", AsyncMock(return_value=payload)
    )
    monkeypatch.setattr(
        "investos.workers.extraction.MarketSetupSignalService.create_signal",
        AsyncMock(side_effect=RuntimeError("injected persistence failure")),
    )
    with pytest.raises(RuntimeError, match="injected persistence failure"):
        await worker_for(session).process_evidence(evidence.id)
    await session.rollback()
    state = await session.scalar(
        select(EvidenceProcessingState).where(
            EvidenceProcessingState.raw_evidence_id == evidence_id
        )
    )
    assert state.source_text_state_json["next_offset"] == 0
    assert not list(
        await session.scalars(select(Fact).where(Fact.source_item_id == item_id))
    )
    assert not list(
        await session.scalars(
            select(FundamentalMetric).where(
                FundamentalMetric.raw_evidence_id == evidence_id
            )
        )
    )


async def test_historical_investment_windows_resume_within_existing_call_budget(
    workflow_session,
):
    session = workflow_session
    evidence, item, subject_name = await setup_source(session)
    evidence.is_processed = True
    item.processing_status = "processed"
    # The legacy extracted text can be a short prefix. The retained original wins.
    item.extracted_text = DOCUMENT[:4000]
    entity = Entity(name=subject_name, entity_type="company")
    session.add(entity)
    await session.flush()
    security = Security(
        entity_id=entity.id,
        ticker=f"SYN{uuid4().hex[:8]}",
        asset_class="equity",
        instrument_type="common_stock",
    )
    session.add(security)
    await session.flush()
    subject = {
        "entity_id": entity.id,
        "security_id": security.id,
        "ticker": security.ticker,
        "name": subject_name,
        "portfolio_relevant": True,
    }
    seen = []

    async def model(title, text, *, public=False):
        seen.append(text)
        template = extraction_payload(subject_name)["fundamental_metrics"][0]
        return {
            "fundamental_metrics": [
                {**template, "metric_name": statement}
                for statement in (INTRO, MIDDLE, TAIL)
                if statement in text
            ],
            "market_setup_signals": [],
        }

    def service():
        result = InvestmentObjectBackfillService(session)
        result.extraction.storage.get_object = AsyncMock(return_value=DOCUMENT.encode())
        result.extraction.extract_investment_objects = model
        result.market_setup._known_subject_catalog = AsyncMock(return_value=[])
        result.market_setup._match_known_subject = lambda *args: subject
        return result

    preview = await service().run(evidence_id=evidence.id, max_model_calls=1)
    assert preview["model_calls"] == 1
    assert not (evidence.metadata_json or {}).get("investment_object_backfill")
    seen.clear()
    first = await service().run(apply=True, evidence_id=evidence.id, max_model_calls=1)
    assert first["model_calls"] == 1
    assert (
        evidence.metadata_json["investment_object_backfill"]["status"]
        == "source_text_partial"
    )
    # Use normal recurring candidate admission, with a populated first-window object.
    continued = await service().run(apply=True, max_model_calls=2, portfolio_only=False)
    assert continued["model_calls"] == 2
    checkpoint = evidence.metadata_json["investment_object_backfill"]
    assert checkpoint["status"] == "completed"
    assert checkpoint["source_text_coverage"]["remaining_chars"] == 0
    metrics = list(
        await session.scalars(
            select(FundamentalMetric).where(
                FundamentalMetric.raw_evidence_id == evidence.id
            )
        )
    )
    assert {metric.metric_name for metric in metrics} == {INTRO, MIDDLE, TAIL}
    assert len(seen) == 3
    assert not list(
        await session.scalars(select(Fact).where(Fact.source_item_id == item.id))
    )
    assert not list(
        await session.scalars(
            select(MarketSetupSignal).where(
                MarketSetupSignal.raw_evidence_id == evidence.id
            )
        )
    )


async def test_relationship_reindex_does_not_skip_the_middle(workflow_session):
    session = workflow_session
    person, company = f"Casey {uuid4().hex}", f"Helix {uuid4().hex}"
    quote = f"{person} co-founded {company}."
    document = DOCUMENT.replace(MIDDLE, quote)
    evidence, item = await source_fixture(session, document)
    calls = []

    async def model(title, text, *, public=False):
        calls.append(text)
        return [relationship(person, company, quote)] if quote in text else []

    for _ in range(4):
        worker = ExtractionWorker(session)
        worker.extract_source_relationships = model
        result = await worker.reindex_source_relationships(evidence.id, apply=True)
        if result["status"] == "completed":
            break
        assert result["status"] == "source_text_partial"
    assert result["status"] == "completed"
    assert result["source_text_coverage"]["remaining_chars"] == 0
    assert len(calls) == 3


@pytest.mark.parametrize("size,overlap", [(512, 0), (512, 128), (12000, 600)])
def test_unicode_windows_cover_every_character_and_preserve_conflicts(size, overlap):
    text = ("A😀é\n\n" * 1000) + "Final qualifier."
    progress = prepare_source_text(text, None)
    while progress["next_offset"] < len(text):
        window = next_source_window(text, progress, size=size, overlap=overlap)
        assert window["end"] - window["input_start"] <= size
        complete_source_window(progress, window, {})
    assert progress["next_offset"] == len(text)
    assert (
        "".join(text[window["start"] : window["end"]] for window in progress["windows"])
        == text
    )
    first = {"facts": [{"statement": "License granted"}]}
    retain_new_outputs(first, progress)
    later = {
        "facts": [
            {"statement": "License granted"},
            {"statement": "License not granted"},
        ]
    }
    retain_new_outputs(later, progress)
    assert later["facts"] == [{"statement": "License not granted"}]
    with pytest.raises(SourceTextChanged):
        prepare_source_text(text + "changed", progress)


async def test_relevance_review_waits_for_all_windows_before_source_wide_action(
    workflow_session, monkeypatch
):
    session = workflow_session
    evidence, item, subject = await setup_source(session)
    evidence.is_processed = True
    item.processing_status = "processed"
    seen = []
    base_model = model_for(subject, seen)

    async def model(**kwargs):
        result = await base_model(**kwargs)
        if len(seen) == 1:
            result["relevance_assessment"].update(
                status="irrelevant", target_supported=False
            )
        return result

    monkeypatch.setattr("investos.workers.extraction.call_llm_json", model)
    first = await worker_for(session).reassess_evidence_relevance(evidence.id)
    assert first["reviewed"] is False and first["reason"] == "source_text_partial"
    assert item.processing_status == "processed"
    assert not (evidence.metadata_json or {}).get("knowledge_promotion_status")
    for _ in range(3):
        result = await worker_for(session).reassess_evidence_relevance(evidence.id)
        if result["reviewed"]:
            break
    assert result["reviewed"] and not result["quarantined"]
    assert result["source_text_coverage"]["remaining_chars"] == 0
    assert len(seen) == 3


@pytest.mark.parametrize("historical", [False, True])
async def test_changed_metric_value_is_not_deduplicated_as_same_source_axis(
    workflow_session, historical
):
    session = workflow_session
    evidence, item, subject_name = await setup_source(session)
    entity = Entity(name=subject_name, entity_type="company")
    session.add(entity)
    await session.flush()
    base = extraction_payload(subject_name)["fundamental_metrics"][0]
    base.update(
        metric_name="Approved capacity", period_label="FY2026", as_of_raw="2026-11-01"
    )
    values = [
        {**base, "value_text": f"{value} units", "numeric_value": value}
        for value in (100, 80, 100)
    ]
    if historical:
        await InvestmentObjectBackfillService(session)._persist_metrics(
            evidence=evidence,
            source_item=item,
            source=await session.get(Source, evidence.source_id),
            subject={"entity_id": entity.id, "security_id": None, "name": subject_name},
            payloads=values,
            result={"skipped_existing": 0},
        )
    else:
        for value in values:
            await FundamentalMetricService(session).create_metric(
                metric_name=value["metric_name"],
                metric_family=value["metric_family"],
                entity_id=entity.id,
                raw_evidence_id=evidence.id,
                source_item_id=item.id,
                value_text=value["value_text"],
                numeric_value=value["numeric_value"],
                unit=value["unit"],
                period_label=value["period_label"],
                as_of=datetime(2026, 11, 1, tzinfo=UTC),
                commit_transaction=False,
            )
    metrics = list(
        await session.scalars(
            select(FundamentalMetric).where(
                FundamentalMetric.raw_evidence_id == evidence.id
            )
        )
    )
    assert len(metrics) == 2
    assert {float(value.numeric_value) for value in metrics} == {100, 80}
