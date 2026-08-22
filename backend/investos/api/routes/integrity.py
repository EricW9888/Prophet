from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from investos.db import get_session
from investos.schemas.integrity import IntegrityAuditResponse
from investos.services.integrity import IntegrityService

router = APIRouter(prefix="/integrity", tags=["integrity"])


@router.get("/state", response_model=IntegrityAuditResponse)
async def get_integrity_state(
    session: AsyncSession = Depends(get_session),
) -> IntegrityAuditResponse:
    return await IntegrityService(session).audit_state()


@router.post("/repair")
async def run_integrity_repair(
    dry_run: bool = False,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Heal detected corruption (orphan/duplicate edges, duplicate canonical
    rows, corrupt fallback theses). Pass dry_run=true to preview without writing."""
    return await IntegrityService(session).repair_state(dry_run=dry_run)


@router.get("/repair/latest")
async def get_latest_repair(
    session: AsyncSession = Depends(get_session),
) -> dict:
    return IntegrityService(session).latest_repair_audit() or {
        "actions": {},
        "total_repaired": 0,
    }
