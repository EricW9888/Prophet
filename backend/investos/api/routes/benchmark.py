from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from investos.db import get_session
from investos.schemas.benchmark import BenchmarkCreate, BenchmarkResponse
from investos.services.risk import BenchmarkService

router = APIRouter(prefix="/benchmarks", tags=["benchmarks"])


@router.get("", response_model=list[BenchmarkResponse])
async def list_benchmarks(session: AsyncSession = Depends(get_session)):
    return await BenchmarkService(session).list_benchmarks()


@router.post("", response_model=BenchmarkResponse)
async def create_benchmark(
    payload: BenchmarkCreate,
    session: AsyncSession = Depends(get_session),
):
    try:
        return await BenchmarkService(session).create_benchmark(payload)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))
