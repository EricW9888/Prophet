from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from investos.db import get_session
from investos.schemas.dashboard import DashboardAgentActionResponse
from investos.services.dashboard import DashboardService

router = APIRouter(prefix="/activity", tags=["activity"])


@router.get("/agent", response_model=list[DashboardAgentActionResponse])
async def get_agent_activity(
    limit: int = Query(default=100, ge=1, le=500),
    source: str | None = Query(default=None),
    action_type: str | None = Query(default=None),
    status: str | None = Query(default=None),
    session_id: str | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
):
    return DashboardService(session).recent_agent_activity(
        limit=limit,
        source=source,
        action_type=action_type,
        status=status,
        session_id=session_id,
    )
