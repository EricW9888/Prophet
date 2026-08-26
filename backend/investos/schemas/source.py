from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class SourceCreate(BaseModel):
    name: str
    source_type: str
    url: Optional[str] = None
    description: Optional[str] = None
    is_trusted: bool = False


class SourceUpdate(BaseModel):
    name: Optional[str] = None
    source_type: Optional[str] = None
    url: Optional[str] = None
    description: Optional[str] = None
    is_trusted: Optional[bool] = None


class SourceTrustSummary(BaseModel):
    factual_reliability: Optional[str] = None
    noise_ratio: Optional[str] = None
    trust_trajectory: Optional[str] = None
    correction_quality: Optional[str] = None


class SourceValueSummary(BaseModel):
    idea_generation_value: Optional[str] = None
    timing_value: Optional[str] = None
    portfolio_relevance_value: Optional[str] = None
    specificity: Optional[str] = None
    originality: Optional[str] = None


class SourceQualitySegmentResponse(BaseModel):
    domain: Optional[str] = None
    ticker: Optional[str] = None
    horizon: Optional[str] = None
    regime: Optional[str] = None
    quality_score: float
    originality_score: float
    timing_usefulness: float
    evidence_count: int
    notes: Optional[str] = None


class SourceOriginSummary(BaseModel):
    origin_kind: str
    origin_label: str
    origin_detail: Optional[str] = None


class SourcePerformanceHistoryResponse(BaseModel):
    id: UUID
    source_id: UUID
    domain: Optional[str] = None
    sector: Optional[str] = None
    regime: Optional[str] = None
    period_start: datetime
    period_end: datetime
    total_claims: int
    correct_claims: int
    incorrect_claims: int
    accuracy_rate: float
    originality_rate: float
    timing_score: float
    computed_at: datetime


class SourceClaimQueueSummary(BaseModel):
    total: int = 0
    pending: int = 0
    deferred: int = 0
    assessed: int = 0
    last_assessment_at: Optional[datetime] = None


class SourceRecentItemResponse(BaseModel):
    id: UUID
    title: Optional[str] = None
    url: Optional[str] = None
    created_at: datetime
    source_item_type: Optional[str] = None
    is_processed: bool
    origin_kind: str
    origin_label: str
    origin_detail: Optional[str] = None
    user_feedback: Optional[dict] = None


class SourceResponse(BaseModel):
    id: UUID
    name: str
    source_type: str
    url: Optional[str] = None
    description: Optional[str] = None
    is_trusted: bool
    origin: SourceOriginSummary = Field(
        default_factory=lambda: SourceOriginSummary(
            origin_kind="catalog",
            origin_label="Source catalog",
            origin_detail=None,
        )
    )
    evidence_count: int = 0
    trust_profile: Optional[SourceTrustSummary] = None
    value_profile: Optional[SourceValueSummary] = None
    quality_segments: list[SourceQualitySegmentResponse] = []
    performance_history: list[SourcePerformanceHistoryResponse] = []
    claim_queue: SourceClaimQueueSummary = Field(
        default_factory=SourceClaimQueueSummary
    )
    recent_items: list[SourceRecentItemResponse] = []
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class SourceEvidenceSummary(BaseModel):
    id: UUID
    source_id: UUID
    source_name: str
    source_type: str
    title: Optional[str] = None
    url: Optional[str] = None
    source_item_type: str
    is_processed: bool
    origin_kind: str
    origin_label: str
    origin_detail: Optional[str] = None
    user_feedback: Optional[dict] = None
    created_at: datetime
    updated_at: datetime


class SourceEvidenceDetail(SourceEvidenceSummary):
    author: Optional[str] = None
    external_id: Optional[str] = None
    raw_content_ref: Optional[str] = None
    event_time: Optional[datetime] = None
    public_time: Optional[datetime] = None
    ingest_time: Optional[datetime] = None
    eligible_action_time: Optional[datetime] = None
    metadata: dict = Field(default_factory=dict)
    source_item_summary: Optional[str] = None
    source_item_excerpt: Optional[str] = None
    source_item_processing_status: Optional[str] = None


class MediaIngestionCapability(BaseModel):
    key: str
    label: str
    status: str
    detail: str


class MediaIngestionCapabilityResponse(BaseModel):
    can_extract_without_transcript: bool
    current_best_path: str
    capabilities: list[MediaIngestionCapability] = Field(default_factory=list)


class YouTubeIngestionRequest(BaseModel):
    url: str = Field(min_length=1, max_length=2048)
    title: Optional[str] = Field(default=None, max_length=500)
    source_id: Optional[UUID] = None


class YouTubeChannelVideoResponse(BaseModel):
    video_id: str
    title: str
    url: str
    published_at: Optional[datetime] = None
    duration_seconds: Optional[float] = None
    view_count: Optional[int] = None
    live_status: Optional[str] = None
    availability: Optional[str] = None
    already_ingested: bool = False
    evidence_id: Optional[UUID] = None


class YouTubeChannelPreviewResponse(BaseModel):
    source_id: UUID
    source_name: str
    channel_url: str
    channel_id: Optional[str] = None
    channel_name: Optional[str] = None
    videos: list[YouTubeChannelVideoResponse] = Field(default_factory=list)


class MediaIngestionJobEventResponse(BaseModel):
    phase: str
    message: str
    created_at: datetime
    detail: Optional[dict] = None


class MediaIngestionJobResponse(BaseModel):
    job_id: UUID
    status: str
    request_url: str
    created_at: datetime
    updated_at: datetime
    events: list[MediaIngestionJobEventResponse] = Field(default_factory=list)
    result: Optional[dict] = None
    error: Optional[str] = None


class SourceFeedbackCreate(BaseModel):
    evidence_id: UUID
    rating: str
    note: Optional[str] = None
    context: Optional[str] = None


class SourceClaimAssessmentCreate(BaseModel):
    assessment: str
    notes: Optional[str] = None
    assessment_evidence: list[UUID] = Field(default_factory=list)
    horizon_days: Optional[int] = None


class SourceClaimAssessmentResponse(BaseModel):
    id: UUID
    source_id: UUID
    claim_id: UUID
    assessment: str
    assessment_time: Optional[datetime] = None
    horizon_days: Optional[int] = None
    notes: Optional[str] = None
    assessment_attempt_count: int = 0
    last_assessment_attempt_at: Optional[datetime] = None
    next_assessment_at: Optional[datetime] = None
    assessment_metadata: Optional[dict] = None
    performance_history: Optional[SourcePerformanceHistoryResponse] = None


class SourceClaimAutoAssessmentCreate(BaseModel):
    apply: bool = False
    min_confidence: float = Field(default=0.75, ge=0.0, le=1.0)


class SourceClaimResearchFollowupResponse(BaseModel):
    started: bool
    reason: str
    evidence_id: Optional[UUID] = None
    processed: bool = False
    query: Optional[str] = None
    title: Optional[str] = None


class OwnershipDisclosureCreate(BaseModel):
    source_name: str
    source_type: str = "ownership_tracker"
    source_url: Optional[str] = None
    source_description: Optional[str] = None
    source_item_type: str = "ownership_disclosure"
    title: Optional[str] = None
    url: Optional[str] = None
    external_id: Optional[str] = None
    author: Optional[str] = None
    metadata: dict = Field(default_factory=dict)
    summary: Optional[str] = None
    event_time: Optional[datetime] = None
    public_time: Optional[datetime] = None
    eligible_action_time: Optional[datetime] = None


class MarketSetupSignalCreate(BaseModel):
    signal_name: str
    signal_family: Optional[str] = None
    subject_type: Optional[str] = None
    subject_id: Optional[UUID] = None
    entity_id: Optional[UUID] = None
    security_id: Optional[UUID] = None
    ticker: Optional[str] = None
    event_id: Optional[UUID] = None
    raw_evidence_id: Optional[UUID] = None
    source_item_id: Optional[UUID] = None
    setup_context: Optional[str] = None
    actual_context: Optional[str] = None
    price_reaction: Optional[str] = None
    value_text: Optional[str] = None
    numeric_value: Optional[float] = None
    unit: Optional[str] = None
    currency: Optional[str] = None
    period_label: Optional[str] = None
    as_of: Optional[datetime] = None
    event_time: Optional[datetime] = None
    public_time: Optional[datetime] = None
    eligible_action_time: Optional[datetime] = None
    direction: Optional[str] = None
    confidence: float = 0.5
    investment_relevance: Optional[str] = None
    next_test: Optional[str] = None
    source_kind: Optional[str] = None
    metadata: dict = Field(default_factory=dict)


class MarketSetupSignalBackfillCreate(BaseModel):
    apply: bool = False
    limit: int = Field(default=500, ge=1, le=2500)
    min_confidence: float = Field(default=0.45, ge=0.0, le=1.0)
    include_conversation_turns: bool = False


class MarketSetupSignalBackfillResponse(BaseModel):
    dry_run: bool
    scanned: int
    candidates: int
    created: int
    skipped_existing: int
    skipped_no_signal: int
    skipped_quality_gate: int
    skipped_unsafe_origin: int
    examples: list[dict] = Field(default_factory=list)


class MarketSetupOutcomeAssessmentCreate(BaseModel):
    apply: bool = False
    limit: int = Field(default=5, ge=1, le=20)
    scan_limit: int = Field(default=500, ge=20, le=5000)
    min_confidence: float = Field(default=0.75, ge=0.0, le=1.0)
    grace_hours: int = Field(default=6, ge=0, le=720)
    retry_hours: int = Field(default=24, ge=1, le=720)
    research_missing_evidence: bool = False
    research_limit: int = Field(default=1, ge=0, le=5)


class MarketSetupOutcomeAssessmentResponse(BaseModel):
    scanned: int
    due: int
    eligible: int
    deferred: int
    proposed: int
    applied: int
    research_attempted: int
    research_started: int
    results: list[dict] = Field(default_factory=list)


class InvestmentObjectBackfillCreate(BaseModel):
    apply: bool = False
    scan_limit: int = Field(default=300, ge=1, le=2500)
    max_model_calls: int = Field(default=10, ge=1, le=100)
    min_confidence: float = Field(default=0.75, ge=0.0, le=1.0)
    portfolio_only: bool = True
    include_conversation_turns: bool = False
    retry_completed: bool = False
    evidence_id: Optional[UUID] = None


class InvestmentObjectBackfillResponse(BaseModel):
    dry_run: bool
    extractor_version: int
    scanned: int
    model_calls: int
    candidate_evidence: int
    metric_candidates: int
    setup_candidates: int
    metrics_created: int
    setup_created: int
    exact_duplicates_removed: int
    skipped_already_structured: int
    skipped_completed: int
    skipped_unsafe_origin: int
    skipped_unusable_text: int
    skipped_undated: int
    skipped_unresolved_subject: int
    skipped_non_portfolio: int
    skipped_quality_gate: int
    skipped_existing: int
    errors: int
    target_evidence_id: Optional[str] = None
    examples: list[dict] = Field(default_factory=list)


class MarketSetupSignalResponse(BaseModel):
    id: UUID
    signal_name: str
    signal_family: str
    subject_type: Optional[str] = None
    subject_id: Optional[UUID] = None
    entity_id: Optional[UUID] = None
    security_id: Optional[UUID] = None
    ticker: Optional[str] = None
    event_id: Optional[UUID] = None
    raw_evidence_id: Optional[UUID] = None
    source_item_id: Optional[UUID] = None
    setup_context: Optional[str] = None
    actual_context: Optional[str] = None
    price_reaction: Optional[str] = None
    value_text: Optional[str] = None
    numeric_value: Optional[float] = None
    unit: Optional[str] = None
    currency: Optional[str] = None
    period_label: Optional[str] = None
    as_of: Optional[datetime] = None
    event_time: Optional[datetime] = None
    public_time: Optional[datetime] = None
    eligible_action_time: Optional[datetime] = None
    direction: Optional[str] = None
    confidence: float
    investment_relevance: Optional[str] = None
    next_test: Optional[str] = None
    source_kind: Optional[str] = None
    outcome_status: str
    outcome_notes: Optional[str] = None
    outcome_score: Optional[float] = None
    metadata_json: dict | None = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class FundamentalMetricCreate(BaseModel):
    metric_name: str
    metric_family: Optional[str] = None
    subject_type: Optional[str] = None
    subject_id: Optional[UUID] = None
    entity_id: Optional[UUID] = None
    security_id: Optional[UUID] = None
    ticker: Optional[str] = None
    raw_evidence_id: Optional[UUID] = None
    source_item_id: Optional[UUID] = None
    value_text: Optional[str] = None
    numeric_value: Optional[float] = None
    unit: Optional[str] = None
    currency: Optional[str] = None
    period_label: Optional[str] = None
    fiscal_year: Optional[int] = None
    fiscal_quarter: Optional[str] = None
    as_of: Optional[datetime] = None
    event_time: Optional[datetime] = None
    public_time: Optional[datetime] = None
    eligible_action_time: Optional[datetime] = None
    stale_after: Optional[datetime] = None
    direction: Optional[str] = None
    confidence: float = 0.5
    investment_relevance: Optional[str] = None
    next_test: Optional[str] = None
    source_kind: Optional[str] = None
    freshness_status: Optional[str] = None
    metadata: dict = Field(default_factory=dict)


class FundamentalMetricResponse(BaseModel):
    id: UUID
    metric_name: str
    metric_family: str
    subject_type: Optional[str] = None
    subject_id: Optional[UUID] = None
    entity_id: Optional[UUID] = None
    security_id: Optional[UUID] = None
    ticker: Optional[str] = None
    raw_evidence_id: Optional[UUID] = None
    source_item_id: Optional[UUID] = None
    value_text: Optional[str] = None
    numeric_value: Optional[float] = None
    unit: Optional[str] = None
    currency: Optional[str] = None
    period_label: Optional[str] = None
    fiscal_year: Optional[int] = None
    fiscal_quarter: Optional[str] = None
    as_of: Optional[datetime] = None
    event_time: Optional[datetime] = None
    public_time: Optional[datetime] = None
    eligible_action_time: Optional[datetime] = None
    stale_after: Optional[datetime] = None
    direction: Optional[str] = None
    confidence: float
    investment_relevance: Optional[str] = None
    next_test: Optional[str] = None
    source_kind: Optional[str] = None
    freshness_status: str
    metadata_json: dict | None = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class SourceClaimAutoAssessmentResponse(BaseModel):
    id: UUID
    source_id: UUID
    claim_id: UUID
    assessment: str
    confidence: float
    rationale: str
    limitations: str
    assessment_evidence: list[UUID] = Field(default_factory=list)
    should_apply: bool
    applied: bool
    notes: Optional[str] = None
    performance_history: Optional[SourcePerformanceHistoryResponse] = None
    follow_up_evidence_count: int = 0
    recommended_research_query: Optional[str] = None
    research_followup: Optional[SourceClaimResearchFollowupResponse] = None
    assessment_attempt_count: int = 0
    last_assessment_attempt_at: Optional[datetime] = None
    next_assessment_at: Optional[datetime] = None


class SourceClaimBatchAssessmentCreate(BaseModel):
    apply: bool = True
    min_confidence: float = Field(default=0.75, ge=0.0, le=1.0)
    limit: int = Field(default=5, ge=1, le=20)
    scan_limit: int = Field(default=500, ge=1, le=5000)
    retry_hours: int = Field(default=24, ge=1, le=24 * 30)
    retry_share: float = Field(default=0.25, ge=0.0, le=1.0)
    research_missing_evidence: bool = False
    research_limit: int = Field(default=1, ge=0, le=5)


class SourceClaimBatchAssessmentResponse(BaseModel):
    scanned: int
    due: int
    eligible: int = 0
    portfolio_relevant_eligible: int = 0
    selected_portfolio_relevant: int = 0
    deferred: int = 0
    proposed: int
    applied: int
    research_attempted: int = 0
    research_started: int = 0
    results: list[SourceClaimAutoAssessmentResponse] = Field(default_factory=list)


class SourceFeedbackResponse(BaseModel):
    evidence_id: UUID
    source_id: UUID
    source_name: str
    source_type: str
    title: Optional[str] = None
    url: Optional[str] = None
    source_item_type: str
    origin_kind: str
    origin_label: str
    origin_detail: Optional[str] = None
    rating: str
    note: Optional[str] = None
    context: Optional[str] = None
    flagged_at: Optional[datetime] = None
    lesson_id: Optional[UUID] = None
    lesson_title: Optional[str] = None
    created_at: datetime
