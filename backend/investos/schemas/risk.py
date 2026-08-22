from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field

from investos.schemas.benchmark import BenchmarkResponse


class ExposureItemResponse(BaseModel):
    label: str
    weight_pct: float
    detail: Optional[str] = None


class RegimeStateResponse(BaseModel):
    regime_type: str
    confidence: float
    signal_source: str
    start_date: datetime
    end_date: Optional[datetime] = None


class ScenarioSummaryResponse(BaseModel):
    name: str
    scenario_description: str
    total_portfolio_impact: float
    portfolio_impact_json: dict = Field(default_factory=dict)
    computed_at: datetime


class PerformanceAttributionItemResponse(BaseModel):
    ticker: str
    name: str
    sector: str
    start_quantity: float
    end_quantity: float
    start_price: float
    end_price: float
    start_price_time: datetime
    end_price_time: datetime
    beginning_value: float
    ending_value: float
    net_flow: float
    gain: float
    contribution_pct: float
    return_pct: Optional[float] = None
    capital_return_pct: Optional[float] = None
    transaction_count: int = 0
    data_status: str = "complete"
    status_detail: Optional[str] = None


class PerformanceAttributionResponse(BaseModel):
    as_of: datetime
    period_start: datetime
    window_days: int
    method: str
    total_beginning_value: float = 0.0
    total_ending_value: float = 0.0
    net_flow: float = 0.0
    gain: float = 0.0
    return_pct: Optional[float] = None
    benchmark_ticker: Optional[str] = None
    benchmark_return_pct: Optional[float] = None
    active_return_pct: Optional[float] = None
    covered_positions: int = 0
    total_positions: int = 0
    coverage_pct: float = 0.0
    unavailable_tickers: list[str] = Field(default_factory=list)
    items: list[PerformanceAttributionItemResponse] = Field(default_factory=list)


class RiskSummaryResponse(BaseModel):
    as_of: datetime
    active_benchmark: Optional[BenchmarkResponse] = None
    benchmark_current_price: Optional[float] = None
    portfolio_return_pct: Optional[float] = None
    benchmark_return_pct: Optional[float] = None
    active_return_pct: Optional[float] = None
    measurement_start: Optional[datetime] = None
    top_sector: Optional[str] = None
    top_sector_weight_pct: float = 0.0
    top_holding: Optional[str] = None
    top_holding_weight_pct: float = 0.0
    concentration_hhi: float = 0.0
    sector_exposures: list[ExposureItemResponse] = Field(default_factory=list)
    asset_class_exposures: list[ExposureItemResponse] = Field(default_factory=list)
    top_positions: list[ExposureItemResponse] = Field(default_factory=list)
    current_regime: Optional[RegimeStateResponse] = None
    scenarios: list[ScenarioSummaryResponse] = Field(default_factory=list)
