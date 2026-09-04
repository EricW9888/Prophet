from __future__ import annotations

import json
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy import delete, select

from investos.db import async_session_maker, engine
from investos.models.catalog import SourceClaimRecord
from investos.models.coverage import CoverageMap
from investos.models.evidence import RawEvidence, SourceItem
from investos.models.fundamental import FundamentalMetric
from investos.models.graph import Edge
from investos.models.knowledge import Claim, Event, Fact
from investos.models.knowledge_mutation import KnowledgeMutation
from investos.models.market_setup import MarketSetupSignal
from investos.models.source import Source
from investos.models.theme import Theme
from investos.services.agent import AgentService
from investos.services.evidence_relevance import (
    EvidenceRelevanceAssessment,
    EvidenceRelevanceService,
)
from investos.services.fundamentals import FundamentalMetricService
from investos.services.graph import GraphService
from investos.services.market_setup import MarketSetupSignalService
from investos.services.retrieval import RetrievalService
from investos.workers.extraction import ExtractionWorker


def test_unknown_relevance_status_fails_closed() -> None:
    assessment = EvidenceRelevanceAssessment.from_payload(
        {
            "status": "probably-relevant",
            "target_supported": True,
            "reason": "Unsupported provider vocabulary.",
            "supported_subjects": ["Example Co.", "Example Co."],
        }
    )

    assert assessment.status == "uncertain"
    assert assessment.knowledge_eligible is False
    assert assessment.processing_status == "quarantined_uncertain"
    assert assessment.supported_subjects == ("Example Co.",)


@pytest.mark.asyncio(loop_scope="session")
async def test_migrated_investment_objects_have_deprecation_lifecycle() -> None:
    def inspect_columns(connection):
        inspector = sa.inspect(connection)
        return {
            table_name: {column["name"] for column in inspector.get_columns(table_name)}
            for table_name in ("fundamental_metrics", "market_setup_signals")
        }

    async with engine.connect() as connection:
        columns = await connection.run_sync(inspect_columns)

    for table_columns in columns.values():
        assert {"is_deprecated", "deprecated_reason"} <= table_columns


@pytest.mark.asyncio(loop_scope="session")
async def test_quarantine_deprecates_promoted_knowledge_but_preserves_provenance(
    monkeypatch,
):
    async with async_session_maker() as session:
        source = Source(
            name=f"Relevance regression {uuid4().hex}",
            source_type="web_research",
        )
        session.add(source)
        await session.flush()
        evidence = RawEvidence(
            source_id=source.id,
            source_item_type="web_research",
            title="Near-match recall report",
            metadata_json={"subject_name": "Tesla", "subject_type": "entity"},
        )
        session.add(evidence)
        await session.flush()
        source_item = SourceItem(
            raw_evidence_id=evidence.id,
            source_id=source.id,
            extracted_text="A Ford recall report with no Tesla coverage.",
            summary="Wrong-subject report.",
            processing_status="processed",
        )
        session.add(source_item)
        await session.flush()
        subject = Theme(
            name=f"Relevance quarantine {uuid4().hex}",
            status="monitoring",
        )
        session.add(subject)
        await session.flush()
        fact = Fact(
            statement="The document contains zero mentions of Tesla.",
            fact_type="absence",
            confidence=0.98,
            source_item_id=source_item.id,
            tier="hard_fact",
            importance="critical",
            directness="secondary",
            novelty="breaking",
            contradiction_role="contradicts_consensus",
            promotion_eligible=False,
            target_horizon="strategic",
        )
        session.add(fact)
        await session.flush()
        claim = Claim(
            statement="Tesla was covered by the Ford report.",
            claim_type="source_claim",
            confidence=0.7,
            source_item_id=source_item.id,
            tier="weak_signal",
            importance="medium",
            directness="secondary",
            novelty="breaking",
            contradiction_role="neutral",
            promotion_eligible=False,
            target_horizon="strategic",
        )
        event = Event(
            title="Tesla recall",
            description="A wrongly attributed Ford recall.",
            event_type="regulatory",
        )
        derived_subject_id = uuid4()
        metric = FundamentalMetric(
            subject_type="test",
            subject_id=derived_subject_id,
            raw_evidence_id=evidence.id,
            source_item_id=source_item.id,
            metric_name="Wrong-source metric",
            metric_family="fundamental",
            freshness_status="current",
        )
        signal = MarketSetupSignal(
            subject_type="test",
            subject_id=derived_subject_id,
            raw_evidence_id=evidence.id,
            source_item_id=source_item.id,
            signal_name="Wrong-source setup",
            signal_family="expectations",
            outcome_status="unscored",
        )
        session.add_all([claim, event, metric, signal])
        await session.flush()
        session.add(
            SourceClaimRecord(
                source_id=source.id,
                claim_id=claim.id,
                claim_time=claim.created_at,
                assessment="pending",
            )
        )
        for node_type, node in (
            ("fact", fact),
            ("claim", claim),
            ("event", event),
        ):
            session.add(
                Edge(
                    source_type=node_type,
                    source_id=node.id,
                    target_type="source_item",
                    target_id=source_item.id,
                    relationship_type="extracted_from",
                )
            )
        session.add(
            Edge(
                source_type="fact",
                source_id=fact.id,
                target_type="theme",
                target_id=subject.id,
                relationship_type="supports",
            )
        )
        await session.flush()

        coverage_llm = AsyncMock(
            side_effect=AssertionError("quarantine must not run a coverage LLM audit")
        )
        monkeypatch.setattr(
            "investos.workers.coverage.call_llm_json",
            coverage_llm,
        )

        assessment = EvidenceRelevanceAssessment(
            status="irrelevant",
            target_supported=False,
            reason="The source supports Ford, not Tesla.",
            supported_subjects=("Ford Motor Company",),
        )
        deprecated = await EvidenceRelevanceService(session).apply_quarantine(
            evidence=evidence,
            source_item=source_item,
            assessment=assessment,
            actor="test_relevance_review",
        )
        await session.flush()

        mutations = (
            (
                await session.execute(
                    select(KnowledgeMutation).where(
                        KnowledgeMutation.source_id.in_([evidence.id, source_item.id])
                    )
                )
            )
            .scalars()
            .all()
        )

        claim_record = (
            await session.execute(
                select(SourceClaimRecord).where(SourceClaimRecord.claim_id == claim.id)
            )
        ).scalar_one()
        coverage = (
            await session.execute(
                select(CoverageMap).where(
                    CoverageMap.subject_type == "theme",
                    CoverageMap.subject_id == subject.id,
                )
            )
        ).scalar_one()

        assert deprecated == 5
        assert evidence.metadata_json["knowledge_promotion_status"] == "quarantined"
        assert evidence.metadata_json["subject_name"] == "Tesla"
        assert source_item.processing_status == "rejected_irrelevant"
        assert fact.is_deprecated is True
        assert claim.is_deprecated is True
        assert event.is_deprecated is True
        assert metric.is_deprecated is True
        assert signal.is_deprecated is True
        assert "supports Ford, not Tesla" in fact.deprecated_reason
        assert claim_record.assessment == "indeterminate"
        assert claim_record.next_assessment_at is None
        assert coverage.total_evidence_count == 0
        assert coverage.high_tier_evidence_count == 0
        coverage_llm.assert_not_awaited()
        assert {item.change_type for item in mutations} == {
            "quarantined",
            "deprecated",
        }
        assert await GraphService(session)._load_node("fact", fact.id) is None
        retrieval = RetrievalService(session)
        assert await retrieval._load_nodes([fact.id]) == []
        assert (
            await retrieval._matching_node_ids(
                Fact,
                Fact.statement.ilike("%zero mentions%"),
                limit=10,
            )
            == []
        )
        assert (
            await GraphService(session)._load_node("fundamental_metric", metric.id)
            is None
        )
        assert (
            await FundamentalMetricService(session).relevant_metrics(
                subject_type="test",
                subject_id=derived_subject_id,
            )
            == []
        )
        assert (
            await MarketSetupSignalService(session).relevant_signals(
                subject_type="test",
                subject_id=derived_subject_id,
            )
            == []
        )
        assert await session.get(RawEvidence, evidence.id) is evidence
        assert await session.get(SourceItem, source_item.id) is source_item

        await session.rollback()


@pytest.mark.asyncio(loop_scope="session")
async def test_adjacent_reassessment_retires_legacy_target_derivatives(monkeypatch):
    async with async_session_maker() as session:
        source = Source(
            name=f"Adjacent reassessment {uuid4().hex}",
            source_type="web_research",
        )
        session.add(source)
        await session.flush()
        evidence = RawEvidence(
            source_id=source.id,
            source_item_type="web_research",
            title="Industry context returned for a company question",
            raw_content_ref="adjacent-review.txt",
            metadata_json={"subject_name": "Example Co.", "subject_type": "entity"},
            is_processed=True,
        )
        session.add(evidence)
        await session.flush()
        source_item = SourceItem(
            raw_evidence_id=evidence.id,
            source_id=source.id,
            extracted_text="A report about another company in the same industry.",
            summary="Adjacent industry context.",
            processing_status="processed",
        )
        session.add(source_item)
        await session.flush()
        fact = Fact(
            statement="The report does not answer the Example Co. question.",
            fact_type="absence",
            confidence=0.9,
            source_item_id=source_item.id,
            tier="hard_fact",
            importance="high",
            directness="secondary",
            novelty="breaking",
            contradiction_role="neutral",
            promotion_eligible=False,
            target_horizon="strategic",
        )
        session.add(fact)
        await session.flush()
        session.add(
            Edge(
                source_type="fact",
                source_id=fact.id,
                target_type="source_item",
                target_id=source_item.id,
                relationship_type="extracted_from",
            )
        )
        await session.flush()

        worker = ExtractionWorker(session)
        worker.storage.get_object = AsyncMock(side_effect=FileNotFoundError)
        worker._extract_structured_data = AsyncMock(
            return_value={
                "relevance_assessment": {
                    "status": "adjacent",
                    "target_supported": False,
                    "reason": "The source covers another company in the same industry.",
                    "supported_subjects": ["Peer Co."],
                }
            }
        )

        result = await worker.reassess_evidence_relevance(evidence.id)

        assert result["reviewed"] is True
        assert result["quarantined"] is False
        assert result["reextraction_required"] is True
        assert result["deprecated_knowledge_count"] == 1
        assert source_item.processing_status == "processed_adjacent_context"
        assert evidence.metadata_json["knowledge_promotion_status"] == (
            "reextraction_required"
        )
        assert fact.is_deprecated is True
        assert await session.get(RawEvidence, evidence.id) is evidence
        assert await session.get(SourceItem, source_item.id) is source_item

        await session.rollback()


@pytest.mark.asyncio(loop_scope="session")
async def test_agent_executes_source_backed_relevance_reassessment(monkeypatch):
    async with async_session_maker() as session:
        source = Source(
            name=f"Agent relevance review {uuid4().hex}",
            source_type="web_research",
        )
        session.add(source)
        await session.flush()
        evidence = RawEvidence(
            source_id=source.id,
            source_item_type="web_research",
            title="Attributable evidence",
        )
        session.add(evidence)
        await session.flush()
        source_item = SourceItem(
            raw_evidence_id=evidence.id,
            source_id=source.id,
            summary="Attributable evidence",
            processing_status="processed",
        )
        session.add(source_item)
        await session.flush()
        fact = Fact(
            statement="A challenged saved statement.",
            fact_type="source_fact",
            confidence=0.8,
            source_item_id=source_item.id,
            tier="strong_derived",
            importance="medium",
            directness="secondary",
            novelty="breaking",
            contradiction_role="neutral",
            promotion_eligible=False,
            target_horizon="strategic",
        )
        session.add(fact)
        await session.flush()

        tool_router = AsyncMock(
            return_value={
                "tool_calls": [
                    {
                        "function": {
                            "name": "reassess_knowledge_relevance",
                            "arguments": json.dumps(
                                {"node_type": "fact", "node_id": str(fact.id)}
                            ),
                        }
                    }
                ]
            }
        )
        reassess = AsyncMock(
            return_value={
                "reviewed": True,
                "quarantined": True,
                "deprecated_knowledge_count": 1,
            }
        )
        monkeypatch.setattr("investos.services.agent.call_llm_tools", tool_router)
        monkeypatch.setattr(
            ExtractionWorker,
            "reassess_evidence_relevance",
            reassess,
        )
        service = AgentService(session)
        service._llm_operating_answer = AsyncMock(
            return_value={
                "assistant_message": "I re-read the source and quarantined it."
            }
        )

        result = await service._maybe_operating_context_answer(
            session_id=uuid4(),
            message=f"This saved fact looks unrelated: {fact.id}",
            resolved_subject_id=uuid4(),
            resolved_subject_type="portfolio",
            resolved_subject_name="Portfolio",
            allow_actions=True,
        )

        assert result is not None
        assert result["operating_query_type"] == "knowledge_relevance_review"
        reassess.assert_awaited_once_with(evidence.id)
        await session.rollback()


@pytest.mark.asyncio(loop_scope="session")
async def test_provider_failure_leaves_evidence_retryable():
    source_id = None
    evidence_id = None
    try:
        async with async_session_maker() as session:
            source = Source(
                name=f"Deferred extraction {uuid4().hex}",
                source_type="web_research",
            )
            session.add(source)
            await session.flush()
            source_id = source.id
            evidence = RawEvidence(
                source_id=source.id,
                source_item_type="web_research",
                title="Retryable evidence",
                raw_content_ref="unused/test.txt",
                metadata_json={"subject_name": "Example Co."},
            )
            session.add(evidence)
            await session.commit()
            evidence_id = evidence.id

            worker = ExtractionWorker(session)
            worker.storage.get_object = AsyncMock(return_value=b"Evidence body")
            worker._extract_structured_data = AsyncMock(
                side_effect=RuntimeError("provider unavailable")
            )

            first = await worker.process_evidence(evidence.id)
            second = await worker.process_evidence(evidence.id)
            await session.refresh(evidence)
            source_item = (
                await session.execute(
                    select(SourceItem).where(SourceItem.raw_evidence_id == evidence.id)
                )
            ).scalar_one()

            assert first["deferred"] is True
            assert second["deferred"] is True
            assert worker._extract_structured_data.await_count == 2
            assert evidence.is_processed is False
            assert source_item.processing_status == "extraction_deferred"
            assert evidence.metadata_json["knowledge_promotion_status"] == "deferred"
    finally:
        if source_id is not None and evidence_id is not None:
            async with async_session_maker() as cleanup:
                await cleanup.execute(
                    delete(Edge).where(
                        Edge.source_type == "raw_evidence",
                        Edge.source_id == evidence_id,
                    )
                )
                await cleanup.execute(
                    delete(SourceItem).where(SourceItem.raw_evidence_id == evidence_id)
                )
                await cleanup.execute(
                    delete(RawEvidence).where(RawEvidence.id == evidence_id)
                )
                await cleanup.execute(delete(Source).where(Source.id == source_id))
                await cleanup.commit()
