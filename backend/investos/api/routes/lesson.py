from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from investos.db import get_session
from investos.schemas.lesson import LessonResponse
from investos.services.lesson import LessonService

router = APIRouter(prefix="/lessons", tags=["lessons"])


@router.get("", response_model=list[LessonResponse])
async def list_lessons(session: AsyncSession = Depends(get_session)):
    return await LessonService(session).list_lessons()
