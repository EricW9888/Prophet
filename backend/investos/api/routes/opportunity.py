from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from investos.db import get_session
from investos.schemas.opportunity import (
    OpportunityCandidateResponse,
    OpportunityCandidateReview,
    OpportunityDiscoveryRunResponse,
    OpportunityShadowTestRequest,
    OpportunityUniverseImportPreview,
    OpportunityUniverseImportRequest,
    OpportunityUniverseImportResult,
    OpportunityUniverseMemberCreate,
    OpportunityUniverseMemberResponse,
    OpportunityUniverseMemberUpdate,
)
from investos.services.opportunity import OpportunityDiscoveryService
from investos.services.shadow import execute_shadow_experiment

router = APIRouter(prefix="/opportunities", tags=["opportunities"])


@router.get(
    "/universe",
    response_model=list[OpportunityUniverseMemberResponse],
)
async def list_opportunity_universe(
    session: AsyncSession = Depends(get_session),
):
    return await OpportunityDiscoveryService(session).list_universe()


@router.get(
    "/universe/import-preview",
    response_model=OpportunityUniverseImportPreview,
)
async def preview_opportunity_universe_import(
    session: AsyncSession = Depends(get_session),
):
    return await OpportunityDiscoveryService(session).preview_universe_import()


@router.post(
    "/universe/import",
    response_model=OpportunityUniverseImportResult,
)
async def import_opportunity_universe(
    payload: OpportunityUniverseImportRequest,
    session: AsyncSession = Depends(get_session),
):
    try:
        return await OpportunityDiscoveryService(session).import_universe(
            payload.sources
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post(
    "/universe",
    response_model=OpportunityUniverseMemberResponse,
    status_code=status.HTTP_201_CREATED,
)
async def add_opportunity_universe_member(
    payload: OpportunityUniverseMemberCreate,
    session: AsyncSession = Depends(get_session),
):
    try:
        return await OpportunityDiscoveryService(session).upsert_universe_member(
            payload
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.patch(
    "/universe/{member_id}",
    response_model=OpportunityUniverseMemberResponse,
)
async def update_opportunity_universe_member(
    member_id: UUID,
    payload: OpportunityUniverseMemberUpdate,
    session: AsyncSession = Depends(get_session),
):
    try:
        return await OpportunityDiscoveryService(session).update_universe_member(
            member_id,
            payload,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.delete(
    "/universe/{member_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def remove_opportunity_universe_member(
    member_id: UUID,
    session: AsyncSession = Depends(get_session),
):
    try:
        await OpportunityDiscoveryService(session).remove_universe_member(member_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/runs", response_model=OpportunityDiscoveryRunResponse)
async def run_opportunity_discovery(
    session: AsyncSession = Depends(get_session),
):
    return await OpportunityDiscoveryService(session).run_discovery()


@router.get("/runs", response_model=list[OpportunityDiscoveryRunResponse])
async def list_opportunity_discovery_runs(
    limit: int = Query(default=20, ge=1, le=100),
    session: AsyncSession = Depends(get_session),
):
    return await OpportunityDiscoveryService(session).list_runs(limit=limit)


@router.get("/candidates", response_model=list[OpportunityCandidateResponse])
async def list_opportunity_candidates(
    candidate_status: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=100, ge=1, le=500),
    session: AsyncSession = Depends(get_session),
):
    try:
        return await OpportunityDiscoveryService(session).list_candidates(
            status=candidate_status,
            limit=limit,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post(
    "/candidates/{candidate_id}/review",
    response_model=OpportunityCandidateResponse,
)
async def review_opportunity_candidate(
    candidate_id: UUID,
    payload: OpportunityCandidateReview,
    session: AsyncSession = Depends(get_session),
):
    try:
        return await OpportunityDiscoveryService(session).review_candidate(
            candidate_id,
            payload,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post(
    "/candidates/{candidate_id}/shadow-test",
    response_model=OpportunityCandidateResponse,
)
async def shadow_test_opportunity_candidate(
    candidate_id: UUID,
    payload: OpportunityShadowTestRequest,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
):
    try:
        candidate, experiment_id = await OpportunityDiscoveryService(
            session
        ).shadow_test_candidate(candidate_id, payload)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    background_tasks.add_task(execute_shadow_experiment, experiment_id)
    return candidate
