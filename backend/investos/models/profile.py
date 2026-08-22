import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import ARRAY, Boolean, DateTime, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, utcnow


class Profile(Base):
    """Persistent memory/narrative of an entity or theme."""

    __tablename__ = "dossiers"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    subject_type: Mapped[str] = mapped_column(String, index=True)
    subject_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)

    # Text narrative sections
    executive_summary: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    business_model: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    bull_case: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    bear_case: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    key_drivers: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    competitor_landscape: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    # Active contradictions tracking
    active_contradictions: Mapped[Optional[list[str]]] = mapped_column(
        ARRAY(String), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    # Autonomous discovery fields
    is_autonomous: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )
    review_status: Mapped[Optional[str]] = mapped_column(
        String, nullable=True
    )  # pending|approved|dismissed
    review_reason: Mapped[Optional[str]] = mapped_column(
        String, nullable=True
    )  # rationale for autonomous discovery

    # Transparency for autonomous strategist
    strategist_reasoning: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    source_rationale: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    version: Mapped[int] = mapped_column(Integer, default=1)


class ProfileSnapshot(Base):
    """Historical point-in-time snapshot of a profile."""

    __tablename__ = "dossier_snapshots"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    profile_id: Mapped[uuid.UUID] = mapped_column(
        "dossier_id", ForeignKey("dossiers.id"), index=True
    )

    executive_summary: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    snapshot_reason: Mapped[str] = mapped_column(
        String
    )  # periodic|major_event|thesis_change
    snapshot_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class ProfileDelta(Base):
    """Record of updates applied to a profile during an ingestion cycle."""

    __tablename__ = "dossier_deltas"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    profile_id: Mapped[uuid.UUID] = mapped_column(
        "dossier_id", ForeignKey("dossiers.id"), index=True
    )

    evidence_ids: Mapped[Optional[list[uuid.UUID]]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), nullable=True
    )
    reasoning_run_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("reasoning_runs.id"), nullable=True
    )

    summary_of_changes: Mapped[str] = mapped_column(String)
    sections_modified: Mapped[Optional[list[str]]] = mapped_column(
        ARRAY(String), nullable=True
    )

    applied_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
