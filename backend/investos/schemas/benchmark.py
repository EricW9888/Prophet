from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel


class BenchmarkCreate(BaseModel):
    ticker: str
    name: Optional[str] = None
    description: Optional[str] = None
    benchmark_type: str = "broad_market"


class BenchmarkResponse(BaseModel):
    id: UUID
    ticker: Optional[str] = None
    name: str
    description: Optional[str] = None
    benchmark_type: str
    created_at: datetime
