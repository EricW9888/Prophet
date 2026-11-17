import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

from sqlalchemy import select

from investos.models.evidence import EvidenceProcessingState
from investos.models.knowledge import Fact
from investos.services.agent import AgentService
from investos.services.agent_action_log import AgentActionLogService
from investos.services.operating_state import OperatingStateService
from investos.workers.extraction import ExtractionWorker
from tests.test_extraction_retention import extraction_payload
from tests.test_investor_workflows import workflow_session
from tests.test_source_relationships import source_fixture


async def test_new_unheld_news_reaches_planner_with_original_qualification(
    workflow_session, monkeypatch
):
    session = workflow_session
    subject = f"Helix Delivery {uuid4().hex}"
    quote = f"{subject}'s operating license has not been granted."
    text = f"Earlier report: approval granted. Correction: {quote}"
    evidence, item = await source_fixture(session, text)
    evidence.source_item_type = "news"
    evidence.is_processed = False
    evidence.raw_content_ref = "synthetic/news.txt"
    evidence.public_time = datetime.now(UTC) - timedelta(days=3)
    item.processing_status = "extraction_pending"
    await session.flush()
    payload = extraction_payload(subject)
    payload["facts"] = [
        {**payload["facts"][0], "statement": quote, "supporting_quote": quote}
    ]
    for kind in ("events", "claims", "fundamental_metrics", "market_setup_signals"):
        payload[kind] = []
    monkeypatch.setattr(
        "investos.workers.extraction.call_llm_json", AsyncMock(return_value=payload)
    )
    for target in (
        "CoverageWorker.audit_subject_coverage",
        "SourceLearningService.learn_from_source",
        "OperatingLoopService.refresh_subject",
        "WatcherService.evaluate_new_evidence",
    ):
        monkeypatch.setattr(
            f"investos.workers.extraction.{target}", AsyncMock(return_value={})
        )
    worker = ExtractionWorker(session)
    worker.storage.get_object = AsyncMock(return_value=text.encode())
    extraction = await worker.process_evidence(evidence.id)
    await session.flush()
    state = await session.scalar(
        select(EvidenceProcessingState).where(
            EvidenceProcessingState.raw_evidence_id == evidence.id
        )
    )
    assert evidence.is_processed and state.extraction_status == "completed", extraction
    assert state.persisted_object_count > 0, extraction
    assert item.processing_status in (
        "processed",
        "processed_adjacent_context",
    ), item.processing_status
    monitor = await OperatingStateService(session).portfolio_monitor_payload()
    changes = monitor.get("recent_evidence_changes", [])
    assert str(evidence.id) in [row["evidence_id"] for row in changes]
    change = next(row for row in changes if row["evidence_id"] == str(evidence.id))
    assert change["source_item_type"] == "news"
    assert change["public_time"] == evidence.public_time.isoformat()
    assert change["observed_at"] != change["public_time"]
    assertion = change["assertions"][0]
    assert assertion["source_passage"]["quote_selector"]["exact"] == quote
    assert "Correction:" in assertion["source_passage"]["quote_selector"]["prefix"]
    assert assertion["source_passage"]["semantic_verification"] is False

    monkeypatch.setattr(
        OperatingStateService,
        "portfolio_state_payload",
        AsyncMock(return_value={"top_holdings": [], "quantity_measurements": []}),
    )
    monkeypatch.setattr(
        OperatingStateService,
        "review_queue_payload",
        AsyncMock(return_value={"top_items": []}),
    )
    monkeypatch.setattr(
        OperatingStateService,
        "lessons_payload",
        AsyncMock(return_value={"recent_lessons": []}),
    )
    monkeypatch.setattr(
        AgentActionLogService, "has_recent_fingerprint", lambda *args, **kwargs: False
    )
    captured = []

    async def planner(**kwargs):
        captured.append(json.loads(kwargs["user_prompt"]))
        return {"strategic_goals": []}

    monkeypatch.setattr("investos.services.agent.call_llm_json", planner)
    first = await AgentService(session).run_strategic_planning_cycle()
    assert captured[0]["portfolio_monitor"]["recent_evidence_changes"] == changes
    assert await OperatingStateService(session)._recent_evidence_changes() == changes
    fact = await session.get(Fact, UUID(assertion["id"]))
    fact.statement += " Derived assertion edited."
    await session.flush()
    changed = await AgentService(session).run_strategic_planning_cycle()
    assert changed["decision_fingerprint"] != first["decision_fingerprint"]
    assert (
        captured[-1]["portfolio_monitor"]["recent_evidence_changes"][0]["assertions"][
            0
        ]["source_passage"]["status"]
        == "assertion_changed"
    )
    fact.statement = quote
    await session.flush()

    evidence.source_item_type = "conversation_turn"
    await session.flush()
    assert await OperatingStateService(session)._recent_evidence_changes() == []
    evidence.source_item_type = "news"
    item.processing_status = "quarantined_uncertain"
    await session.flush()
    assert await OperatingStateService(session)._recent_evidence_changes() == []
    item.processing_status = "processed"

    state = await session.scalar(
        select(EvidenceProcessingState).where(
            EvidenceProcessingState.raw_evidence_id == evidence.id
        )
    )
    state.extraction_status = "pending"
    await session.flush()
    pending = await OperatingStateService(session).portfolio_monitor_payload()
    assert str(evidence.id) not in [
        row["evidence_id"] for row in pending["recent_evidence_changes"]
    ]
    quiet = await AgentService(session).run_strategic_planning_cycle()
    assert quiet["detail"] == "no_material_planning_signal"
    assert len(captured) == 2
