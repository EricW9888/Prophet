from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from investos.db import get_session
from investos.schemas.verification import VerificationRequest, VerificationResponse
from investos.services.verification import VerificationService

router = APIRouter(prefix="/verification", tags=["verification"])


@router.post("/", response_model=VerificationResponse)
async def run_verification(
    payload: VerificationRequest,
    session: AsyncSession = Depends(get_session),
):
    try:
        return await VerificationService(session).run(payload)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))
