from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field


class PortfolioHistoryPoint(BaseModel):
    at: datetime
    value: float | None
    gain: float | None = None
    return_pct: float | None = None
    covered_positions: int
    total_positions: int
    unavailable_tickers: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)


class PortfolioHistoryEvent(BaseModel):
    id: str
    at: datetime
    kind: Literal["ledger", "research", "alert"]
    label: str
    detail: str | None = None
    href: str


class PortfolioHistoryResponse(BaseModel):
    receipt_id: UUID
    computed_at: datetime
    cache_hit: bool = False
    window_days: int
    points: list[PortfolioHistoryPoint]
    events: list[PortfolioHistoryEvent]
    events_truncated: bool = False
    reasons: list[str] = Field(default_factory=list)
