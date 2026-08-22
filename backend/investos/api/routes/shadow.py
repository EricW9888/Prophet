from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from investos.db import get_session
from investos.schemas.shadow import (
    ShadowExperimentCreate,
    ShadowExperimentResponse,
    ShadowOrderCreate,
)
from investos.services.shadow import ShadowService, execute_shadow_experiment

router = APIRouter(prefix="/shadow", tags=["shadow"])


@router.get("/experiments", response_model=list[ShadowExperimentResponse])
async def list_shadow_experiments(session: AsyncSession = Depends(get_session)):
    service = ShadowService(session)
    experiments = await service.list_experiments()
    serialized = [
        await service.serialize_experiment(item, include_details=False)
        for item in experiments
    ]
    return serialized


@router.get("/experiments/{experiment_id}", response_model=ShadowExperimentResponse)
async def get_shadow_experiment(
    experiment_id: UUID,
    session: AsyncSession = Depends(get_session),
):
    service = ShadowService(session)
    experiment = await service.get_experiment(experiment_id)
    if experiment is None:
        raise HTTPException(status_code=404, detail="Shadow experiment not found")
    return await service.serialize_experiment(experiment)


@router.post("/experiments", response_model=ShadowExperimentResponse)
async def create_shadow_experiment(
    payload: ShadowExperimentCreate,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
):
    service = ShadowService(session)
    try:
        experiment = await service.create_experiment(payload)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    if payload.auto_run:
        background_tasks.add_task(execute_shadow_experiment, experiment.id)
    return await service.serialize_experiment(experiment)


@router.post(
    "/experiments/{experiment_id}/run", response_model=ShadowExperimentResponse
)
async def run_shadow_experiment(
    experiment_id: UUID,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
):
    service = ShadowService(session)
    try:
        experiment = await service.queue_experiment_run(experiment_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    background_tasks.add_task(execute_shadow_experiment, experiment.id)
    return await service.serialize_experiment(experiment)


@router.post(
    "/experiments/{experiment_id}/orders",
    response_model=ShadowExperimentResponse,
)
async def submit_shadow_order(
    experiment_id: UUID,
    payload: ShadowOrderCreate,
    session: AsyncSession = Depends(get_session),
):
    service = ShadowService(session)
    try:
        experiment = await service.submit_manual_paper_order(experiment_id, payload)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return await service.serialize_experiment(experiment)


@router.post(
    "/experiments/{experiment_id}/orders/{order_id}/cancel",
    response_model=ShadowExperimentResponse,
)
async def cancel_shadow_order(
    experiment_id: UUID,
    order_id: UUID,
    session: AsyncSession = Depends(get_session),
):
    service = ShadowService(session)
    try:
        experiment = await service.cancel_paper_order(experiment_id, order_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return await service.serialize_experiment(experiment)
