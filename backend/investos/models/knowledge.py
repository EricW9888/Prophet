import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Numeric, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin


class Event(TimestampMixin, Base):
    __tablename__ = "events"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    title: Mapped[str] = mapped_column(String, index=True)
    description: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    event_type: Mapped[str] = mapped_column(String, index=True)

    is_evolving: Mapped[bool] = mapped_column(Boolean, default=False)
    parent_event_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("events.id"), nullable=True
    )
    historical_episode_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("historical_episodes.id"), nullable=True
    )
    is_deprecated: Mapped[bool] = mapped_column(Boolean, default=False)
    deprecated_reason: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    # Horizon columns
    target_horizon: Mapped[str] = mapped_column(
        String, default="strategic", index=True
    )  # tactical|strategic|visionary
    horizon_reasoning: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    valid_until: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class Fact(TimestampMixin, Base):
    __tablename__ = "facts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    statement: Mapped[str] = mapped_column(String)
    fact_type: Mapped[str] = mapped_column(String, index=True)
    confidence: Mapped[float] = mapped_column(Numeric)

    source_item_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("source_items.id"), index=True
    )
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    superseded_by_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("facts.id"), nullable=True
    )

    # Epistemic tier columns
    tier: Mapped[str] = mapped_column(
        String, index=True
    )  # hard_fact|strong_derived|credible_interpretation|weak_signal|rumor|speculative_commentary
    importance: Mapped[str] = mapped_column(String)  # critical|high|medium|low|trivial
    directness: Mapped[str] = mapped_column(String)  # primary|secondary|tertiary
    novelty: Mapped[str] = mapped_column(String)  # breaking|confirming|redundant|stale
    contradiction_role: Mapped[str] = mapped_column(
        String
    )  # supports_consensus|contradicts_consensus|neutral|ambiguous
    promotion_eligible: Mapped[bool] = mapped_column(Boolean, default=False)
    is_deprecated: Mapped[bool] = mapped_column(Boolean, default=False)
    deprecated_reason: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    # Horizon columns
    target_horizon: Mapped[str] = mapped_column(
        String, default="strategic", index=True
    )  # tactical|strategic|visionary
    horizon_reasoning: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    valid_until: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class Claim(TimestampMixin, Base):
    __tablename__ = "claims"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    statement: Mapped[str] = mapped_column(String)
    claim_type: Mapped[str] = mapped_column(String, index=True)
    claimant: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    confidence: Mapped[float] = mapped_column(Numeric)
    sentiment: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    source_item_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("source_items.id"), index=True
    )
    is_original: Mapped[bool] = mapped_column(Boolean, default=False)
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    superseded_by_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("claims.id"), nullable=True
    )

    # Epistemic tier columns
    tier: Mapped[str] = mapped_column(String, index=True)
    importance: Mapped[str] = mapped_column(String)
    directness: Mapped[str] = mapped_column(String)
    novelty: Mapped[str] = mapped_column(String)
    contradiction_role: Mapped[str] = mapped_column(String)
    promotion_eligible: Mapped[bool] = mapped_column(Boolean, default=False)
    is_deprecated: Mapped[bool] = mapped_column(Boolean, default=False)
    deprecated_reason: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    # Horizon columns
    target_horizon: Mapped[str] = mapped_column(
        String, default="strategic", index=True
    )  # tactical|strategic|visionary
    horizon_reasoning: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    valid_until: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
