from datetime import date, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class RiskWatchPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    loss_budget: float = Field(gt=0, le=1e12)
    currency: Literal["USD"] = "USD"
    max_age_hours: int = Field(default=96, ge=1, le=168)


class PortfolioRiskAssessment(BaseModel):
    id: UUID
    detected_at: datetime
    packet_hash: str
    result: dict[str, Any]


class CapturedHoldingsValue(BaseModel):
    assessment_id: UUID
    captured_at: datetime
    price_session: date | None = None
    covered_positions: int
    total_positions: int
    value_usd: float | None = None


class PortfolioRiskState(BaseModel):
    checked_at: datetime
    last_refresh_at: datetime | None = None
    refresh_interval_seconds: int
    portfolio_data_review_count: int = 0
    automation_enabled: bool
    market_data_enabled: bool
    assessment: PortfolioRiskAssessment | None = None
    watch: dict[str, Any] | None = None
    delivery: list[dict[str, Any]] = Field(default_factory=list)


class RiskWatchRequest(RiskWatchPolicy):
    enabled: bool = True
