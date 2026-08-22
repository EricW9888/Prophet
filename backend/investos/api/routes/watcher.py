from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from investos.db import get_session
from investos.models.watcher import ActiveWatcher
from investos.schemas.watcher import ActiveWatcherResponse
from investos.services.watcher import WatcherService

router = APIRouter(prefix="/watcher", tags=["watcher"])


@router.get("/active", response_model=list[ActiveWatcherResponse])
async def list_active_watchers(session: AsyncSession = Depends(get_session)):
    return await WatcherService(session).list_active_with_countdowns()


@router.get("/reminders", response_model=list[ActiveWatcherResponse])
async def list_active_reminders(session: AsyncSession = Depends(get_session)):
    return await WatcherService(session).list_reminders()


@router.post("/{watcher_id}/deactivate")
async def deactivate_watcher(
    watcher_id: UUID, session: AsyncSession = Depends(get_session)
):
    from sqlalchemy import update

    await session.execute(
        update(ActiveWatcher)
        .where(ActiveWatcher.id == watcher_id)
        .values(is_active=False)
    )
    await session.commit()
    return {"status": "ok"}
