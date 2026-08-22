from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from investos.db import get_session
from investos.schemas.reasoning import ReasoningRunTraceResponse
from investos.services.reasoning_trace import ReasoningTraceService

router = APIRouter(prefix="/reasoning", tags=["reasoning"])


@router.get("/runs/{run_id}", response_model=ReasoningRunTraceResponse)
async def get_reasoning_run(run_id: UUID, session: AsyncSession = Depends(get_session)):
    trace = await ReasoningTraceService(session).get_run_trace(run_id)
    if trace is None:
        raise HTTPException(status_code=404, detail="Reasoning run not found")
    return trace
