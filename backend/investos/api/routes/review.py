from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from investos.db import get_session
from investos.schemas.review import ReviewQueueItemResponse
from investos.services.review import ReviewService

router = APIRouter(prefix="/review", tags=["review"])


@router.get("/queue", response_model=list[ReviewQueueItemResponse])
async def list_review_queue(session: AsyncSession = Depends(get_session)):
    return await ReviewService(session).list_queue()


@router.post("/queue/refresh", response_model=list[ReviewQueueItemResponse])
async def refresh_review_queue(session: AsyncSession = Depends(get_session)):
    return await ReviewService(session).refresh_queue()
