import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    ARRAY,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, utcnow


class Lesson(Base):
    """Extracted generalized lesson to improve future reasoning."""

    __tablename__ = "lessons"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    title: Mapped[str] = mapped_column(String)
    summary: Mapped[str] = mapped_column(String)

    lesson_type: Mapped[str] = mapped_column(
        String
    )  # bias|market_mechanic|source_reliability|analytical_error

    applicable_sectors: Mapped[Optional[list[str]]] = mapped_column(
        ARRAY(String), nullable=True
    )
    applicable_regimes: Mapped[Optional[list[str]]] = mapped_column(
        ARRAY(String), nullable=True
    )

    originating_decision_review_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )  # Will FK to DecisionReview
    originating_experiment_result_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )  # Will FK to ExperimentResult
    experiment_family_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("experiment_family_states.id"), nullable=True, index=True
    )

    maturity_status: Mapped[str] = mapped_column(String, default="active", index=True)
    confidence_score: Mapped[float] = mapped_column(Float, default=0.0)
    supporting_observations: Mapped[int] = mapped_column(Integer, default=0)
    contradicting_observations: Mapped[int] = mapped_column(Integer, default=0)
    neutral_observations: Mapped[int] = mapped_column(Integer, default=0)
    last_validated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    stale_after: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    metadata_json: Mapped[dict] = mapped_column(JSONB, default=dict)

    usage_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class LessonObservation(Base):
    """Immutable outcome evidence supporting or challenging a learned lesson."""

    __tablename__ = "lesson_observations"
    __table_args__ = (
        UniqueConstraint(
            "lesson_id",
            "experiment_result_id",
            name="uq_lesson_observation_result",
        ),
        UniqueConstraint(
            "experiment_result_id",
            name="lesson_observations_experiment_result_id_key",
        ),
        Index(
            "ix_lesson_observations_experiment_result_id",
            "experiment_result_id",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    lesson_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("lessons.id"), index=True)
    experiment_result_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("experiment_results.id")
    )
    relationship: Mapped[str] = mapped_column(String)
    observed_alpha: Mapped[float] = mapped_column(Float)
    rationale: Mapped[str] = mapped_column(String)
    evidence_json: Mapped[dict] = mapped_column(JSONB, default=dict)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
