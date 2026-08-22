from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from investos.db import get_session
from investos.schemas.risk import PerformanceAttributionResponse, RiskSummaryResponse
from investos.services.risk import RiskService

router = APIRouter(prefix="/risk", tags=["risk"])


@router.get("/summary", response_model=RiskSummaryResponse)
async def get_risk_summary(
    refresh: bool = Query(default=False),
    session: AsyncSession = Depends(get_session),
):
    try:
        return await RiskService(session).get_summary(refresh=refresh)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/performance-attribution", response_model=PerformanceAttributionResponse)
async def get_performance_attribution(
    window_days: int = Query(default=21, ge=1, le=1825),
    session: AsyncSession = Depends(get_session),
):
    try:
        return await RiskService(session).get_performance_attribution(
            window_days=window_days
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))
