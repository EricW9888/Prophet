from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from investos.db import get_session
from investos.schemas.automation import AutomationJobStatus
from investos.schemas.dashboard import DashboardSummaryResponse
from investos.services.dashboard import DashboardService

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@router.get("/summary", response_model=DashboardSummaryResponse)
async def get_dashboard_summary(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    coordinator = getattr(request.app.state, "automation", None)
    jobs = []
    if coordinator:
        jobs = [
            AutomationJobStatus(
                name=item.name,
                enabled=item.enabled,
                interval_seconds=item.interval_seconds,
                last_run_at=item.last_run_at,
                last_status=item.last_status,
                detail=item.detail,
            )
            for item in coordinator.status()
        ]
    return await DashboardService(session).get_summary(
        automation_enabled=bool(coordinator),
        jobs=jobs,
    )
