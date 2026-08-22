import uuid
from datetime import date
from typing import Optional

from sqlalchemy import Boolean, Date, String
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
