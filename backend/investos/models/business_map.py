import uuid
from datetime import date, datetime
from typing import Optional

from sqlalchemy import Date, DateTime, Float, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, utcnow


class BusinessDriver(Base):
    """One line of a company's revenue breakdown, as tagged in its annual report.

    A segment, product line, region or major customer with its revenue and
    share of total revenue for one fiscal year, read from the filer's inline
    XBRL. Earlier years are kept; the latest period is the current map.
    """

    __tablename__ = "business_drivers"
    __table_args__ = (
        UniqueConstraint(
            "entity_id", "period_end", "kind", "member", name="uq_business_driver"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    entity_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("entities.id"), index=True)
    kind: Mapped[str] = mapped_column(String)  # segment|product|region|customer
    member: Mapped[str] = mapped_column(String)
    label: Mapped[str] = mapped_column(String)
    revenue: Mapped[float] = mapped_column(Float)
    share: Mapped[float] = mapped_column(Float)
    total_revenue: Mapped[float] = mapped_column(Float)
    period_start: Mapped[date] = mapped_column(Date)
    period_end: Mapped[date] = mapped_column(Date, index=True)
    filing_url: Mapped[str] = mapped_column(String)
    accession: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
