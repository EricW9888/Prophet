import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import ARRAY, DateTime, Float, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, utcnow


class Implication(Base):
    """Stored result of implication analysis for an event/fact."""

    __tablename__ = "implications"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    trigger_event_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("events.id"), nullable=True
    )
    trigger_fact_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("facts.id"), nullable=True
    )

    title: Mapped[str] = mapped_column(String)
    summary: Mapped[str] = mapped_column(String)

    reasoning_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("reasoning_runs.id"))

    overall_confidence: Mapped[float] = mapped_column(Float)
    overall_time_horizon: Mapped[str] = mapped_column(String)
    already_priced_in_assessment: Mapped[str] = mapped_column(String)

    affected_positions: Mapped[Optional[list[uuid.UUID]]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), nullable=True
    )
    affected_watchlist: Mapped[Optional[list[uuid.UUID]]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), nullable=True
    )
    affected_themes: Mapped[Optional[list[uuid.UUID]]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), nullable=True
    )
    affected_benchmarks: Mapped[Optional[list[uuid.UUID]]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class ImpactChannel(Base):
    """One causal channel within an implication."""

    __tablename__ = "impact_channels"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    implication_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("implications.id"), index=True
    )

    channel_name: Mapped[str] = mapped_column(
        String
    )  # e.g. "commodity price", "inflation expectations"
    direction: Mapped[str] = mapped_column(
        String
    )  # positive|negative|neutral|uncertain
    magnitude: Mapped[str] = mapped_column(String)  # low|medium|high
    confidence: Mapped[float] = mapped_column(Float)
    time_horizon: Mapped[str] = mapped_column(
        String
    )  # intraday|multi_day|multi_week|multi_month|multi_year

    reasoning: Mapped[str] = mapped_column(String)
    order_num: Mapped[str] = mapped_column(
        String
    )  # first|second|third (since 'order' is reserved word)

    competing_with_channel_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("impact_channels.id"), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class ChannelAssessment(Base):
    """Assessment of which channel is dominant and why."""

    __tablename__ = "channel_assessments"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    implication_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("implications.id"), index=True
    )

    dominant_channel_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("impact_channels.id")
    )

    reasoning: Mapped[str] = mapped_column(String)
    confidence: Mapped[float] = mapped_column(Float)

    distinguishing_evidence_needed: Mapped[Optional[str]] = mapped_column(
        String, nullable=True
    )
    assessed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class PricedInAssessment(Base):
    """Explicit assessment of whether/how much is already priced in."""

    __tablename__ = "priced_in_assessments"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    implication_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("implications.id"), index=True
    )
    security_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("securities.id"), nullable=True
    )

    assessment: Mapped[str] = mapped_column(
        String
    )  # not_priced|partially_priced|fully_priced|overreaction
    confidence: Mapped[float] = mapped_column(Float)
    reasoning: Mapped[str] = mapped_column(String)

    price_move_observed: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    price_move_expected: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    assessed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
