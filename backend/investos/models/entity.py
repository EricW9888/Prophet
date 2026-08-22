import uuid
from datetime import date
from typing import Optional

from sqlalchemy import Boolean, Date, ForeignKey, Numeric, String
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base


class Entity(Base):
    __tablename__ = "entities"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String, index=True)
    entity_type: Mapped[str] = mapped_column(
        String, index=True
    )  # company|person|organization|index|commodity|currency
    aliases: Mapped[Optional[list[str]]] = mapped_column(ARRAY(String), nullable=True)
    sector: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    industry: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    country: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    description: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    securities: Mapped[list["Security"]] = relationship(
        "Security", back_populates="entity"
    )


class Security(Base):
    __tablename__ = "securities"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    entity_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("entities.id"), index=True)
    ticker: Mapped[str] = mapped_column(String, index=True)
    exchange: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    asset_class: Mapped[str] = mapped_column(
        String, index=True
    )  # equity|option|etf|bond|crypto|commodity_future|fx
    instrument_type: Mapped[str] = mapped_column(
        String
    )  # common_stock|preferred|adr|warrant|call_option|put_option|etf_leveraged|etf_inverse|bond_fund|crypto_spot

    share_class: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    isin: Mapped[Optional[str]] = mapped_column(String, nullable=True, unique=True)
    cusip: Mapped[Optional[str]] = mapped_column(String, nullable=True, unique=True)

    multiplier: Mapped[Optional[float]] = mapped_column(Numeric, nullable=True)
    underlying_security_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("securities.id"), nullable=True
    )
    expiration_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    strike_price: Mapped[Optional[float]] = mapped_column(Numeric, nullable=True)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    delisted_at: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    entity: Mapped["Entity"] = relationship("Entity", back_populates="securities")
    underlying_security: Mapped[Optional["Security"]] = relationship(
        "Security", remote_side=[id]
    )
