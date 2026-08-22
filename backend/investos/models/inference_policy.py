import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Float, Integer, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, utcnow


class InferencePolicy(Base):
    """Routing rules governing model selection and cost."""

    __tablename__ = "inference_policies"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    task_type: Mapped[str] = mapped_column(String, unique=True, index=True)
    # extraction|profile_update|implication|memo|critique|chat_response|coverage_gap

    model_tier: Mapped[str] = mapped_column(String)  # cheap|standard|premium
    model_name: Mapped[str] = mapped_column(
        String
    )  # e.g. "codex-mini", "o3-mini", "o3"

    max_input_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    max_output_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    temperature: Mapped[float] = mapped_column(Float, default=0.0)

    enable_caching: Mapped[bool] = mapped_column(Boolean, default=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class InferenceLog(Base):
    """Aggregate daily usage logging for budget constraints."""

    __tablename__ = "inference_logs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    log_date: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), index=True
    )  # Midnight UTC

    model_name: Mapped[str] = mapped_column(String, index=True)
    task_type: Mapped[str] = mapped_column(String, index=True)

    total_calls: Mapped[int] = mapped_column(Integer, default=0)
    total_input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    total_output_tokens: Mapped[int] = mapped_column(Integer, default=0)

    total_cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
