from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from investos.core.url_security import UnsafeUrlError, resolve_public_url
from investos.db import get_session
from investos.models.news import PublisherFeed
from investos.services.news_intake import NewsIntakeService

router = APIRouter(prefix="/news-intake", tags=["sources"])


class FeedCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=120)
    url: str = Field(min_length=1, max_length=2000)


class FeedUpdate(BaseModel):
    enabled: bool


@router.get("")
async def status(
    offset: int = Query(0, ge=0), session: AsyncSession = Depends(get_session)
):
    return await NewsIntakeService(session).status(offset=offset)


@router.post("/feeds")
async def add_feed(
    payload: FeedCreate, request: Request, session: AsyncSession = Depends(get_session)
):
    try:
        await resolve_public_url(payload.url)
    except (UnsafeUrlError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext('prophet:publisher-feeds'))")
    )
    if await session.scalar(
        select(PublisherFeed.id).where(PublisherFeed.url == payload.url)
    ):
        raise HTTPException(409, "This feed is already configured.")
    if (await session.scalar(select(func.count()).select_from(PublisherFeed))) >= 20:
        raise HTTPException(409, "At most 20 publisher feeds may be configured.")
    row = PublisherFeed(url=payload.url, name=payload.name.strip())
    session.add(row)
    await session.commit()
    coordinator = getattr(request.app.state, "automation", None)
    if coordinator is not None:
        coordinator.signal_ready_work("news_feed_changed")
    return {"id": row.id, "enabled": row.enabled}


@router.patch("/feeds/{feed_id}")
async def update_feed(
    feed_id: UUID,
    payload: FeedUpdate,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    row = await session.get(PublisherFeed, feed_id, with_for_update=True)
    if row is None:
        raise HTTPException(404, "Feed not found.")
    row.enabled = payload.enabled
    if payload.enabled:
        row.next_poll_at = None
    await session.commit()
    coordinator = getattr(request.app.state, "automation", None)
    if coordinator is not None:
        coordinator.signal_ready_work("news_feed_changed")
    return {"id": row.id, "enabled": row.enabled}
