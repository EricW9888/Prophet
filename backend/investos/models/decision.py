import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import ARRAY, Boolean, DateTime, Float, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, utcnow


class DecisionJournal(Base):
    """Frozen record of 'why I did this right now'."""

    __tablename__ = "decision_journals"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    position_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("positions.id"), index=True, nullable=True
    )

    decision_type: Mapped[str] = mapped_column(
        String, index=True
    )  # enter|exit|trim|add|hold_through_earnings|pass

    rationale: Mapped[str] = mapped_column(String)
    expected_catalyst_timeframe: Mapped[Optional[str]] = mapped_column(
        String, nullable=True
    )
    expected_return: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    conclusion_state_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("conclusion_states.id"), nullable=True
    )
    evidence_packet_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("evidence_packets.id"), nullable=True
    )
    profile_version_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )

    transaction_ids: Mapped[Optional[list[uuid.UUID]]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class DecisionReview(Base):
    """Subsequent autopsy of a decision after the fact."""

    __tablename__ = "decision_reviews"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    decision_journal_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("decision_journals.id"), index=True
    )

    outcome_assessment: Mapped[str] = mapped_column(
        String
    )  # correct_for_right_reason|correct_for_wrong_reason|wrong_for_right_reason|wrong_for_wrong_reason

    actual_return: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    mistake_preventable: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)

    what_went_right: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    what_went_wrong: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    what_to_improve: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    reasoning_run_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("reasoning_runs.id"), nullable=True
    )

    extracted_lesson_ids: Mapped[Optional[list[uuid.UUID]]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), nullable=True
    )

    reviewed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
