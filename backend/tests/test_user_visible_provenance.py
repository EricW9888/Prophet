from uuid import uuid4

import pytest

from investos.api.routes.timeline import _source_context
from investos.db import async_session_maker
from investos.models.evidence import RawEvidence, SourceItem
from investos.models.graph import Edge
from investos.models.knowledge import Event, Fact
from investos.models.reasoning import EvidencePacket, ReasoningRun
from investos.models.source import Source
from investos.services.reasoning_trace import ReasoningTraceService
from investos.services.retrieval import RetrievalService

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_timeline_and_reasoning_trace_expose_attributable_source_records():
    async with async_session_maker() as session:
        source = Source(
            name=f"Example research {uuid4().hex}",
            source_type="web_research",
            url="https://research.example.test/",
        )
        session.add(source)
        await session.flush()
        evidence = RawEvidence(
            source_id=source.id,
            source_item_type="web_research",
            title="Example memory-cycle report",
            url="https://research.example.test/reports/memory-cycle",
            author="Example Analyst",
            metadata_json={"trigger": "research_loop", "query": "memory cycle"},
        )
        session.add(evidence)
        await session.flush()
        source_item = SourceItem(
            raw_evidence_id=evidence.id,
            source_id=source.id,
            extracted_text="Synthetic source text.",
            summary="Synthetic source summary.",
            processing_status="processed",
        )
        session.add(source_item)
        await session.flush()
        fact = Fact(
            statement="Example memory pricing improved during the quarter.",
            fact_type="financial",
            confidence=0.9,
            source_item_id=source_item.id,
            tier="hard_fact",
            importance="high",
            directness="primary",
            novelty="confirming",
            contradiction_role="neutral",
            promotion_eligible=True,
            target_horizon="strategic",
        )
        event = Event(
            title="Example pricing update",
            description="Example memory pricing changed.",
            event_type="guidance",
        )
        session.add_all([fact, event])
        await session.flush()
        session.add(
            Edge(
                source_type="event",
                source_id=event.id,
                target_type="source_item",
                target_id=source_item.id,
                relationship_type="extracted_from",
                confidence=1.0,
            )
        )
        packet = EvidencePacket(
            query_text="What changed?",
            direct_evidence_ids=[fact.id],
            connected_evidence_ids=[event.id],
            contradiction_evidence_ids=[fact.id],
        )
        session.add(packet)
        await session.flush()
        run = ReasoningRun(
            evidence_packet_id=packet.id,
            run_type="analysis",
            model_used="test_provider",
        )
        session.add(run)
        await session.flush()

        subject_name, timeline_sources = await _source_context(
            session,
            node_type="fact",
            node_id=fact.id,
            source_item_id=source_item.id,
        )
        trace = await ReasoningTraceService(session).get_run_trace(run.id)

        assert subject_name is None
        assert len(timeline_sources) == 1
        assert timeline_sources[0].raw_evidence_id == evidence.id
        assert timeline_sources[0].source_item_id == source_item.id
        assert timeline_sources[0].url == evidence.url
        assert timeline_sources[0].url_kind == "evidence_item"
        assert timeline_sources[0].origin_label == "Autonomous research"

        assert trace is not None and trace.evidence_packet is not None
        assert len(trace.evidence_packet.sources) == 1
        reasoning_source = trace.evidence_packet.sources[0]
        assert reasoning_source.raw_evidence_id == evidence.id
        assert reasoning_source.evidence_roles == [
            "direct",
            "connected",
            "contradiction",
        ]
        assert set(reasoning_source.knowledge_node_ids) == {fact.id, event.id}
        assert reasoning_source.url == evidence.url

        loaded_event = await RetrievalService(session)._load_nodes([event.id])
        assert loaded_event[0]["source"]["url"] == evidence.url
        assert loaded_event[0]["sources"][0]["source_item_id"] == str(source_item.id)

        await session.rollback()


async def test_source_home_is_labeled_as_fallback_when_item_url_is_missing():
    async with async_session_maker() as session:
        source = Source(
            name=f"Example source home {uuid4().hex}",
            source_type="manual",
            url="https://notes.example.test/",
        )
        session.add(source)
        await session.flush()
        evidence = RawEvidence(
            source_id=source.id,
            source_item_type="manual_note",
            title="Private research note",
            metadata_json={"origin": "source_workspace"},
        )
        session.add(evidence)
        await session.flush()
        source_item = SourceItem(
            raw_evidence_id=evidence.id,
            source_id=source.id,
            extracted_text="Synthetic note.",
            processing_status="processed",
        )
        session.add(source_item)
        await session.flush()
        fact = Fact(
            statement="A synthetic manual observation.",
            fact_type="observation",
            confidence=0.7,
            source_item_id=source_item.id,
            tier="credible_interpretation",
            importance="medium",
            directness="primary",
            novelty="confirming",
            contradiction_role="neutral",
            promotion_eligible=False,
            target_horizon="strategic",
        )
        session.add(fact)
        await session.flush()

        _subject_name, sources = await _source_context(
            session,
            node_type="fact",
            node_id=fact.id,
            source_item_id=source_item.id,
        )

        assert len(sources) == 1
        assert sources[0].url == source.url
        assert sources[0].url_kind == "source_home"
        assert sources[0].origin_label == "Manual note"

        await session.rollback()
