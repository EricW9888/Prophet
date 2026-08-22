from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from investos.db import get_session
from investos.services.operating_state import OperatingStateService

router = APIRouter(prefix="/discoveries", tags=["discoveries"])


@router.get("")
async def list_discoveries(session: AsyncSession = Depends(get_session)):
    try:
        return await OperatingStateService(session).discoveries_payload()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/{subject_id}/approve")
async def approve_discovery(
    subject_id: UUID,
    subject_type: str = "entity",
    session: AsyncSession = Depends(get_session),
):
    try:
        success = await OperatingStateService(session).approve_discovery(
            subject_id, subject_type
        )
        return {"success": success}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/{subject_id}/dismiss")
async def dismiss_discovery(
    subject_id: UUID,
    subject_type: str = "entity",
    reason: str | None = None,
    session: AsyncSession = Depends(get_session),
):
    try:
        success = await OperatingStateService(session).dismiss_discovery(
            subject_id, subject_type, reason
        )
        return {"success": success}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))
