from datetime import UTC, datetime, timedelta
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
from investos.models.graph import Edge, GraphNodeLayout
from investos.models.knowledge import Fact
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
