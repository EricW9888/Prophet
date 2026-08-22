import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, utcnow


class ActiveWatcher(Base):
    """
    Decisive monitoring for specific catalysts or price levels.
    Enables 'closed-loop' adjustment where the agent checks back on its own predictions.
    """

    __tablename__ = "active_watchers"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    # Context
    source: Mapped[str] = mapped_column(
        String
    )  # chat|shadow_experiment|autonomous_discovery
    source_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )

    # Target
    ticker: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    entity_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("entities.id"), nullable=True
    )

    # Condition
    condition_type: Mapped[str] = mapped_column(
        String
    )  # open-ended trigger label, e.g. price_above|earnings_release|thesis_contradiction
    condition_params_json: Mapped[dict] = mapped_column(
        JSONB
    )  # e.g. {"threshold": 419.0}

    # Logic
    objective: Mapped[str] = mapped_column(String)  # e.g. "Sell at $419"
    adjustment_plan: Mapped[str] = mapped_column(
        String
    )  # What to do if it doesn't happen
    deadline: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Status
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String)  # pending|triggered|expired|failed

    last_checked_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    triggered_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Results
    trigger_detail: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    action_taken_json: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
