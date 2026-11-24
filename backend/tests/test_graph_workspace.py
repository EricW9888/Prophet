from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import event

from investos.api.routes.graph import router
from investos.db import async_session_maker, engine, get_session
from investos.models.conclusion import ConclusionState
from investos.models.coverage import CoverageMap
from investos.models.entity import Entity, Security
from investos.models.evidence import RawEvidence, SourceItem
from investos.models.fundamental import FundamentalMetric
from investos.models.graph import Edge, GraphNodeLayout
from investos.models.knowledge import Event, Fact
from investos.models.portfolio import Position
from investos.models.profile import Profile
from investos.models.reasoning import ReasoningRun
from investos.models.source import Source
from investos.services.graph import (
    EXPLICIT_NODE_EDGE_LIMIT,
    SYNTHETIC_PORTFOLIO_ID,
    GraphService,
    _NeighborhoodReads,
)
from tests.test_investor_workflows import workflow_session


async def test_inspector_distinguishes_holdings_profile_and_peer_context(
    workflow_session,
):
    session = workflow_session
    issuer = Entity(name="Test infrastructure issuer", entity_type="company")
    peer = Entity(name="Test component issuer", entity_type="company")
    session.add_all([issuer, peer])
    await session.flush()
    security = Security(
        entity_id=issuer.id,
        ticker=f"C{uuid4().hex[:8]}",
        asset_class="equity",
        instrument_type="common_stock",
    )
    summary = "The filing confirms registration and lists the annual report sections."
    basis = "stored knowledge overlaps on infrastructure"
    peer_edge = Edge(
        source_type="entity",
        source_id=issuer.id,
        target_type="entity",
        target_id=peer.id,
        relationship_type="contextualizes",
        confidence=0.42,
        reasoning=basis,
        properties_json={"origin": "portfolio_peer_context"},
    )
    session.add_all(
        [
            security,
            peer_edge,
            Profile(
                subject_type="entity",
                subject_id=issuer.id,
                executive_summary=summary,
                key_drivers="This filing includes a consolidated balance sheet.",
            ),
        ]
    )
    await session.flush()
    session.add(
        Position(
            security_id=security.id,
            direction="long",
            list_type="holding",
            quantity=1,
        )
    )
    source = Source(name="Test issuer report", source_type="web_research")
    session.add(source)
    await session.flush()
    raw = RawEvidence(source_id=source.id, source_item_type="web_research")
    session.add(raw)
    await session.flush()
    item = SourceItem(raw_evidence_id=raw.id, source_id=source.id)
    session.add(item)
    await session.flush()
    facts = [
        Fact(
            statement=statement,
            source_item_id=item.id,
            fact_type="business_model",
            confidence=1,
            tier="hard_fact",
            importance="high",
            directness="primary",
            novelty="incremental",
            contradiction_role="neutral",
            is_verified=verified,
            is_deprecated=deprecated,
        )
        for statement, verified, deprecated in [
            ("The issuer leases compute capacity.", False, False),
            ("A deprecated account of the issuer's business.", True, True),
            ("The peer sells components.", True, False),
        ]
    ]
    session.add_all(facts)
    await session.flush()
    session.add_all(
        [
            Edge(
                source_type="fact",
                source_id=fact.id,
                target_type="entity",
                target_id=target.id,
                relationship_type="supports",
            )
            for fact, target in zip(facts, [issuer, issuer, peer], strict=True)
        ]
    )
    await session.flush()
    service = GraphService(session)
    detail = await service.get_node_detail(node_type="entity", node_id=issuer.id)
    assert detail is not None
    assert (
        detail.relevance_reasoning
        == f"Your current holdings include {security.ticker}."
    )
    assert detail.properties["linked_holdings"] == [security.ticker]
    assert "linked_companies" not in detail.properties
    assert detail.properties["profile_summary"] == summary
    assert detail.properties["portfolio_mechanism_status"] == "not_established"
    assert "portfolio_mechanism" not in detail.properties
    assert len(detail.business_evidence) == 1
    evidence = detail.business_evidence[0]
    assert evidence.fact_id == facts[0].id
    assert evidence.statement == facts[0].statement
    assert evidence.is_verified is False
    assert evidence.citation.raw_evidence_id == raw.id

    result = await service.get_neighborhood(
        node_type="entity", node_id=issuer.id, depth=1, limit=20
    )
    edge = next(edge for edge in result.edges if edge.id == peer_edge.id)
    assert edge.portfolio_peer_basis == basis
    assert edge.supporting_claim_id is None


@pytest.mark.parametrize(
    ("reaches_holding", "attached_to_root"),
    [(False, False), (True, False), (False, True), (True, True)],
)
async def test_context_route_to_portfolio_root_does_not_imply_holding_exposure(
    workflow_session, reaches_holding, attached_to_root
):
    session = workflow_session
    subject = Entity(name=f"Synthetic research subject {uuid4()}", entity_type="person")
    issuer = Entity(name=f"Synthetic issuer {uuid4()}", entity_type="company")
    context = Event(
        title="Source context",
        description="Reported context to investigate",
        event_type="market_context",
    )
    session.add_all([subject, issuer, context])
    await session.flush()
    security = Security(
        entity_id=issuer.id,
        ticker=f"C{uuid4().hex[:8]}",
        asset_class="equity",
        instrument_type="common_stock",
    )
    session.add(security)
    await session.flush()
    position = Position(
        security_id=security.id, direction="long", list_type="holding", quantity=1
    )
    session.add(position)
    await session.flush()
    session.add(
        Edge(
            source_type="event",
            source_id=context.id,
            target_type="entity",
            target_id=subject.id,
            relationship_type="context_for",
        )
    )
    if reaches_holding:
        session.add(
            Edge(
                source_type="position",
                source_id=position.id,
                target_type="event",
                target_id=context.id,
                relationship_type="supports",
            )
        )
    else:
        session.add(
            Edge(
                source_type="event",
                source_id=context.id,
                target_type="portfolio",
                target_id=SYNTHETIC_PORTFOLIO_ID,
                relationship_type="context_for",
            )
        )
    if attached_to_root:
        session.add_all(
            [
                Edge(
                    source_type="entity",
                    source_id=subject.id,
                    target_type="portfolio",
                    target_id=SYNTHETIC_PORTFOLIO_ID,
                    relationship_type="context_for",
                ),
                Edge(
                    source_type="portfolio",
                    source_id=SYNTHETIC_PORTFOLIO_ID,
                    target_type="position",
                    target_id=position.id,
                    relationship_type="holds",
                ),
            ]
        )
    await session.flush()

    async def use_test_session():
        yield session

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_session] = use_test_session
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(f"/graph/nodes/entity/{subject.id}")
        response.raise_for_status()
    detail = response.json()
    properties = detail["properties"]
    assert properties.get("direct_portfolio_link", False) is attached_to_root
    if not reaches_holding:
        assert properties["portfolio_significance"] == "portfolio-level context"
        assert properties["portfolio_linkage_status"] == "unresolved"
        assert "Holding linkage remains unresolved" in detail["relevance_reasoning"]
        assert not properties.get("linked_holdings")
        assert not properties.get("linked_companies")
        assert "affected_holdings" not in properties
    else:
        assert properties["portfolio_significance"] == "connected portfolio context"
        assert properties["linked_holdings"] == [security.ticker]
        assert "Source context" in properties["why_in_graph"]
        assert "indirect" in detail["relevance_reasoning"]
        assert (
            f"{security.ticker} → supports → Source context"
            in properties["nearest_connections"]
        )
    assert await session.get(Entity, subject.id) is subject
    await session.rollback()


@pytest.mark.parametrize("target_type", ["portfolio", "position"])
async def test_inspector_api_preserves_hypothesis_status_and_exact_links(
    workflow_session, target_type
):
    session = workflow_session
    proposal = (
        "Proposed mechanism: A proposed restriction could delay shipments. "
        "Next test: Check enactment and actual shipments."
    )
    actor = Entity(
        name=f"Synthetic policy actor {uuid4()}",
        entity_type="person",
        description=proposal,
    )
    issuer = Entity(name=f"Synthetic issuer {uuid4()}", entity_type="company")
    session.add_all([actor, issuer])
    await session.flush()
    ticker = f"B{uuid4().hex[:8]}"
    security = Security(
        entity_id=issuer.id,
        ticker=ticker,
        asset_class="equity",
        instrument_type="common_stock",
    )
    session.add(security)
    await session.flush()
    position = Position(
        security_id=security.id,
        direction="long",
        list_type="holding",
        quantity=1,
        market_value=1000000,
    )
    session.add(position)
    await session.flush()
    session.add(
        Edge(
            source_type="entity",
            source_id=actor.id,
            target_type=target_type,
            target_id=(
                position.id if target_type == "position" else SYNTHETIC_PORTFOLIO_ID
            ),
            relationship_type="contextualizes",
        )
    )
    await session.flush()

    async def use_test_session():
        yield session

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_session] = use_test_session
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(f"/graph/nodes/entity/{actor.id}")
        response.raise_for_status()
    detail = response.json()
    assert detail["properties"]["portfolio_mechanism_status"] == "unverified_hypothesis"
    assert "unverified" in detail["relevance_reasoning"].lower()
    assert "could delay shipments" in detail["relevance_reasoning"]
    assert "This matters through" not in detail["relevance_reasoning"]
    assert "affected_holdings" not in detail["properties"]
    if target_type == "portfolio":
        assert ticker not in detail["relevance_reasoning"]
        assert not detail["properties"].get("linked_holdings")
    else:
        assert detail["properties"]["linked_holdings"] == [ticker]
    await session.refresh(actor)
    assert actor.description == proposal


@pytest.mark.asyncio(loop_scope="session")
async def test_longer_portfolio_path_remains_unresolved_by_bounded_detail_check():
    async with async_session_maker() as session:
        nodes = [
            Entity(name=f"Synthetic linkage {uuid4()}", entity_type="company")
            for _ in range(4)
        ]
        session.add_all(nodes)
        await session.flush()
        root, intermediary, second, held = nodes
        security = Security(
            entity_id=held.id,
            ticker=f"G{uuid4().hex[:8]}",
            asset_class="equity",
            instrument_type="common_stock",
        )
        session.add(security)
        await session.flush()
        session.add(
            Position(
                security_id=security.id,
                direction="long",
                list_type="holding",
                quantity=1,
            )
        )
        session.add_all(
            Edge(
                source_type="entity",
                source_id=source.id,
                target_type="entity",
                target_id=target.id,
                relationship_type="related_to",
            )
            for source, target in zip(nodes, nodes[1:])
        )
        await session.flush()
        graph = GraphService(session)
        expanded = await graph.get_neighborhood(
            node_type="entity", node_id=root.id, depth=3, limit=8
        )
        assert f"entity:{held.id}" in {node.key for node in expanded.nodes}
        detail = await graph.get_node_detail(node_type="entity", node_id=root.id)
        assert detail.properties["portfolio_linkage_status"] == "unresolved"
        assert detail.properties["portfolio_route_depth"] == 2
        assert "two-hop check" in detail.relevance_reasoning
        nearer = await graph.get_node_detail(
            node_type="entity", node_id=intermediary.id
        )
        assert (
            nearer.properties["portfolio_significance"] == "connected portfolio context"
        )
        assert "portfolio_linkage_status" not in nearer.properties
        await session.rollback()


@pytest.mark.asyncio(loop_scope="session")
async def test_bounded_neighborhood_shares_slots_across_frontier_branches():
    async with async_session_maker() as session:
        nodes = [
            Entity(name=f"Synthetic branch {uuid4()}", entity_type="company")
            for _ in range(24)
        ]
        session.add_all(nodes)
        await session.flush()
        root, crowded, quiet, counterparty, *leaves = nodes
        now = datetime.now(UTC)

        def link(source, target, created_at):
            return Edge(
                source_type="entity",
                source_id=source.id,
                target_type="entity",
                target_id=target.id,
                relationship_type="related_to",
                created_at=created_at,
            )

        session.add_all(
            [
                link(root, crowded, now + timedelta(seconds=1)),
                link(root, quiet, now),
                link(quiet, counterparty, now + timedelta(seconds=2)),
            ]
            + [link(crowded, leaf, now + timedelta(seconds=2)) for leaf in leaves]
        )
        await session.flush()
        view = await GraphService(session).get_neighborhood(
            node_type="entity", node_id=root.id, depth=2, limit=6
        )
        assert len(view.nodes) == 6
        keys = {node.key for node in view.nodes}
        assert f"entity:{counterparty.id}" in keys
        assert any(f"entity:{leaf.id}" in keys for leaf in leaves)
        assert all(
            edge.source_key in keys and edge.target_key in keys for edge in view.edges
        )
        await session.rollback()


@pytest.mark.asyncio(loop_scope="session")
async def test_graph_batches_node_provenance_and_autonomous_profiles():
    async with async_session_maker() as session:
        source = Source(name="Synthetic graph provenance", source_type="web_research")
        session.add(source)
        await session.flush()
        raw = RawEvidence(source_id=source.id, source_item_type="web_research")
        session.add(raw)
        await session.flush()
        item = SourceItem(raw_evidence_id=raw.id, source_id=source.id)
        session.add(item)
        entities = [
            Entity(name=f"Synthetic batch {i}", entity_type="company")
            for i in range(35)
        ]
        session.add_all(entities)
        await session.flush()
        facts = [
            Fact(
                statement=f"Synthetic fact {i}",
                source_item_id=item.id,
                fact_type="test",
                confidence=1,
                tier="hard_fact",
                importance="high",
                directness="primary",
                novelty="incremental",
                contradiction_role="neutral",
                promotion_eligible=True,
            )
            for i in range(35)
        ]
        session.add_all(facts)
        session.add_all(
            [
                Profile(
                    subject_type="entity",
                    subject_id=node.id,
                    is_autonomous=index % 2 == 0,
                )
                for index, node in enumerate(entities)
            ]
        )
        await session.flush()
        query_count = 0

        def count_query(*_args):
            nonlocal query_count
            query_count += 1

        event.listen(engine.sync_engine, "before_cursor_execute", count_query)
        try:
            reads = _NeighborhoodReads(GraphService(session))
            await reads.prefetch_nodes(
                [f"entity:{node.id}" for node in entities]
                + [f"fact:{node.id}" for node in facts]
            )
            for index, entity in enumerate(entities):
                assert await reads.node("entity", entity.id) is entity
                assert await reads.is_autonomous("entity", entity) is (index % 2 == 0)
            assert await reads.layer("source_item", item) == "source"
            assert reads.nodes[f"raw_evidence:{raw.id}"] is raw
            assert reads.nodes[f"source:{source.id}"] is source
            assert query_count <= 9
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", count_query)
        await session.rollback()


@pytest.mark.asyncio(loop_scope="session")
async def test_batched_explicit_edges_keep_each_nodes_limit_and_direction():
    first, second, empty = [("entity", uuid4()) for _ in range(3)]
    started = datetime.now(UTC)
    async with async_session_maker() as session:
        session.add_all(
            [
                Edge(
                    source_type=first[0],
                    source_id=first[1],
                    target_type="entity",
                    target_id=uuid4(),
                    relationship_type="synthetic",
                    created_at=started + timedelta(seconds=index),
                )
                for index in range(EXPLICIT_NODE_EDGE_LIMIT + 1)
            ]
        )
        incoming = Edge(
            source_type="entity",
            source_id=uuid4(),
            target_type=second[0],
            target_id=second[1],
            relationship_type="synthetic",
            created_at=started,
        )
        session.add(incoming)
        await session.flush()
        graph = GraphService(session)
        grouped = await graph._explicit_edges_for_nodes([first, second, empty])

        expected = await graph._edges_touching_node(
            node_type=first[0], node_id=first[1]
        )
        assert len(grouped[first]) == EXPLICIT_NODE_EDGE_LIMIT
        assert [edge.id for edge in grouped[first]] == [edge.id for edge in expected]
        assert grouped[second] == [incoming]
        assert grouped[empty] == []


@pytest.mark.asyncio(loop_scope="session")
async def test_workspace_web_preserves_scope_edges_and_retained_layout():
    async with async_session_maker() as session:
        entity = Entity(name="Synthetic held issuer", entity_type="company")
        outside = Entity(name="Synthetic untracked issuer", entity_type="company")
        session.add_all([entity, outside])
        await session.flush()
        security = Security(
            entity_id=entity.id,
            ticker=f"G{uuid4().hex[:8]}",
            asset_class="equity",
            instrument_type="common_stock",
        )
        session.add(security)
        await session.flush()
        position = Position(
            security_id=security.id, direction="long", list_type="holding", quantity=1
        )
        session.add_all(
            [
                position,
                Profile(subject_type="portfolio", subject_id=SYNTHETIC_PORTFOLIO_ID),
                Profile(subject_type="entity", subject_id=entity.id),
                Profile(subject_type="entity", subject_id=outside.id),
            ]
        )
        # The portfolio root's built-in profile links reach untracked names;
        # the expanded scope must also retain their independent neighborhoods.
        missing = uuid4()
        edge = Edge(
            source_type="entity",
            source_id=outside.id,
            target_type="entity",
            target_id=missing,
            relationship_type="synthetic_missing_target",
        )
        session.add(edge)
        run = ReasoningRun(run_type="test", model_used="synthetic")
        session.add(run)
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
            subject_type="entity",
            subject_id=entity.id,
            evidence_class_coverage_json={},
        )
        session.add_all([conclusion, coverage])
        await session.flush()
        position_key = f"position:{position.id}"
        session.add(GraphNodeLayout(node_key=position_key, x=180.0, y=260.0))
        await session.flush()

        async def use_test_session():
            yield session

        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_session] = use_test_session
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            result = await client.get("/graph/workspace")
            result.raise_for_status()
            portfolio = result.json()
            nodes = {node["key"]: node for node in portfolio["nodes"]}
            assert portfolio["root_key"] == f"portfolio:{SYNTHETIC_PORTFOLIO_ID}"
            assert [node["key"] for node in nodes.values() if node["is_root"]] == [
                portfolio["root_key"]
            ]
            assert (nodes[position_key]["x"], nodes[position_key]["y"]) == (180, 260)
            assert any(
                item["source_key"] == portfolio["root_key"]
                and item["target_key"] == position_key
                and item["relationship_type"] == "current_holding"
                for item in portfolio["edges"]
            )

            result = await client.get("/graph/workspace?scope=expanded")
            result.raise_for_status()
            expanded = result.json()
            keys = {node["key"] for node in expanded["nodes"]}
            assert f"entity:{outside.id}" in keys
            entity_links = {
                (item["relationship_type"], item["target_key"])
                for item in expanded["edges"]
                if item["source_key"] == f"entity:{entity.id}"
            }
            assert ("has_accepted_state", f"conclusion:{conclusion.id}") in entity_links
            assert f"coverage_map:{coverage.id}" not in keys
            assert all(
                item["source_key"] in keys and item["target_key"] in keys
                for item in expanded["edges"]
            )
            assert str(edge.id) not in {item["id"] for item in expanded["edges"]}
            result = await client.get(
                "/graph/workspace?scope=expanded&include_system=true"
            )
            result.raise_for_status()
            assert any(
                item["source_key"] == f"entity:{entity.id}"
                and item["target_key"] == f"coverage_map:{coverage.id}"
                and item["relationship_type"] == "monitored_by"
                for item in result.json()["edges"]
            )
            assert (
                await client.get("/graph/workspace?scope=unknown")
            ).status_code == 422


def test_metric_summary_prefers_source_text_without_duplicating_numeric_representation():
    metric = FundamentalMetric(
        value_text="55,555,555",
        numeric_value=Decimal("55555555"),
        unit="shares",
        period_label="offering",
    )
    body = GraphService(None)._fundamental_metric_body(metric)
    assert "Value: 55,555,555 shares" in body
    assert "e+07" not in body
    assert body.count("55,555,555") == 1
    metric.value_text = None
    metric.numeric_value = Decimal("1234567.25")
    assert "Value: 1,234,567.25 shares" in GraphService(None)._fundamental_metric_body(
        metric
    )
    metric.numeric_value = Decimal("0")
    assert "Value: 0 shares" in GraphService(None)._fundamental_metric_body(metric)
