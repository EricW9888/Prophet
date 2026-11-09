from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from investos.api.routes.graph import router
from investos.db import async_session_maker, get_session
from investos.models.entity import Entity, Security
from investos.models.graph import Edge, GraphNodeLayout
from investos.models.portfolio import Position
from investos.models.profile import Profile
from investos.services.graph import SYNTHETIC_PORTFOLIO_ID


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
            assert all(
                item["source_key"] in keys and item["target_key"] in keys
                for item in expanded["edges"]
            )
            assert str(edge.id) not in {item["id"] for item in expanded["edges"]}
            assert (
                await client.get("/graph/workspace?scope=unknown")
            ).status_code == 422
