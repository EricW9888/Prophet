import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    ARRAY,
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


class CoverageMap(Base):
    """Tracks what the system knows and doesn't know about a subject."""

    __tablename__ = "coverage_maps"
    __table_args__ = (
        UniqueConstraint(
            "subject_type",
            "subject_id",
            name="uq_coverage_maps_subject",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    subject_type: Mapped[str] = mapped_column(String, index=True)
    subject_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)

    total_evidence_count: Mapped[int] = mapped_column(Integer, default=0)
    high_tier_evidence_count: Mapped[int] = mapped_column(Integer, default=0)

    contradiction_count: Mapped[int] = mapped_column(Integer, default=0)
    unresolved_contradiction_count: Mapped[int] = mapped_column(Integer, default=0)

    evidence_class_coverage_json: Mapped[dict] = mapped_column(
        JSONB
    )  # {"official_filing": true, "peer_analysis": false}

    overall_coverage_score: Mapped[float] = mapped_column(Float, default=0.0)  # 0-100

    last_computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class MissingEvidenceClass(Base):
    """Explicitly identified gap in knowledge (e.g., 'no recent management commentary')."""

    __tablename__ = "missing_evidence_classes"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    coverage_map_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("coverage_maps.id"), index=True
    )

    class_name: Mapped[str] = mapped_column(String)
    importance_to_thesis: Mapped[str] = mapped_column(
        String
    )  # critical|high|medium|low
    identified_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    resolved_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class UnresolvedQuestion(Base):
    """Specific question the system knows it needs an answer to."""

    __tablename__ = "unresolved_questions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    coverage_map_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("coverage_maps.id"), index=True
    )

    question_text: Mapped[str] = mapped_column(String)
    urgency: Mapped[int] = mapped_column(Integer, default=1)  # 1-5

    originating_evidence_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )

    status: Mapped[str] = mapped_column(
        String, default="open"
    )  # open|investigating|answered|obsolete
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class Resolution(Base):
    """When a question is answered or a class gap is filled."""

    __tablename__ = "resolutions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    unresolved_question_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("unresolved_questions.id"), nullable=True
    )
    missing_evidence_class_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("missing_evidence_classes.id"), nullable=True
    )

    resolving_evidence_ids: Mapped[Optional[list[uuid.UUID]]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), nullable=True
    )
    reasoning_run_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("reasoning_runs.id"), nullable=True
    )

    summary: Mapped[str] = mapped_column(String)
    resolved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
