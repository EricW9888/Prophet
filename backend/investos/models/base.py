import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow():
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    """Applied to all important records for time-honesty."""

    event_time: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    public_time: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    ingest_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    first_reasoned_time: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    eligible_action_time: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    stale_after: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    def mark_updated(self):
        self.updated_at = utcnow()
