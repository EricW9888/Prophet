"""Batched Knowledge reads return exactly what the one-node-at-a-time reads return."""

from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

from investos.models.conclusion import ConclusionState
from investos.models.coverage import CoverageMap, UnresolvedQuestion
from investos.models.entity import Entity, Security
from investos.models.evidence import RawEvidence, SourceItem
from investos.models.fundamental import FundamentalMetric
from investos.models.graph import Edge
from investos.models.knowledge import Claim, Event, Fact
from investos.models.market_setup import MarketSetupSignal
from investos.models.portfolio import Position
from investos.models.reasoning import ReasoningRun
from investos.models.review import ReviewQueueItem
from investos.models.source import Source
from investos.services.graph import GraphService, _NeighborhoodReads
from tests.test_investor_workflows import workflow_session  # noqa: F401


async def _entity_with_more_rows_than_each_limit(session):
    now = datetime.now(UTC)
    entity = Entity(name=f"Batch issuer {uuid4().hex[:6]}", entity_type="company")
    session.add(entity)
    await session.flush()
    security = Security(
        entity_id=entity.id,
        ticker=f"B{uuid4().hex[:7]}",
        asset_class="equity",
        instrument_type="common_stock",
    )
    run = ReasoningRun(run_type="test", model_used="synthetic")
    session.add_all([security, run])
    await session.flush()
    conclusion = ConclusionState(
        subject_type="entity",
        subject_id=entity.id,
        current_thesis_summary="Synthetic thesis",
        current_stance="neutral",
        confidence_band="low",
        reasoning_run_id=run.id,
    )
    coverage = CoverageMap(
        subject_type="entity", subject_id=entity.id, evidence_class_coverage_json={}
    )
    session.add_all([conclusion, coverage])
    await session.flush()

    for index in range(9):
        session.add(
            UnresolvedQuestion(
                coverage_map_id=coverage.id,
                question_text=f"question {index}",
                urgency=index % 4,
                status="open" if index % 3 else "investigating",
                created_at=now - timedelta(minutes=index),
            )
        )
    # Reviews reach the entity directly and through its conclusion.
    for index in range(7):
        session.add(
            ReviewQueueItem(
                item_type="entity",
                item_id=entity.id,
                priority_score=50 + index,
                trigger_reason=f"entity review {index}",
            )
        )
        session.add(
            ReviewQueueItem(
                item_type="conclusion",
                item_id=conclusion.id,
                priority_score=53.5 + index,
                trigger_reason=f"conclusion review {index}",
            )
        )
    for index, list_type in enumerate(
        ["holding", "watchlist", "considering", "watchlist"] * 2
    ):
        session.add(
            Position(
                security_id=security.id,
                list_type=list_type,
                direction="long",
                quantity=1,
                market_value=None if index == 5 else 100 * index,
            )
        )
    # Metrics and setups match through entity_id or through subject_id, and
    # some dates are missing (PostgreSQL ranks NULL first when descending).
    for index in range(7):
        common = dict(
            metric_name=f"metric_{index}",
            metric_family="synthetic",
            as_of=None if index == 3 else date(2026, 1, 1) + timedelta(days=index * 2),
            created_at=now - timedelta(minutes=index),
        )
        session.add(FundamentalMetric(entity_id=entity.id, **common))
        session.add(
            FundamentalMetric(
                subject_type="entity",
                subject_id=entity.id,
                **{
                    **common,
                    "metric_name": f"subject_metric_{index}",
                    "as_of": date(2026, 1, 2) + timedelta(days=index * 2),
                },
            )
        )
        session.add(
            MarketSetupSignal(
                ticker="SYN",
                signal_name=f"setup {index}",
                signal_family="synthetic",
                entity_id=entity.id if index % 2 else None,
                subject_type=None if index % 2 else "entity",
                subject_id=None if index % 2 else entity.id,
                public_time=None if index == 2 else now - timedelta(hours=index),
                created_at=now - timedelta(minutes=index),
            )
        )
    await session.flush()
    return entity


async def test_entity_connections_match_per_node_reads(workflow_session):
    session = workflow_session
    entity = await _entity_with_more_rows_than_each_limit(session)
    service = GraphService(session)

    one_at_a_time = await service._subject_operating_specs(
        subject_type="entity", subject_id=entity.id, reads=None
    )
    reads = _NeighborhoodReads(service)
    await reads.prefetch_nodes([f"entity:{entity.id}"])
    await reads.prefetch_synthetic([("entity", entity.id)])
    assert entity.id in reads.entity_rows
    batched = await service._subject_operating_specs(
        subject_type="entity", subject_id=entity.id, reads=reads
    )

    assert batched == one_at_a_time
    kinds = {spec[2] for spec in batched}
    assert {
        "watching_for",
        "needs_review",
        "has_fundamental_metric",
        "has_market_setup",
    } <= kinds
    assert any(
        spec[2].startswith(("currently_held", "tracked_as_")) for spec in batched
    )


async def test_shared_source_context_matches_per_node_reads(workflow_session):
    session = workflow_session
    now = datetime.now(UTC)
    source = Source(name=f"batch-{uuid4().hex[:6]}.example", source_type="news")
    session.add(source)
    await session.flush()
    items = []
    for index in range(2):
        raw = RawEvidence(
            source_id=source.id, source_item_type="news", title=f"Synthetic {index}"
        )
        session.add(raw)
        await session.flush()
        item = SourceItem(raw_evidence_id=raw.id, source_id=source.id)
        session.add(item)
        items.append(item)
    await session.flush()
    for item in items:
        for index in range(8):
            session.add(
                Claim(
                    statement=f"claim {index}",
                    claim_type="forecast",
                    confidence=0.5,
                    source_item_id=item.id,
                    tier="secondary",
                    importance="medium",
                    directness="direct",
                    novelty="new",
                    contradiction_role="none",
                    created_at=now - timedelta(minutes=index),
                )
            )
            session.add(
                Fact(
                    statement=f"fact {index}",
                    fact_type="synthetic",
                    confidence=0.5,
                    tier="secondary",
                    importance="medium",
                    directness="direct",
                    novelty="new",
                    contradiction_role="none",
                    source_item_id=item.id,
                    created_at=now - timedelta(minutes=index),
                )
            )
        for index in range(6):
            event = Event(title=f"event {index}", event_type="synthetic")
            session.add(event)
            await session.flush()
            session.add(
                Edge(
                    source_type="event",
                    source_id=event.id,
                    target_type="source_item",
                    target_id=item.id,
                    relationship_type="reported_in",
                    created_at=now - timedelta(minutes=index),
                )
            )
    await session.flush()
    service = GraphService(session)

    batched = await service._sibling_evidence_candidates_for(
        [item.id for item in items]
    )

    for item in items:
        assert batched[item.id] == await service._sibling_evidence_candidates(item.id)
        assert len(batched[item.id]) == 6 + 6 + 4
