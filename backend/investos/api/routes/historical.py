from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from investos.db import get_session
from investos.services.historical import HistoricalEpisodeService

router = APIRouter(prefix="/historical", tags=["historical"])


@router.get("/episodes")
async def list_episodes(session: AsyncSession = Depends(get_session)):
    svc = HistoricalEpisodeService(session)
    return [svc._format(ep, 0) for ep in await svc.list_episodes()]


@router.get("/analogies")
async def find_analogies(
    query: str,
    limit: int = 3,
    session: AsyncSession = Depends(get_session),
):
    svc = HistoricalEpisodeService(session)
    return await svc.find_analogies(query, limit=limit)


@router.post("/seed")
async def seed_episodes(session: AsyncSession = Depends(get_session)):
    svc = HistoricalEpisodeService(session)
    created = await svc.seed_default_episodes()
    return {"created": created}
