from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from investos.db import get_session
from investos.models.evidence import RawEvidence, SourceItem
from investos.models.fundamental import FundamentalMetric
from investos.models.graph import Edge
from investos.models.knowledge import Claim, Event, Fact
from investos.models.market_setup import MarketSetupSignal
from investos.models.profile import Profile
from investos.models.source import Source
from investos.schemas.graph import (
    GraphLayoutSyncRequest,
    GraphNeighborhoodResponse,
    GraphNodeDetailResponse,
    GraphRelationResponse,
    GraphSearchResultResponse,
    GraphStatsResponse,
    SubjectAliasCreate,
    SubjectAliasResponse,
    SubjectAliasSubjectOption,
    SubjectAliasUpdate,
)
from investos.services.graph import GraphService
from investos.services.subject_alias import SubjectAliasService

router = APIRouter(prefix="/graph", tags=["graph"])


async def _count(session: AsyncSession, model, *filters) -> int:
    stmt = select(func.count()).select_from(model)
    for condition in filters:
        stmt = stmt.where(condition)
    return int((await session.execute(stmt)).scalar_one())


@router.get("/aliases", response_model=list[SubjectAliasResponse])
async def list_subject_aliases(
    limit: int = 100,
    session: AsyncSession = Depends(get_session),
):
    return await SubjectAliasService(session).list_aliases(limit=limit)


@router.post("/aliases", response_model=SubjectAliasResponse)
async def create_subject_alias(
    payload: SubjectAliasCreate,
    session: AsyncSession = Depends(get_session),
):
    try:
        return await SubjectAliasService(session).create_alias(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/alias-subjects", response_model=list[SubjectAliasSubjectOption])
async def list_subject_alias_options(
    query: str | None = None,
    limit: int = 100,
    session: AsyncSession = Depends(get_session),
):
    return await SubjectAliasService(session).list_subject_options(
        query=query, limit=limit
    )


@router.get("/stats", response_model=GraphStatsResponse)
async def get_graph_stats(
    session: AsyncSession = Depends(get_session),
):
    active_facts = await _count(session, Fact, Fact.is_deprecated.is_(False))
    active_claims = await _count(session, Claim, Claim.is_deprecated.is_(False))
    active_events = await _count(session, Event, Event.is_deprecated.is_(False))
    deprecated_facts = await _count(session, Fact, Fact.is_deprecated.is_(True))
    deprecated_claims = await _count(session, Claim, Claim.is_deprecated.is_(True))
    deprecated_events = await _count(session, Event, Event.is_deprecated.is_(True))
    active_knowledge_nodes = active_facts + active_claims + active_events
    deprecated_knowledge_nodes = (
        deprecated_facts + deprecated_claims + deprecated_events
    )
    return GraphStatsResponse(
        active_facts=active_facts,
        active_claims=active_claims,
        active_events=active_events,
        deprecated_facts=deprecated_facts,
        deprecated_claims=deprecated_claims,
        deprecated_events=deprecated_events,
        total_edges=await _count(session, Edge),
        profiles=await _count(session, Profile),
        sources=await _count(session, Source),
        raw_evidence=await _count(session, RawEvidence),
        source_items=await _count(session, SourceItem),
        fundamental_metrics=await _count(
            session,
            FundamentalMetric,
            FundamentalMetric.is_deprecated.is_(False),
        ),
        market_setup_signals=await _count(
            session,
            MarketSetupSignal,
            MarketSetupSignal.is_deprecated.is_(False),
        ),
        active_knowledge_nodes=active_knowledge_nodes,
        total_knowledge_nodes=active_knowledge_nodes + deprecated_knowledge_nodes,
    )


@router.get("/search", response_model=list[GraphSearchResultResponse])
async def search_graph_nodes(
    query: str,
    limit: int = 20,
    session: AsyncSession = Depends(get_session),
):
    return await GraphService(session).search_nodes(query=query, limit=limit)


@router.patch("/aliases/{alias_id}", response_model=SubjectAliasResponse)
async def update_subject_alias(
    alias_id: UUID,
    payload: SubjectAliasUpdate,
    session: AsyncSession = Depends(get_session),
):
    try:
        alias = await SubjectAliasService(session).update_alias(alias_id, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if alias is None:
        raise HTTPException(status_code=404, detail="subject_alias_not_found")
    return alias


@router.post("/aliases/{alias_id}/approve", response_model=SubjectAliasResponse)
async def approve_subject_alias(
    alias_id: UUID,
    session: AsyncSession = Depends(get_session),
):
    alias = await SubjectAliasService(session).approve_alias(alias_id)
    if alias is None:
        raise HTTPException(status_code=404, detail="subject_alias_not_found")
    return alias


@router.delete("/aliases/{alias_id}")
async def delete_subject_alias(
    alias_id: UUID,
    session: AsyncSession = Depends(get_session),
):
    deleted = await SubjectAliasService(session).delete_alias(alias_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="subject_alias_not_found")
    return {"ok": True}


@router.get("/nodes/{node_type}/{node_id}", response_model=GraphNodeDetailResponse)
async def get_graph_node_detail(
    node_type: str,
    node_id: str,
    session: AsyncSession = Depends(get_session),
):
    detail = await GraphService(session).get_node_detail(
        node_type=node_type, node_id=node_id
    )
    if detail is None:
        raise HTTPException(status_code=404, detail="graph_node_not_found")
    return detail


@router.get(
    "/neighborhood/{node_type}/{node_id}", response_model=GraphNeighborhoodResponse
)
async def get_graph_neighborhood(
    node_type: str,
    node_id: str,
    limit: int = 14,
    depth: int = 2,
    include_system: bool = False,
    session: AsyncSession = Depends(get_session),
):
    try:
        detail = await GraphService(session).get_neighborhood(
            node_type=node_type,
            node_id=node_id,
            depth=depth,
            limit=limit,
            include_system_nodes=include_system,
        )
        if detail is None:
            raise HTTPException(status_code=404, detail="graph_node_not_found")
        return detail
    except HTTPException:
        raise
    except Exception as e:
        import traceback

        print(f"ERROR in get_neighborhood for {node_type}:{node_id} -> {str(e)}")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/compare", response_model=GraphRelationResponse)
async def compare_graph_nodes(
    node_a_type: str,
    node_a_id: str,
    node_b_type: str,
    node_b_id: str,
    session: AsyncSession = Depends(get_session),
):
    detail = await GraphService(session).compare_nodes(
        node_a_type=node_a_type,
        node_a_id=node_a_id,
        node_b_type=node_b_type,
        node_b_id=node_b_id,
    )
    if detail is None:
        raise HTTPException(status_code=404, detail="graph_node_not_found")
    return detail


@router.post("/layout", response_model=dict)
async def sync_graph_layout(
    payload: GraphLayoutSyncRequest,
    session: AsyncSession = Depends(get_session),
):
    await GraphService(session).sync_layout(payload.layouts)
    return {"status": "ok", "saved_count": len(payload.layouts)}
