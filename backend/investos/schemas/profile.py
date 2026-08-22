from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class ProfileListItem(BaseModel):
    id: UUID
    subject_type: str
    subject_id: UUID
    subject_name: str
    executive_summary: Optional[str] = None
    current_stance: Optional[str] = None
    confidence_band: Optional[str] = None
    coverage_score: Optional[float] = None
    updated_at: datetime


class MissingEvidenceResponse(BaseModel):
    id: UUID
    class_name: str
    importance_to_thesis: str
    identified_at: datetime

    model_config = ConfigDict(from_attributes=True)


class UnresolvedQuestionResponse(BaseModel):
    id: UUID
    question_text: str
    urgency: int
    status: str
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class EvidenceNodeResponse(BaseModel):
    id: UUID
    node_type: str
    text: str
    tier: Optional[str] = None
    subject_name: Optional[str] = None
    source_name: Optional[str] = None
    source_type: Optional[str] = None
    created_at: datetime


class HistoricalAnalogyLensResponse(BaseModel):
    name: str
    period: Optional[str] = None
    lens_use_policy: Optional[str] = None
    current_application_prompt: Optional[str] = None
    what_rhymes: Optional[str] = None
    dominant_channel_test: Optional[str] = None
    where_analogy_breaks: Optional[str] = None
    portfolio_transmission: Optional[str] = None
    best_next_check: Optional[str] = None
    investor_questions: list[str] = []


class FundamentalMetricContextResponse(BaseModel):
    id: str
    metric_name: str
    metric_family: str
    ticker: Optional[str] = None
    value_text: Optional[str] = None
    numeric_value: Optional[float] = None
    unit: Optional[str] = None
    currency: Optional[str] = None
    period_label: Optional[str] = None
    as_of: Optional[str] = None
    public_time: Optional[str] = None
    stale_after: Optional[str] = None
    direction: Optional[str] = None
    confidence: float = 0.0
    investment_relevance: Optional[str] = None
    next_test: Optional[str] = None
    freshness_status: Optional[str] = None
    source_name: Optional[str] = None
    source_type: Optional[str] = None
    evidence_title: Optional[str] = None
    url: Optional[str] = None


class MarketSetupContextResponse(BaseModel):
    id: str
    signal_name: str
    signal_family: str
    ticker: Optional[str] = None
    setup_context: Optional[str] = None
    actual_context: Optional[str] = None
    price_reaction: Optional[str] = None
    value_text: Optional[str] = None
    numeric_value: Optional[float] = None
    unit: Optional[str] = None
    currency: Optional[str] = None
    period_label: Optional[str] = None
    as_of: Optional[str] = None
    public_time: Optional[str] = None
    direction: Optional[str] = None
    confidence: float = 0.0
    investment_relevance: Optional[str] = None
    next_test: Optional[str] = None
    outcome_status: Optional[str] = None
    outcome_score: Optional[float] = None
    outcome_assessment: Optional[dict] = None
    outcome_assessment_attempt: Optional[dict] = None
    source_name: Optional[str] = None
    source_type: Optional[str] = None
    evidence_title: Optional[str] = None
    url: Optional[str] = None


class ProfileDetailResponse(BaseModel):
    id: UUID
    subject_type: str
    subject_id: UUID
    subject_name: str
    executive_summary: Optional[str] = None
    bull_case: Optional[str] = None
    bear_case: Optional[str] = None
    active_contradictions: list[str] = []
    current_stance: Optional[str] = None
    confidence_band: Optional[str] = None
    current_thesis_summary: Optional[str] = None
    what_would_falsify: list[str] = []
    coverage_score: Optional[float] = None
    missing_evidence: list[MissingEvidenceResponse] = []
    unresolved_questions: list[UnresolvedQuestionResponse] = []
    recent_evidence: list[EvidenceNodeResponse] = []
    historical_analogy_lenses: list[HistoricalAnalogyLensResponse] = []
    fundamental_metrics: list[FundamentalMetricContextResponse] = []
    market_setup_signals: list[MarketSetupContextResponse] = []
    updated_at: datetime
