import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, utcnow


class EvidenceImportanceScore(Base):
    """Composite score computed from tier fields.
    Used for retrieval ranking and evidence packet assembly."""

    __tablename__ = "evidence_importance_scores"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    evidence_type: Mapped[str] = mapped_column(String)  # fact|claim
    evidence_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)

    tier: Mapped[str] = mapped_column(String)
    importance: Mapped[str] = mapped_column(String)
    directness: Mapped[str] = mapped_column(String)
    novelty: Mapped[str] = mapped_column(String)

    composite_score: Mapped[float] = mapped_column(Float, index=True)
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class EvidencePromotionPolicy(Base):
    """Hard rules for when evidence can upgrade a conclusion."""

    __tablename__ = "evidence_promotion_policies"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String, unique=True)

    min_tier_for_conclusion_change: Mapped[str] = mapped_column(String)
    min_supporting_items: Mapped[int] = mapped_column(Integer, default=1)
    min_distinct_sources: Mapped[int] = mapped_column(Integer, default=1)

    repeated_weak_equals_strong: Mapped[bool] = mapped_column(Boolean, default=False)
    require_contradiction_check: Mapped[bool] = mapped_column(Boolean, default=True)
    require_benchmark_confounder_check: Mapped[bool] = mapped_column(
        Boolean, default=True
    )
    require_macro_confounder_check: Mapped[bool] = mapped_column(Boolean, default=True)

    tier_weights_json: Mapped[dict] = mapped_column(JSONB)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class RetrievalBudget(Base):
    """Bounds on retrieval expansion to prevent context explosion."""

    __tablename__ = "retrieval_budgets"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    run_type: Mapped[str] = mapped_column(String, unique=True, index=True)

    max_direct_items: Mapped[int] = mapped_column(Integer)
    max_connected_items: Mapped[int] = mapped_column(Integer)
    max_historical_items: Mapped[int] = mapped_column(Integer)
    max_contradiction_items: Mapped[int] = mapped_column(Integer)

    max_total_items: Mapped[int] = mapped_column(Integer)
    max_total_tokens: Mapped[int] = mapped_column(Integer)

    expansion_allowed: Mapped[bool] = mapped_column(Boolean, default=False)
    max_expansion_rounds: Mapped[int] = mapped_column(Integer, default=0)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class ResearchTriggerPolicy(Base):
    """Encodes conditions for broader research as weighted policy."""

    __tablename__ = "research_trigger_policies"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String, unique=True)

    coverage_thin_weight: Mapped[float] = mapped_column(Float)
    contradiction_high_weight: Mapped[float] = mapped_column(Float)
    stake_large_weight: Mapped[float] = mapped_column(Float)
    source_trust_weak_weight: Mapped[float] = mapped_column(Float)
    event_ambiguity_high_weight: Mapped[float] = mapped_column(Float)
    benchmark_confounder_weight: Mapped[float] = mapped_column(Float)

    trigger_threshold: Mapped[float] = mapped_column(Float)
    max_research_budget_usd: Mapped[float] = mapped_column(Float)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
