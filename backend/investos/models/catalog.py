import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import ARRAY, Boolean, DateTime, Float, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, utcnow


class HistoricalEpisode(Base):
    __tablename__ = "historical_episodes"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String, index=True)
    description: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    episode_type: Mapped[str] = mapped_column(String, index=True)

    start_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end_time: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    affected_sectors: Mapped[Optional[list[str]]] = mapped_column(
        ARRAY(String), nullable=True
    )
    affected_themes: Mapped[Optional[list[str]]] = mapped_column(
        ARRAY(String), nullable=True
    )
    dominant_channel: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    notes: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class SourceProfile(Base):
    __tablename__ = "source_profiles"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    source_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sources.id"), unique=True, index=True
    )

    specialization_domains: Mapped[Optional[list[str]]] = mapped_column(
        ARRAY(String), nullable=True
    )
    known_weaknesses: Mapped[Optional[list[str]]] = mapped_column(
        ARRAY(String), nullable=True
    )
    trust_trajectory: Mapped[str] = mapped_column(
        String
    )  # improving|stable|degrading|compromised

    first_tracked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    total_claims_tracked: Mapped[int] = mapped_column(Integer, default=0)
    active_since: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    notes: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    last_reviewed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class SourceTrustProfile(Base):
    __tablename__ = "source_trust_profiles"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    source_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sources.id"), unique=True, index=True
    )

    factual_reliability: Mapped[str] = mapped_column(
        String
    )  # very_low|low|medium|high|very_high
    calibration: Mapped[str] = mapped_column(
        String
    )  # under_confident|calibrated|over_confident
    correction_quality: Mapped[str] = mapped_column(
        String
    )  # never_corrects|slow_corrects|fast_corrects
    noise_ratio: Mapped[str] = mapped_column(
        String
    )  # very_noisy|noisy|moderate|clean|very_clean
    trust_trajectory: Mapped[str] = mapped_column(
        String
    )  # improving|stable|degrading|compromised

    last_evaluated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class SourceValueProfile(Base):
    __tablename__ = "source_value_profiles"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    source_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sources.id"), unique=True, index=True
    )

    idea_generation_value: Mapped[str] = mapped_column(String)  # none|low|medium|high
    timing_value: Mapped[str] = mapped_column(String)  # none|low|medium|high
    portfolio_relevance_value: Mapped[str] = mapped_column(
        String
    )  # none|low|medium|high
    specificity: Mapped[str] = mapped_column(
        String
    )  # vague|moderate|specific|very_specific
    originality: Mapped[str] = mapped_column(
        String
    )  # repeater|occasional_original|primary_source

    best_domains: Mapped[Optional[list[str]]] = mapped_column(
        ARRAY(String), nullable=True
    )
    last_evaluated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class SourceClaimRecord(Base):
    __tablename__ = "source_claim_records"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    source_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sources.id"), index=True)
    claim_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("claims.id"), index=True)

    domain: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    sector: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    ticker: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    regime: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    claim_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    assessment: Mapped[str] = mapped_column(
        String
    )  # pending|correct|incorrect|partially_correct|indeterminate
    assessment_time: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    assessment_attempt_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0"
    )
    last_assessment_attempt_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    next_assessment_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    assessment_metadata: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)

    assessment_evidence: Mapped[Optional[list[uuid.UUID]]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), nullable=True
    )
    horizon_days: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(String, nullable=True)


class SourcePerformanceHistory(Base):
    __tablename__ = "source_performance_history"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    source_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sources.id"), index=True)

    domain: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    sector: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    regime: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    total_claims: Mapped[int] = mapped_column(Integer, default=0)
    correct_claims: Mapped[int] = mapped_column(Integer, default=0)
    incorrect_claims: Mapped[int] = mapped_column(Integer, default=0)

    accuracy_rate: Mapped[float] = mapped_column(Float, default=0.0)
    originality_rate: Mapped[float] = mapped_column(Float, default=0.0)
    timing_score: Mapped[float] = mapped_column(Float, default=0.0)

    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
