import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, utcnow


class ManualOverride(Base):
    """Explicit human overrides that bypass LLM logic."""

    __tablename__ = "manual_overrides"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    target_type: Mapped[str] = mapped_column(
        String, index=True
    )  # thesis|implication|coverage|source|conclusion|fact|claim
    target_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)

    override_type: Mapped[str] = mapped_column(
        String
    )  # invalidate|force_update|mark_compromised|change_tier|lock_state

    reason: Mapped[str] = mapped_column(String)

    new_state_json: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    revoked_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
