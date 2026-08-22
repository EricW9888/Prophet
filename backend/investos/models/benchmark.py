import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import ARRAY, DateTime, Float, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, utcnow


class Benchmark(Base):
    """Index or alternative benchmark for comparing performance and narrative."""

    __tablename__ = "benchmarks"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String, index=True)
    ticker: Mapped[Optional[str]] = mapped_column(
        String, nullable=True
    )  # e.g. SPY, QQQ, TLT
    description: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    benchmark_type: Mapped[str] = mapped_column(
        String
    )  # broad_market|sector|factor|custom

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class BenchmarkConstituent(Base):
    """Constituent weighting in a custom aggregate benchmark at a point in time."""

    __tablename__ = "benchmark_constituents"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    benchmark_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("benchmarks.id"), index=True
    )
    security_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("securities.id"), index=True
    )

    weight_pct: Mapped[float] = mapped_column(Float)
    as_of_date: Mapped[datetime] = mapped_column(DateTime(timezone=True))
