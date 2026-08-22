import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Float, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, utcnow


class SubjectAlias(Base):
    """Searchable aliases/synonyms for subjects across entities and themes."""

    __tablename__ = "subject_aliases"
    __table_args__ = (
        UniqueConstraint(
            "normalized_alias",
            "subject_type",
            "subject_id",
            name="uq_subject_alias_subject",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    alias: Mapped[str] = mapped_column(String, index=True)
    normalized_alias: Mapped[str] = mapped_column(String, index=True)
    subject_type: Mapped[str] = mapped_column(String, index=True)
    subject_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    source: Mapped[str] = mapped_column(String, default="system", index=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.8, index=True)
    reason: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
