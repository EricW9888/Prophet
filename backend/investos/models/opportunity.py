import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, utcnow


class OpportunityUniverseMember(Base):
    """An operator-selected security that discovery may inspect without owning."""

    __tablename__ = "opportunity_universe_members"
    __table_args__ = (
        UniqueConstraint(
            "security_id",
            name="uq_opportunity_universe_members_security",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    security_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("securities.id"), index=True
    )
    entity_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("entities.id"), index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    priority: Mapped[float] = mapped_column(Float, default=0.5)
    source: Mapped[str] = mapped_column(String, default="manual", index=True)
    metadata_json: Mapped[dict] = mapped_column(JSONB, default=dict)
    last_inspected_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    next_inspection_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class OpportunityDiscoveryRun(Base):
    """Durable coverage and cost boundary for one resumable discovery pass."""

    __tablename__ = "opportunity_discovery_runs"
    __table_args__ = (
        UniqueConstraint(
            "active_key",
            name="uq_opportunity_discovery_runs_active_key",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    status: Mapped[str] = mapped_column(String, default="running", index=True)
    active_key: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    owner_token: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    heartbeat_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    universe_size: Mapped[int] = mapped_column(Integer, default=0)
    planned_count: Mapped[int] = mapped_column(Integer, default=0)
    inspected_count: Mapped[int] = mapped_column(Integer, default=0)
    skipped_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    estimated_credits: Mapped[int] = mapped_column(Integer, default=0)
    remaining_member_ids_json: Mapped[list] = mapped_column(JSONB, default=list)
    inspected_member_ids_json: Mapped[list] = mapped_column(JSONB, default=list)
    skipped_json: Mapped[list] = mapped_column(JSONB, default=list)
    failures_json: Mapped[list] = mapped_column(JSONB, default=list)
    provider_attempts_json: Mapped[list] = mapped_column(JSONB, default=list)
    limits_json: Mapped[dict] = mapped_column(JSONB, default=dict)
    detail: Mapped[Optional[str]] = mapped_column(String, nullable=True)


class OpportunityCandidate(Base):
    """Reviewable, source-linked hypothesis; never an accepted view or holding."""

    __tablename__ = "opportunity_candidates"
    __table_args__ = (
        UniqueConstraint(
            "fingerprint",
            name="uq_opportunity_candidates_fingerprint",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("opportunity_discovery_runs.id"), index=True
    )
    entity_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("entities.id"), index=True)
    security_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("securities.id"), index=True
    )
    shadow_experiment_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("shadow_experiments.id"), nullable=True, index=True
    )
    ticker: Mapped[str] = mapped_column(String, index=True)
    fingerprint: Mapped[str] = mapped_column(String, index=True)
    status: Mapped[str] = mapped_column(String, default="new", index=True)
    title: Mapped[str] = mapped_column(String)
    family_key: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    priority_score: Mapped[float] = mapped_column(Float, default=0.0, index=True)
    signal_stage: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    why_now: Mapped[str] = mapped_column(String)
    investable_thesis: Mapped[str] = mapped_column(String)
    portfolio_transmission: Mapped[str] = mapped_column(String)
    expected_edge: Mapped[str] = mapped_column(String)
    falsification_tests_json: Mapped[list] = mapped_column(JSONB, default=list)
    assumptions_json: Mapped[list] = mapped_column(JSONB, default=list)
    uncertainties_json: Mapped[list] = mapped_column(JSONB, default=list)
    evidence_refs_json: Mapped[list] = mapped_column(JSONB, default=list)
    evidence_snapshot_json: Mapped[list] = mapped_column(JSONB, default=list)
    ranking_json: Mapped[dict] = mapped_column(JSONB, default=dict)
    discovery_profile_json: Mapped[dict] = mapped_column(JSONB, default=dict)
    review_reason: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class OpportunityCandidateObservation(Base):
    """Immutable discovery-time hypothesis with a later point-in-time outcome."""

    __tablename__ = "opportunity_candidate_observations"
    __table_args__ = (
        UniqueConstraint(
            "candidate_id",
            "run_id",
            name="uq_opportunity_candidate_observations_candidate_run",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    candidate_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("opportunity_candidates.id", ondelete="CASCADE"), index=True
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("opportunity_discovery_runs.id"), index=True
    )
    security_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("securities.id"), index=True
    )
    ticker: Mapped[str] = mapped_column(String, index=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    horizon_label: Mapped[str] = mapped_column(String)
    horizon_days: Mapped[int] = mapped_column(Integer)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    expected_relative_direction: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="pending", index=True)
    profile_snapshot_json: Mapped[dict] = mapped_column(JSONB, default=dict)
    evidence_refs_json: Mapped[list] = mapped_column(JSONB, default=list)
    evidence_snapshot_json: Mapped[list] = mapped_column(JSONB, default=list)
    benchmark_ticker: Mapped[str] = mapped_column(String)
    market_data_provider: Mapped[str] = mapped_column(String)
    candidate_start_time: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    candidate_start_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    benchmark_start_time: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    benchmark_start_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    evaluated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    candidate_end_time: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    candidate_end_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    benchmark_end_time: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    benchmark_end_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    candidate_return_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    benchmark_return_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    excess_return_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    cash_return_pct: Mapped[float] = mapped_column(Float, default=0.0)
    result_label: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    last_attempt_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    evaluation_policy_json: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
