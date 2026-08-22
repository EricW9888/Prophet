from datetime import datetime
from typing import Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from investos.schemas.lesson import LessonResponse


class ShadowExperimentCreate(BaseModel):
    name: str
    policy_description: str
    start_point: Optional[datetime] = None
    end_point: Optional[datetime] = None
    trigger_type: Optional[str] = None
    trigger_reason: Optional[str] = None
    horizon_label: Optional[str] = None
    initiated_by: Optional[str] = None
    operator_prompt: Optional[str] = None
    discovery_profile: Optional[dict] = None
    subject_refs: list[dict] = Field(default_factory=list)
    auto_run: bool = True
    account_basis: Literal["clone_portfolio", "cash_only"] = "clone_portfolio"
    starting_cash: Optional[float] = Field(default=None, ge=0)


class ShadowOrderCreate(BaseModel):
    ticker: str = Field(min_length=1, max_length=24)
    side: Literal["buy", "sell"]
    quantity: float = Field(gt=0)
    rationale: str = Field(default="User-submitted paper order.", max_length=1000)


class ShadowActionResponse(BaseModel):
    id: UUID
    experiment_id: UUID
    action: str
    security_id: UUID
    quantity: float
    price: float
    simulated_timestamp: datetime
    rationale: str

    model_config = ConfigDict(from_attributes=True)


class ShadowOrderResponse(BaseModel):
    id: UUID
    experiment_id: UUID
    security_id: UUID
    ticker: str
    client_order_id: str
    provider: str
    side: str
    order_type: str
    time_in_force: str
    status: str
    requested_quantity: float
    filled_quantity: float
    reference_price: float
    filled_avg_price: Optional[float] = None
    reserved_notional: float
    quote_session: Optional[str] = None
    quote_time: Optional[datetime] = None
    submitted_at: datetime
    accepted_at: Optional[datetime] = None
    filled_at: Optional[datetime] = None
    canceled_at: Optional[datetime] = None
    rejection_reason: Optional[str] = None
    rationale: str
    checkpoint_index: int
    evidence_refs_json: list = Field(default_factory=list)
    source_decision_json: dict = Field(default_factory=dict)

    model_config = ConfigDict(from_attributes=True)


class ShadowFillResponse(BaseModel):
    id: UUID
    order_id: UUID
    experiment_id: UUID
    security_id: UUID
    side: str
    quantity: float
    price: float
    gross_notional: float
    fee: float
    slippage_bps: float
    filled_at: datetime
    quote_time: Optional[datetime] = None
    quote_session: Optional[str] = None
    cash_after: float
    position_quantity_after: float

    model_config = ConfigDict(from_attributes=True)


class ShadowAccountEventResponse(BaseModel):
    id: UUID
    experiment_id: UUID
    source_transaction_id: UUID
    security_id: UUID
    ticker: str
    event_type: str
    status: str
    occurred_at: datetime
    applied_at: datetime
    quantity_before: float
    quantity_after: float
    cash_before: float
    cash_after: float
    amount: float
    derivation: str
    detail: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class ShadowPaperPositionResponse(BaseModel):
    security_id: UUID
    ticker: str
    quantity: float
    avg_cost_basis: float
    current_price: float
    market_value: float
    unrealized_pnl: float
    weight_pct: float
    marked_at: Optional[datetime] = None


class ExperimentResultResponse(BaseModel):
    id: UUID
    experiment_id: UUID
    shadow_return: float
    actual_return: float
    alpha: float
    max_drawdown: float
    sharpe_ratio: Optional[float] = None
    reasoning: str

    model_config = ConfigDict(from_attributes=True)


class ShadowExperimentResponse(BaseModel):
    id: UUID
    name: str
    policy_description: str
    start_point: datetime
    end_point: datetime
    trigger_type: Optional[str] = None
    trigger_reason: Optional[str] = None
    horizon_label: Optional[str] = None
    initiated_by: Optional[str] = None
    execution_mode: str = "autonomous"
    operator_prompt: Optional[str] = None
    discovery_profile: Optional[dict] = None
    guidance_mode: Optional[str] = None
    guidance_summary: Optional[str] = None
    snapshot_summary: dict = Field(default_factory=dict)
    run_details: dict = Field(default_factory=dict)
    report: dict = Field(default_factory=dict)
    run_status: str
    skip_reason: Optional[str] = None
    created_at: datetime
    completed_at: Optional[datetime] = None
    actions: list[ShadowActionResponse] = Field(default_factory=list)
    orders: list[ShadowOrderResponse] = Field(default_factory=list)
    fills: list[ShadowFillResponse] = Field(default_factory=list)
    account_events: list[ShadowAccountEventResponse] = Field(default_factory=list)
    paper_positions: list[ShadowPaperPositionResponse] = Field(default_factory=list)
    result: Optional[ExperimentResultResponse] = None
    lesson: Optional[LessonResponse] = None

    model_config = ConfigDict(from_attributes=True)
