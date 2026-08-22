import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, utcnow


class ExperimentFamilyState(Base):
    """Tracks state of counterfactual hypothesis templates to prevent duplication."""

    __tablename__ = "experiment_family_states"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    family_name: Mapped[str] = mapped_column(String, unique=True)

    description: Mapped[str] = mapped_column(String)
    trigger_conditions_json: Mapped[dict] = mapped_column(JSONB)

    total_runs: Mapped[int] = mapped_column(Integer, default=0)
    last_run_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    cooldown_days: Mapped[int] = mapped_column(Integer, default=30)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class ShadowExperiment(Base):
    """A specific instance of replay."""

    __tablename__ = "shadow_experiments"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    family_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("experiment_family_states.id"), nullable=True
    )

    name: Mapped[str] = mapped_column(String)
    policy_description: Mapped[str] = mapped_column(String)

    start_point: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end_point: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    initial_portfolio_state_json: Mapped[dict] = mapped_column(JSONB)
    final_portfolio_state_json: Mapped[Optional[dict]] = mapped_column(
        JSONB, nullable=True
    )

    run_status: Mapped[str] = mapped_column(
        String
    )  # queued|running|completed|failed|skipped (legacy: pending)
    skip_reason: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class ShadowEvidenceEvent(Base):
    """Durable evidence wake-up for an active shadow experiment."""

    __tablename__ = "shadow_evidence_events"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    experiment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("shadow_experiments.id"), index=True
    )
    raw_evidence_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("raw_evidence.id"), nullable=True, index=True
    )
    subject_type: Mapped[str] = mapped_column(String, index=True)
    subject_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    security_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("securities.id"), nullable=True, index=True
    )
    trigger_reason: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="queued", index=True)
    idempotency_key: Mapped[str] = mapped_column(String, unique=True, index=True)
    metadata_json: Mapped[dict] = mapped_column(JSONB, default=dict)
    processing_detail: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    processed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class ShadowAccountEvent(Base):
    """Idempotent corporate action applied to a simulated account."""

    __tablename__ = "shadow_account_events"
    __table_args__ = (
        UniqueConstraint(
            "experiment_id",
            "source_transaction_id",
            name="uq_shadow_account_event_source",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    experiment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("shadow_experiments.id"), index=True
    )
    source_transaction_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("transactions.id"), index=True
    )
    security_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("securities.id"), index=True
    )
    ticker: Mapped[str] = mapped_column(String, index=True)
    event_type: Mapped[str] = mapped_column(String, index=True)
    status: Mapped[str] = mapped_column(String, index=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    applied_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    quantity_before: Mapped[float] = mapped_column(Numeric)
    quantity_after: Mapped[float] = mapped_column(Numeric)
    cash_before: Mapped[float] = mapped_column(Numeric)
    cash_after: Mapped[float] = mapped_column(Numeric)
    amount: Mapped[float] = mapped_column(Numeric, default=0)
    derivation: Mapped[str] = mapped_column(String)
    detail: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    source_payload_json: Mapped[dict] = mapped_column(JSONB, default=dict)
    account_snapshot_before_json: Mapped[dict] = mapped_column(JSONB, default=dict)
    account_snapshot_after_json: Mapped[dict] = mapped_column(JSONB, default=dict)


class ShadowAction(Base):
    """Every transaction the counterfactual policy executed."""

    __tablename__ = "shadow_actions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    experiment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("shadow_experiments.id"), index=True
    )

    action: Mapped[str] = mapped_column(String)  # buy|sell|hold
    security_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("securities.id"))

    quantity: Mapped[float] = mapped_column(Float)
    price: Mapped[float] = mapped_column(Float)

    simulated_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    rationale: Mapped[str] = mapped_column(String)

    reasoning_run_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("reasoning_runs.id"), nullable=True
    )


class ShadowOrder(Base):
    """Durable paper-broker order. LLM output can propose but cannot fill it."""

    __tablename__ = "shadow_orders"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    experiment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("shadow_experiments.id"), index=True
    )
    security_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("securities.id"), index=True
    )
    ticker: Mapped[str] = mapped_column(String, index=True)
    client_order_id: Mapped[str] = mapped_column(String, unique=True, index=True)
    provider: Mapped[str] = mapped_column(String, default="local_simulator")
    side: Mapped[str] = mapped_column(String)
    order_type: Mapped[str] = mapped_column(String, default="market")
    time_in_force: Mapped[str] = mapped_column(String, default="day")
    status: Mapped[str] = mapped_column(String, index=True)
    requested_quantity: Mapped[float] = mapped_column(Numeric)
    filled_quantity: Mapped[float] = mapped_column(Numeric, default=0)
    reference_price: Mapped[float] = mapped_column(Numeric)
    filled_avg_price: Mapped[Optional[float]] = mapped_column(Numeric, nullable=True)
    reserved_notional: Mapped[float] = mapped_column(Numeric, default=0)
    quote_session: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    quote_time: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    accepted_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    filled_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    canceled_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    rejection_reason: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    rationale: Mapped[str] = mapped_column(String)
    checkpoint_index: Mapped[int] = mapped_column(Integer)
    evidence_refs_json: Mapped[list] = mapped_column(JSONB, default=list)
    source_decision_json: Mapped[dict] = mapped_column(JSONB, default=dict)
    account_snapshot_before_json: Mapped[dict] = mapped_column(JSONB, default=dict)
    account_snapshot_after_json: Mapped[dict] = mapped_column(JSONB, default=dict)


class ShadowFill(Base):
    """Immutable simulated execution produced by the deterministic paper broker."""

    __tablename__ = "shadow_fills"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    order_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("shadow_orders.id"), unique=True, index=True
    )
    experiment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("shadow_experiments.id"), index=True
    )
    security_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("securities.id"), index=True
    )
    side: Mapped[str] = mapped_column(String)
    quantity: Mapped[float] = mapped_column(Numeric)
    price: Mapped[float] = mapped_column(Numeric)
    gross_notional: Mapped[float] = mapped_column(Numeric)
    fee: Mapped[float] = mapped_column(Numeric, default=0)
    slippage_bps: Mapped[float] = mapped_column(Numeric, default=0)
    filled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    quote_time: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    quote_session: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    cash_after: Mapped[float] = mapped_column(Numeric)
    position_quantity_after: Mapped[float] = mapped_column(Numeric)


class ExperimentResult(Base):
    """Performance evaluation comparing shadow portfolio to real portfolio."""

    __tablename__ = "experiment_results"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    experiment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("shadow_experiments.id"), index=True, unique=True
    )

    shadow_return: Mapped[float] = mapped_column(Float)
    actual_return: Mapped[float] = mapped_column(Float)
    alpha: Mapped[float] = mapped_column(Float)

    max_drawdown: Mapped[float] = mapped_column(Float)
    sharpe_ratio: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    reasoning: Mapped[str] = mapped_column(String)
    reasoning_run_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("reasoning_runs.id"), nullable=True
    )
