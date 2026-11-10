import uuid
from datetime import date, datetime
from typing import Optional

from sqlalchemy import Boolean, CheckConstraint, Date, DateTime, Index, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class MarketCalendar(Base):
    """Trading sessions for core exchanges."""

    __tablename__ = "market_calendar"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    exchange_code: Mapped[str] = mapped_column(
        String, index=True
    )  # NYSE|NASDAQ|LSE|...

    calendar_date: Mapped[date] = mapped_column(Date, index=True)
    is_open: Mapped[bool] = mapped_column(Boolean)
    is_half_day: Mapped[bool] = mapped_column(Boolean, default=False)

    holiday_name: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    open_time_utc: Mapped[Optional[str]] = mapped_column(
        String, nullable=True
    )  # "14:30"
    close_time_utc: Mapped[Optional[str]] = mapped_column(
        String, nullable=True
    )  # "21:00"


class MarketSessionObservation(Base):
    """Provider session schedules observed alongside a completed daily bar."""

    __tablename__ = "market_session_observations"
    __table_args__ = (
        Index("ix_market_session_provider_symbol", "provider", "symbol", "opens_at"),
        CheckConstraint("closes_at > opens_at", name="ck_market_session_order"),
        CheckConstraint("observed_at >= closes_at", name="ck_market_session_observed"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    provider: Mapped[str] = mapped_column(String)
    symbol: Mapped[str] = mapped_column(String)
    exchange: Mapped[str] = mapped_column(String)
    timezone: Mapped[str] = mapped_column(String)
    opens_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    closes_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
