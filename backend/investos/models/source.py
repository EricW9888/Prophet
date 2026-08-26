import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Numeric, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, utcnow


class Source(Base):
    __tablename__ = "sources"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String, index=True)
    source_type: Mapped[str] = mapped_column(
        String, index=True
    )  # x_account|youtube|email|filing|news|manual|web_research|analyst|official|peer_research|ownership_tracker
    url: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    description: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    is_trusted: Mapped[bool] = mapped_column(Boolean, default=False)
    trust_origin: Mapped[str] = mapped_column(
        String, default="discovered", server_default="discovered"
    )
    trust_review_status: Mapped[str] = mapped_column(
        String, default="current", server_default="current"
    )
    trust_review_reason: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    trust_reviewed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    def apply_operator_trust(self, trusted: bool) -> None:
        self.is_trusted = trusted
        self.trust_origin = "operator"
        self.trust_review_status = "current"
        self.trust_review_reason = None
        self.trust_reviewed_at = utcnow()

    def apply_learned_trust(self, trusted: bool, *, reason: str) -> None:
        """Apply learned trust without silently replacing an operator decision."""

        self.trust_reviewed_at = utcnow()
        if self.trust_origin == "operator":
            if self.is_trusted == trusted:
                self.trust_review_status = "current"
                self.trust_review_reason = None
            else:
                self.trust_review_status = "change_recommended"
                self.trust_review_reason = reason
            return

        self.is_trusted = trusted
        self.trust_origin = "learned"
        self.trust_review_status = "current"
        self.trust_review_reason = None

    quality_segments: Mapped[list["SourceQualitySegment"]] = relationship(
        "SourceQualitySegment", back_populates="source"
    )


class SourceQualitySegment(Base):
    __tablename__ = "source_quality_segments"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    source_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sources.id"), index=True)

    domain: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    sector: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    ticker: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    horizon: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    regime: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    event_type: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    quality_score: Mapped[float] = mapped_column(Numeric, default=0.0)
    originality_score: Mapped[float] = mapped_column(Numeric, default=0.0)
    timing_usefulness: Mapped[float] = mapped_column(Numeric, default=0.0)

    notes: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    last_evaluated: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    evidence_count: Mapped[int] = mapped_column(Numeric, default=0)

    source: Mapped["Source"] = relationship("Source", back_populates="quality_segments")
