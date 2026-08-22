from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, Field

from investos.schemas.automation import AutomationJobStatus
from investos.schemas.portfolio import PortfolioBuildPoint, PositionResponse
from investos.schemas.profile import EvidenceNodeResponse, ProfileListItem
from investos.schemas.review import ReviewQueueItemResponse


class DashboardTransactionResponse(BaseModel):
    id: UUID
    ticker: str
    entity_name: Optional[str] = None
    action: str
    quantity: float
    price: Optional[float] = None
    executed_at: datetime
    source_type: Optional[str] = None
    source_label: Optional[str] = None
    source_evidence_id: Optional[UUID] = None
    source_confidence: Optional[float] = None
    provenance: dict = Field(default_factory=dict)


class DashboardQuestionResponse(BaseModel):
    id: UUID
    subject_type: str
    subject_name: str
    question_text: str
    urgency: int
    created_at: datetime


class DashboardShadowSummaryResponse(BaseModel):
    id: UUID
    name: str
    policy_description: str
    run_status: str
    created_at: datetime
    completed_at: Optional[datetime] = None
    alpha: Optional[float] = None
    shadow_return: Optional[float] = None
    actual_return: Optional[float] = None


class DashboardLlmUsageResponse(BaseModel):
    analysis_runs_24h: int
    cached_runs_24h: int
    verification_runs_24h: int
    total_input_tokens_24h: int
    total_output_tokens_24h: int
    avg_duration_ms: float = 0.0


class DashboardLessonResponse(BaseModel):
    id: UUID
    title: str
    summary: str
    lesson_type: str
    created_at: datetime


class DashboardSourceResponse(BaseModel):
    id: UUID
    name: str
    source_type: str
    is_trusted: bool
    updated_at: datetime


class DashboardResearchActivityResponse(BaseModel):
    automation_enabled: bool
    provider_configured: bool
    open_question_count: int
    pending_evidence_count: int
    latest_run_at: Optional[datetime] = None
    latest_status: Optional[str] = None
    latest_detail: Optional[str] = None
    latest_item_title: Optional[str] = None
    latest_item_subject_name: Optional[str] = None
    latest_item_created_at: Optional[datetime] = None
    latest_item_processed: bool = False


class DashboardResearchActionResponse(BaseModel):
    timestamp: str
    status: str
    summary: str
    title: Optional[str] = None
    query: Optional[str] = None
    search_depth: Optional[str] = None


class DashboardAgentActionResponse(BaseModel):
    id: str
    timestamp: str
    source: str
    action_type: str
    status: str
    summary: str
    subject_id: Optional[str] = None
    subject_type: Optional[str] = None
    subject_name: Optional[str] = None
    metadata: dict = {}


class DashboardPortfolioMonitorItemResponse(BaseModel):
    item_label: str
    item_type: str
    priority_score: float
    trigger_reason: str


class DashboardPortfolioResearchItemResponse(BaseModel):
    id: str
    title: str
    subject_name: str
    created_at: datetime
    is_processed: bool


class DashboardPortfolioMonitorResponse(BaseModel):
    monitored_holding_count: int
    priority_review_count: int
    priority_review_items: list[DashboardPortfolioMonitorItemResponse]
    recent_research_items: list[DashboardPortfolioResearchItemResponse]


class DashboardSummaryResponse(BaseModel):
    as_of: datetime
    holdings_count: int = 0
    watchlist_count: int = 0
    considering_count: int = 0
    total_market_value: float = 0.0
    total_unrealized_pnl: float = 0.0
    total_value: float = 0.0
    buying_power: float = 0.0
    top_winners: list[PositionResponse] = []
    top_losers: list[PositionResponse] = []
    portfolio_build_series: list[PortfolioBuildPoint] = []
    profile_count: int = 0
    evidence_node_count: int = 0
    active_evidence_node_count: int = 0
    deprecated_evidence_node_count: int = 0
    open_questions_count: int = 0
    pending_shadow_experiments_count: int = 0
    automation_enabled: bool = False
    jobs: list[AutomationJobStatus] = []
    recent_transactions: list[DashboardTransactionResponse] = []
    recent_evidence: list[EvidenceNodeResponse] = []
    recent_profiles: list[ProfileListItem] = []
    open_questions: list[DashboardQuestionResponse] = []
    review_queue: list[ReviewQueueItemResponse] = []
    recent_lessons: list[DashboardLessonResponse] = []
    trusted_sources: list[DashboardSourceResponse] = []
    research_activity: DashboardResearchActivityResponse
    recent_research_actions: list[DashboardResearchActionResponse] = []
    recent_agent_actions: list[DashboardAgentActionResponse] = []
    portfolio_monitor: DashboardPortfolioMonitorResponse
    recent_shadow_experiments: list[DashboardShadowSummaryResponse] = []
    llm_usage: DashboardLlmUsageResponse
    active_benchmark_ticker: Optional[str] = None
    portfolio_return_pct: Optional[float] = None
    benchmark_return_pct: Optional[float] = None
    active_return_pct: Optional[float] = None
    top_sector: Optional[str] = None
    top_sector_weight_pct: float = 0.0
    current_regime: Optional[str] = None
