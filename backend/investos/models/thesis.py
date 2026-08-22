import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import ARRAY, DateTime, Float, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, utcnow


class Thesis(Base):
    """Human-defined explicit thesis associated with an entity or position."""

    __tablename__ = "theses"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    entity_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("entities.id"), index=True, nullable=True
    )
    theme_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("themes.id"), index=True, nullable=True
    )

    # Text narrative sections
    overview: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    catalysts: Mapped[Optional[list[str]]] = mapped_column(ARRAY(String), nullable=True)
    risks: Mapped[Optional[list[str]]] = mapped_column(ARRAY(String), nullable=True)

    # Financial targets
    base_target: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    bull_target: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    bear_target: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    time_horizon_months: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # State mapping to conclusion system
    conclusion_state_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("conclusion_states.id"), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
