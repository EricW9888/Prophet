import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, TimestampMixin, utcnow


class RawEvidence(TimestampMixin, Base):
    __tablename__ = "raw_evidence"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    source_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sources.id"), index=True)
    source_item_type: Mapped[str] = mapped_column(String)
    external_id: Mapped[Optional[str]] = mapped_column(
        String, nullable=True, index=True
    )

    title: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    raw_content_ref: Mapped[Optional[str]] = mapped_column(
        String, nullable=True
    )  # object-store key
    content_hash: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    url: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    author: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    metadata_json: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    is_processed: Mapped[bool] = mapped_column(Boolean, default=False)


class SourceItem(Base):
    """The processed verison of RawEvidence"""

    __tablename__ = "source_items"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    raw_evidence_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("raw_evidence.id"), unique=True
    )
    source_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sources.id"), index=True)

    extracted_text: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    summary: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    processing_status: Mapped[str] = mapped_column(String, default="pending")


class ResearchDiscoveryObservation(Base):
    """A provisional search observation, separate from attributable evidence."""

    __tablename__ = "research_discovery_observations"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    provider: Mapped[str] = mapped_column(String, index=True)
    request_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    query: Mapped[str] = mapped_column(String, index=True)
    effective_query: Mapped[str] = mapped_column(String)
    search_title: Mapped[str] = mapped_column(String)
    result_rank: Mapped[int] = mapped_column(Integer)
    result_title: Mapped[str] = mapped_column(String)
    url: Mapped[str] = mapped_column(String, index=True)
    snippet: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    content_kind: Mapped[str] = mapped_column(String)
    outcome: Mapped[str] = mapped_column(String, index=True)
    evidence_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("raw_evidence.id"), nullable=True, index=True
    )
    subject_type: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    subject_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    subject_name: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    error: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    metadata_json: Mapped[dict] = mapped_column(JSONB, default=dict)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
