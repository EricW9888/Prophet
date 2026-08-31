from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from investos.db import get_session
from investos.schemas.profile import ProfileDetailResponse, ProfileListItem
from investos.services.profile import ProfileService

router = APIRouter(prefix="/profiles", tags=["profiles"])


@router.get("", response_model=list[ProfileListItem])
async def list_profiles(
    show_all: bool = False,
    pinnable: bool = False,
    session: AsyncSession = Depends(get_session),
):
    return await ProfileService(session).list_profiles(show_all=show_all)


@router.get("/{profile_id}", response_model=ProfileDetailResponse)
async def get_profile(profile_id: UUID, session: AsyncSession = Depends(get_session)):
    profile = await ProfileService(session).get_profile(profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")
    return profile
