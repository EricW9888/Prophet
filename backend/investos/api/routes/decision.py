from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from investos.db import get_session
from investos.schemas.decision import (
    DecisionJournalCreate,
    DecisionJournalResponse,
    DecisionReviewCreate,
    DecisionReviewResponse,
)
from investos.services.decision import DecisionService

router = APIRouter(prefix="/decisions", tags=["decisions"])


@router.get("/", response_model=list[DecisionJournalResponse])
async def list_decisions(session: AsyncSession = Depends(get_session)):
    return await DecisionService(session).list_decisions()


@router.post("/", response_model=DecisionJournalResponse)
async def create_decision(
    payload: DecisionJournalCreate,
    session: AsyncSession = Depends(get_session),
):
    return await DecisionService(session).create_decision(payload)


@router.post("/reviews", response_model=DecisionReviewResponse)
async def create_review(
    payload: DecisionReviewCreate,
    session: AsyncSession = Depends(get_session),
):
    try:
        return await DecisionService(session).create_review(payload)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
