import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import ARRAY, Boolean, DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, utcnow


class VerificationRun(Base):
    """Structured response to 'are you sure?' / challenge prompts."""

    __tablename__ = "verification_runs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    conclusion_state_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conclusion_states.id"), index=True
    )

    trigger: Mapped[str] = mapped_column(
        String
    )  # user_challenge|automated_review|contradiction_spike|coverage_gap_detected|scheduled
    evidence_packet_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("evidence_packets.id")
    )

    higher_tier_evidence_checked: Mapped[Optional[list[uuid.UUID]]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), nullable=True
    )
    contradiction_coverage_status: Mapped[str] = mapped_column(String)
    missing_classes_found: Mapped[Optional[list[str]]] = mapped_column(
        ARRAY(String), nullable=True
    )

    prior_stance: Mapped[str] = mapped_column(String)
    verified_stance: Mapped[str] = mapped_column(String)
    conclusion_changed: Mapped[bool] = mapped_column(Boolean, default=False)

    change_reasoning: Mapped[str] = mapped_column(String)
    conclusion_revision_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("conclusion_revisions.id"), nullable=True
    )
    reasoning_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("reasoning_runs.id"))

    verified_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
