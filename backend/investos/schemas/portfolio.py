from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class TransactionBase(BaseModel):
    action: str
    quantity: float
    price: Optional[float] = None
    executed_at: datetime
    notes: Optional[str] = None
    lot_type: Optional[str] = None
    provenance_json: Optional[dict] = None


class TransactionCreate(TransactionBase):
    pass


class TransactionCreateByTicker(TransactionCreate):
    ticker: str
    list_type: str = "holding"
    direction: str = "long"


class TransactionCorrectionRequest(BaseModel):
    action: Optional[str] = None
    quantity: Optional[float] = None
    price: Optional[float] = None
    executed_at: Optional[datetime] = None
    notes: Optional[str] = None
    lot_type: Optional[str] = None
    reason: Optional[str] = None


class ResearchObjectCreate(BaseModel):
    ticker: str
    entity_name: Optional[str] = None
    list_type: str = "watchlist"
    direction: str = "long"
    conviction: Optional[int] = None
    summary: Optional[str] = None
    bull_case: Optional[str] = None
    bear_case: Optional[str] = None
    open_questions: list[str] = Field(default_factory=list)


class ResearchObjectResponse(BaseModel):
    position_id: UUID
    profile_id: UUID
    coverage_map_id: UUID
    ticker: str
    entity_name: str
    list_type: str
    open_question_count: int


class TransactionResponse(TransactionBase):
    id: UUID
    position_id: Optional[UUID] = None
    status: str = "settled"
    superseded_by_id: Optional[UUID] = None
    ticker: Optional[str] = None
    entity_name: Optional[str] = None
    source_type: Optional[str] = None
    source_label: Optional[str] = None
    source_evidence_id: Optional[UUID] = None
    source_confidence: Optional[float] = None
    provenance: dict = Field(default_factory=dict)

    model_config = ConfigDict(from_attributes=True)


class TransactionCorrectionResponse(BaseModel):
    original: TransactionResponse
    replacement: Optional[TransactionResponse] = None
    reason: Optional[str] = None
    corrected_at: Optional[datetime] = None


class LotBase(BaseModel):
    quantity: float
    cost_basis: float
    acquired_at: datetime
    lot_type: Optional[str] = None
    realized_pnl: Optional[float] = 0.0
    closed_at: Optional[datetime] = None


class LotResponse(LotBase):
    id: UUID
    position_id: UUID

    model_config = ConfigDict(from_attributes=True)


class PositionBase(BaseModel):
    direction: str
    list_type: str
    quantity: float = 0.0
    avg_cost_basis: float = 0.0
    current_price: float = 0.0
    market_value: float = 0.0
    unrealized_pnl: float = 0.0
    realized_pnl: float = 0.0
    weight_pct: float = 0.0
    is_nonlinear: bool = False
    notional_exposure: Optional[float] = None
    delta_exposure: Optional[float] = None
    conviction: Optional[int] = None
    added_at: datetime
    last_reviewed_at: Optional[datetime] = None
    review_urgency: Optional[int] = None


class PositionCreate(BaseModel):
    security_id: UUID
    direction: str = "long"
    list_type: str = "holding"
    conviction: Optional[int] = None


class PositionResponse(PositionBase):
    id: UUID
    security_id: UUID
    ticker: Optional[str] = None
    entity_name: Optional[str] = None

    lots: list[LotResponse] = Field(default_factory=list)
    transactions: list[TransactionResponse] = Field(default_factory=list)

    model_config = ConfigDict(from_attributes=True)


class PortfolioImportRow(BaseModel):
    ticker: str
    action: str
    quantity: float
    price: Optional[float] = None
    executed_at: datetime
    notes: Optional[str] = None
    list_type: str = "holding"
    direction: str = "long"


class PortfolioSimpleImportRequest(BaseModel):
    mode: str = "transactions"
    content: str
    default_executed_at: Optional[datetime] = None


class PortfolioImportResponse(BaseModel):
    imported_count: int
    skipped_count: int
    errors: list[str] = Field(default_factory=list)
    inferred_remaining_buying_power: Optional[float] = None
    inference_note: Optional[str] = None


class PortfolioBuildPoint(BaseModel):
    as_of: datetime
    net_capital_deployed: float
    gross_trade_notional: float
    active_holding_count: int
    transaction_count: int


class PortfolioOverviewResponse(BaseModel):
    holdings: list[PositionResponse] = Field(default_factory=list)
    watchlist: list[PositionResponse] = Field(default_factory=list)
    considering: list[PositionResponse] = Field(default_factory=list)
    recent_transactions: list[TransactionResponse] = Field(default_factory=list)
    top_winners: list[PositionResponse] = Field(default_factory=list)
    top_losers: list[PositionResponse] = Field(default_factory=list)
    total_value: float = 0.0
    buying_power: float = 0.0
    build_series: list[PortfolioBuildPoint] = Field(default_factory=list)


class ReconcileHolding(BaseModel):
    ticker: str
    quantity: float


class ReconcileRequest(BaseModel):
    holdings: list[ReconcileHolding] = Field(default_factory=list)
    cash: Optional[float] = None
    create_review_items: bool = True


class ReconcileTextRequest(BaseModel):
    """Paste/CSV holdings snapshot (e.g. a Robinhood statement export)."""

    text: str
    create_review_items: bool = True


class ReconcileDiff(BaseModel):
    ticker: str
    kind: str  # missing_in_book|extra_in_book|quantity_mismatch
    book_quantity: float
    broker_quantity: float
    delta: float


class CashDiscrepancy(BaseModel):
    book_cash: float
    broker_cash: float
    delta: float


class ReconcileResponse(BaseModel):
    in_sync: bool
    discrepancies: list[ReconcileDiff] = Field(default_factory=list)
    cash_discrepancy: Optional[CashDiscrepancy] = None
    review_items_created: int = 0
