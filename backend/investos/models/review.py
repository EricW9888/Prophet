import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, utcnow


class ReviewQueueItem(Base):
    """Prioritized queue for human review and action."""

    __tablename__ = "review_queue_items"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    item_type: Mapped[str] = mapped_column(
        String, index=True
    )  # position|watchlist|thesis|source|conclusion
    item_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)

    priority_score: Mapped[float] = mapped_column(Float, index=True)

    # Priority components
    size_factor: Mapped[float] = mapped_column(Float, default=0.0)
    evidence_change_factor: Mapped[float] = mapped_column(Float, default=0.0)
    contradiction_pressure: Mapped[float] = mapped_column(Float, default=0.0)
    thesis_drift: Mapped[float] = mapped_column(Float, default=0.0)
    catalyst_proximity: Mapped[float] = mapped_column(Float, default=0.0)
    coverage_weakness: Mapped[float] = mapped_column(Float, default=0.0)

    status: Mapped[str] = mapped_column(
        String, default="pending"
    )  # pending|in_review|resolved|dismissed

    trigger_reason: Mapped[str] = mapped_column(String)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    resolved_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    reasoning_run_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("reasoning_runs.id"), nullable=True
    )
