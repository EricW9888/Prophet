import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import ARRAY, DateTime, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, utcnow


class ConclusionState(Base):
    """THE CENTRAL BELIEF OBJECT for any subject."""

    __tablename__ = "conclusion_states"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    subject_type: Mapped[str] = mapped_column(
        String, index=True
    )  # position|entity|theme|benchmark|event
    subject_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)

    current_thesis_summary: Mapped[str] = mapped_column(String)
    current_stance: Mapped[str] = mapped_column(
        String
    )  # bullish|bearish|neutral|uncertain|no_view
    confidence_band: Mapped[str] = mapped_column(
        String
    )  # very_low|low|medium|high|very_high

    dominant_channel_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("impact_channels.id"), nullable=True
    )

    key_supporting_evidence_ids: Mapped[Optional[list[uuid.UUID]]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), nullable=True
    )
    key_contradicting_evidence_ids: Mapped[Optional[list[uuid.UUID]]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), nullable=True
    )
    what_would_falsify: Mapped[Optional[list[str]]] = mapped_column(
        ARRAY(String), nullable=True
    )
    what_would_strengthen: Mapped[Optional[list[str]]] = mapped_column(
        ARRAY(String), nullable=True
    )

    last_updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    last_verified_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    update_count: Mapped[int] = mapped_column(Integer, default=0)
    reasoning_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("reasoning_runs.id"))


class ConclusionRevision(Base):
    """Logged every time a ConclusionState changes."""

    __tablename__ = "conclusion_revisions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    conclusion_state_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conclusion_states.id"), index=True
    )

    previous_stance: Mapped[str] = mapped_column(String)
    new_stance: Mapped[str] = mapped_column(String)
    previous_confidence: Mapped[str] = mapped_column(String)
    new_confidence: Mapped[str] = mapped_column(String)

    trigger_evidence_ids: Mapped[Optional[list[uuid.UUID]]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), nullable=True
    )
    revision_reasoning: Mapped[str] = mapped_column(String)

    reasoning_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("reasoning_runs.id"))
    revised_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
