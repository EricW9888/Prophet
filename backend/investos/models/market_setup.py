import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Numeric, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin


class MarketSetupSignal(TimestampMixin, Base):
    """Source-dated market setup, expectation, positioning, and metric signals."""

    __tablename__ = "market_setup_signals"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    subject_type: Mapped[Optional[str]] = mapped_column(
        String, nullable=True, index=True
    )
    subject_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True, index=True
    )
    entity_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("entities.id"), nullable=True, index=True
    )
    security_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("securities.id"), nullable=True, index=True
    )
    ticker: Mapped[Optional[str]] = mapped_column(String, nullable=True, index=True)
    event_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("events.id"), nullable=True, index=True
    )
    raw_evidence_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("raw_evidence.id"), nullable=True, index=True
    )
    source_item_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("source_items.id"), nullable=True, index=True
    )

    signal_name: Mapped[str] = mapped_column(String, index=True)
    signal_family: Mapped[str] = mapped_column(String, index=True)
    setup_context: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    actual_context: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    price_reaction: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    value_text: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    numeric_value: Mapped[Optional[float]] = mapped_column(Numeric, nullable=True)
    unit: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    currency: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    period_label: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    as_of: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    direction: Mapped[Optional[str]] = mapped_column(String, nullable=True, index=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    investment_relevance: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    next_test: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    source_kind: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    outcome_status: Mapped[str] = mapped_column(String, default="unscored", index=True)
    outcome_notes: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    outcome_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    is_deprecated: Mapped[bool] = mapped_column(Boolean, default=False)
    deprecated_reason: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    metadata_json: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
