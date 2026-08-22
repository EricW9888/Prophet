import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Numeric, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, TimestampMixin, utcnow

if TYPE_CHECKING:
    from .entity import Security


class CashLedgerEntry(TimestampMixin, Base):
    __tablename__ = "cash_ledger_entries"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    entry_type: Mapped[str] = mapped_column(
        String, index=True
    )  # deposit|withdrawal|trade_settlement|dividend|fee
    amount: Mapped[float] = mapped_column(Numeric)
    balance_after: Mapped[float] = mapped_column(Numeric)

    transaction_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("transactions.id"), nullable=True, index=True
    )
    description: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    executed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class Position(Base):
    __tablename__ = "positions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    security_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("securities.id"), index=True
    )
    direction: Mapped[str] = mapped_column(String)  # long|short

    quantity: Mapped[float] = mapped_column(Numeric, default=0.0)
    avg_cost_basis: Mapped[float] = mapped_column(Numeric, default=0.0)
    current_price: Mapped[float] = mapped_column(Numeric, default=0.0)
    market_value: Mapped[float] = mapped_column(Numeric, default=0.0)
    unrealized_pnl: Mapped[float] = mapped_column(Numeric, default=0.0)
    weight_pct: Mapped[float] = mapped_column(Numeric, default=0.0)

    # Derivative/synthetic exposure fields
    notional_exposure: Mapped[Optional[float]] = mapped_column(Numeric, nullable=True)
    delta_exposure: Mapped[Optional[float]] = mapped_column(Numeric, nullable=True)
    is_nonlinear: Mapped[bool] = mapped_column(Boolean, default=False)

    thesis_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )  # Will FK to theses
    conviction: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # 1-5
    list_type: Mapped[str] = mapped_column(
        String, index=True
    )  # holding|watchlist|considering|theme_basket

    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_reviewed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    review_urgency: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # Autonomous discovery fields
    is_autonomous: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )
    review_status: Mapped[Optional[str]] = mapped_column(
        String, nullable=True
    )  # pending|approved|dismissed

    lots: Mapped[list["Lot"]] = relationship("Lot", back_populates="position")
    transactions: Mapped[list["Transaction"]] = relationship(
        "Transaction", back_populates="position"
    )
    security: Mapped["Security"] = relationship("Security")


class Lot(Base):
    __tablename__ = "lots"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    position_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("positions.id"), index=True
    )

    quantity: Mapped[float] = mapped_column(Numeric)
    cost_basis: Mapped[float] = mapped_column(Numeric)

    acquired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    lot_type: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    realized_pnl: Mapped[float] = mapped_column(Numeric, default=0.0)
    closed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    position: Mapped["Position"] = relationship("Position", back_populates="lots")


class Transaction(Base):
    __tablename__ = "transactions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    position_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("positions.id"), nullable=True, index=True
    )
    action: Mapped[str] = mapped_column(
        String
    )  # buy|sell|dividend|deposit|withdrawal|split|merger|spinoff|exercise|assign|expire

    quantity: Mapped[float] = mapped_column(Numeric)
    price: Mapped[Optional[float]] = mapped_column(Numeric, nullable=True)
    executed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    notes: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    status: Mapped[str] = mapped_column(
        String, server_default="settled", default="settled"
    )  # settled|pending|canceled|corrected
    superseded_by_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("transactions.id"), nullable=True
    )
    provenance_json: Mapped[Optional[dict]] = mapped_column(
        type_=__import__("sqlalchemy.dialects.postgresql", fromlist=["JSONB"]).JSONB,
        nullable=True,
    )

    position: Mapped["Position"] = relationship(
        "Position", back_populates="transactions"
    )
