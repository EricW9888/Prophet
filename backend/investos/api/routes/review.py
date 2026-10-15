from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from investos.db import get_session
from investos.schemas.review import ReviewQueueItemResponse, ReviewQueuePageResponse
from investos.services.review import ReviewService

router = APIRouter(prefix="/review", tags=["review"])


@router.get("/queue", response_model=list[ReviewQueueItemResponse])
async def list_review_queue(session: AsyncSession = Depends(get_session)):
    return await ReviewService(session).list_queue()


@router.get("/queue/page", response_model=ReviewQueuePageResponse)
async def review_queue_page(
    limit: int = Query(100, ge=1, le=100),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),
):
    return await ReviewService(session).queue_page(limit=limit, offset=offset)


@router.get("/queue/{item_id}", response_model=ReviewQueueItemResponse | None)
async def review_queue_item(
    item_id: UUID, session: AsyncSession = Depends(get_session)
):
    return await ReviewService(session).queued_item(item_id)


@router.post("/queue/refresh", response_model=list[ReviewQueueItemResponse])
async def refresh_review_queue(session: AsyncSession = Depends(get_session)):
    return await ReviewService(session).refresh_queue()
