from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field


class OpportunityUniverseMemberCreate(BaseModel):
    ticker: str = Field(min_length=1, max_length=32)
    entity_name: str | None = Field(default=None, max_length=240)
    priority: float = Field(default=0.5, ge=0.0, le=1.0)
    enabled: bool = True


class OpportunityUniverseMemberUpdate(BaseModel):
    priority: float | None = Field(default=None, ge=0.0, le=1.0)
    enabled: bool | None = None


class OpportunityUniverseMemberResponse(BaseModel):
    id: UUID
    security_id: UUID
    entity_id: UUID
    ticker: str
    entity_name: str
    enabled: bool
    priority: float
    source: str
    last_inspected_at: datetime | None
    next_inspection_at: datetime | None
    created_at: datetime
    updated_at: datetime


class OpportunityDiscoveryRunResponse(BaseModel):
    id: UUID
    status: str
    captured_at: datetime
    started_at: datetime
    completed_at: datetime | None
    universe_size: int
    planned_count: int
    inspected_count: int
    skipped_count: int
    failed_count: int
    estimated_credits: int
    remaining_member_ids: list[str]
    inspected_member_ids: list[str]
    skipped: list[dict]
    failures: list[dict]
    provider_attempts: list[dict]
    limits: dict
    detail: str | None


class OpportunityCandidateObservationResponse(BaseModel):
    id: UUID
    run_id: UUID
    captured_at: datetime
    horizon_label: str
    horizon_days: int
    due_at: datetime
    expected_relative_direction: str
    status: str
    profile_snapshot: dict
    evidence_refs: list[str]
    evidence_snapshot: list[dict]
    benchmark_ticker: str
    market_data_provider: str
    candidate_start_time: datetime | None
    candidate_start_price: float | None
    benchmark_start_time: datetime | None
    benchmark_start_price: float | None
    evaluated_at: datetime | None
    candidate_end_time: datetime | None
    candidate_end_price: float | None
    benchmark_end_time: datetime | None
    benchmark_end_price: float | None
    candidate_return_pct: float | None
    benchmark_return_pct: float | None
    excess_return_pct: float | None
    cash_return_pct: float
    result_label: str | None
    attempt_count: int
    last_attempt_at: datetime | None
    last_error: str | None
    evaluation_policy: dict


class OpportunityCandidateResponse(BaseModel):
    id: UUID
    run_id: UUID
    entity_id: UUID
    security_id: UUID
    shadow_experiment_id: UUID | None
    ticker: str
    status: str
    title: str
    family_key: str | None
    priority_score: float
    signal_stage: str | None
    why_now: str
    investable_thesis: str
    portfolio_transmission: str
    expected_edge: str
    falsification_tests: list[str]
    assumptions: list[str]
    uncertainties: list[str]
    evidence_refs: list[str]
    evidence_snapshot: list[dict]
    ranking: dict
    review_reason: str | None
    captured_at: datetime
    first_seen_at: datetime
    last_seen_at: datetime
    expires_at: datetime
    observations: list[OpportunityCandidateObservationResponse] = Field(
        default_factory=list
    )


class OpportunityCandidateReview(BaseModel):
    status: Literal["new", "monitoring", "rejected", "expired"]
    reason: str | None = Field(default=None, max_length=1200)


class OpportunityShadowTestRequest(BaseModel):
    account_basis: Literal["clone_portfolio", "cash_only"] = "clone_portfolio"
    starting_cash: float | None = Field(default=None, gt=0)
