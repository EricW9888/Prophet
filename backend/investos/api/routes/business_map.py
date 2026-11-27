from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from investos.db import get_session
from investos.services.business_map import BusinessMapService

router = APIRouter(prefix="/business-map", tags=["business-map"])


@router.get("/{entity_id}")
async def get_business_map(
    entity_id: UUID, session: AsyncSession = Depends(get_session)
):
    """Where a company's revenue comes from, per its latest annual report, or null."""
    return await BusinessMapService(session).latest_map(entity_id)
