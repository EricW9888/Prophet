import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, utcnow


class AttributionResult(Base):
    """Return attribution: how much is explained by factors vs idiosyncratic."""

    __tablename__ = "attribution_results"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    position_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("positions.id"), index=True, nullable=True
    )
    portfolio_wide: Mapped[bool] = mapped_column(Boolean, default=False)

    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    total_return: Mapped[float] = mapped_column(Float)
    factor_return: Mapped[float] = mapped_column(Float)
    idiosyncratic_return: Mapped[float] = mapped_column(Float)

    factor_contributions_json: Mapped[dict] = mapped_column(
        JSONB
    )  # e.g. {"market": 0.02, "sector": 0.01}

    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class FactorExposure(Base):
    """Current factor exposures for portfolio or a position."""

    __tablename__ = "factor_exposures"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    position_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("positions.id"), index=True, nullable=True
    )
    portfolio_wide: Mapped[bool] = mapped_column(Boolean, default=False)

    factor_name: Mapped[str] = mapped_column(String, index=True)
    exposure_value: Mapped[float] = mapped_column(Float)

    as_of_date: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class RegimeState(Base):
    """Current and historical regime classification."""

    __tablename__ = "regime_states"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    regime_type: Mapped[str] = mapped_column(
        String, index=True
    )  # risk_on|risk_off|transition|crisis|euphoria
    confidence: Mapped[float] = mapped_column(Float)
    signal_source: Mapped[str] = mapped_column(String)  # what data drove this

    start_date: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end_date: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )  # null = current

    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class ScenarioAnalysis(Base):
    """What-if analysis results."""

    __tablename__ = "scenario_analyses"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String)
    scenario_description: Mapped[str] = mapped_column(String)

    shock_parameters_json: Mapped[dict] = mapped_column(
        JSONB
    )  # e.g. {"rates": 0.50, "oil": 15}
    portfolio_impact_json: Mapped[dict] = mapped_column(JSONB)  # per-position impact
    total_portfolio_impact: Mapped[float] = mapped_column(Float)

    reasoning_run_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("reasoning_runs.id"), nullable=True
    )
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
