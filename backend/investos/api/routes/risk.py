from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from investos.db import get_session
from investos.schemas.portfolio_history import PortfolioHistoryResponse
from investos.schemas.portfolio_risk import (
    CapturedHoldingsValue,
    PortfolioRiskState,
    RiskWatchRequest,
)
from investos.schemas.risk import PerformanceAttributionResponse, RiskSummaryResponse
from investos.services.advisory_trial import AdvisoryTrialService
from investos.services.portfolio_history import PortfolioHistoryService
from investos.services.portfolio_risk import PortfolioRiskService
from investos.services.risk import RiskService

router = APIRouter(prefix="/risk", tags=["risk"])


@router.get("/portfolio/history", response_model=PortfolioHistoryResponse)
async def get_portfolio_history(
    window_days: int = Query(default=31, ge=1, le=366),
    refresh: bool = False,
    session: AsyncSession = Depends(get_session),
):
    return await PortfolioHistoryService(session).history(window_days, refresh=refresh)


@router.get("/portfolio/history/{receipt_id}")
async def get_portfolio_history_receipt(
    receipt_id: UUID, session: AsyncSession = Depends(get_session)
):
    from investos.models.quant import AttributionResult

    record = await session.get(AttributionResult, receipt_id)
    if (
        record is None
        or record.factor_contributions_json.get("kind") != "portfolio_history"
    ):
        raise HTTPException(status_code=404, detail="History calculation not found")
    return record.factor_contributions_json


@router.get("/portfolio", response_model=PortfolioRiskState)
async def get_portfolio_risk(session: AsyncSession = Depends(get_session)):
    return await PortfolioRiskService(session).state()


@router.post("/portfolio/refresh", response_model=PortfolioRiskState)
async def refresh_portfolio_risk(session: AsyncSession = Depends(get_session)):
    return await PortfolioRiskService(session).refresh()


@router.put("/portfolio/watch", response_model=PortfolioRiskState)
async def configure_portfolio_risk(
    payload: RiskWatchRequest, session: AsyncSession = Depends(get_session)
):
    return await PortfolioRiskService(session).configure(payload)


@router.get("/portfolio/packets/{assessment_id}")
async def get_portfolio_risk_packet(
    assessment_id: UUID, session: AsyncSession = Depends(get_session)
):
    packet = await PortfolioRiskService(session).packet(assessment_id)
    if packet is None:
        raise HTTPException(status_code=404, detail="Risk assessment not found")
    return packet


@router.get("/portfolio/value-history", response_model=list[CapturedHoldingsValue])
async def get_portfolio_value_history(
    basis: Literal["capture", "session"] = "capture",
    session: AsyncSession = Depends(get_session),
):
    return await PortfolioRiskService(session).captured_value_history(
        by_session=basis == "session"
    )


@router.get("/portfolio/advisory-trial")
async def get_advisory_trial(session: AsyncSession = Depends(get_session)):
    return await AdvisoryTrialService(session).state()


@router.post("/portfolio/advisory-trial")
async def start_advisory_trial(session: AsyncSession = Depends(get_session)):
    try:
        return await AdvisoryTrialService(session).start()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/portfolio/advisory-trial/{trial_id}/stop")
async def stop_advisory_trial(
    trial_id: UUID, session: AsyncSession = Depends(get_session)
):
    return await AdvisoryTrialService(session).stop(trial_id)


@router.get("/portfolio/advisory-trial/receipts/{observation_id}")
async def get_advisory_trial_receipt(
    observation_id: UUID, session: AsyncSession = Depends(get_session)
):
    receipt = await AdvisoryTrialService(session).receipt(observation_id)
    if receipt is None:
        raise HTTPException(status_code=404, detail="Advisory observation not found")
    return receipt


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
