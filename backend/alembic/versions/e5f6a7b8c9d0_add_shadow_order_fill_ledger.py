"""add_shadow_order_fill_ledger

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-07-10 00:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "e5f6a7b8c9d0"
down_revision: Union[str, None] = "d4e5f6a7b8c9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "shadow_orders",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("experiment_id", sa.UUID(), nullable=False),
        sa.Column("security_id", sa.UUID(), nullable=False),
        sa.Column("client_order_id", sa.String(), nullable=False),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("side", sa.String(), nullable=False),
        sa.Column("order_type", sa.String(), nullable=False),
        sa.Column("time_in_force", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("requested_quantity", sa.Numeric(), nullable=False),
        sa.Column("filled_quantity", sa.Numeric(), nullable=False),
        sa.Column("reference_price", sa.Numeric(), nullable=False),
        sa.Column("filled_avg_price", sa.Numeric(), nullable=True),
        sa.Column("reserved_notional", sa.Numeric(), nullable=False),
        sa.Column("quote_session", sa.String(), nullable=True),
        sa.Column("quote_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("filled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("canceled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rejection_reason", sa.String(), nullable=True),
        sa.Column("rationale", sa.String(), nullable=False),
        sa.Column("checkpoint_index", sa.Integer(), nullable=False),
        sa.Column("evidence_refs_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("source_decision_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("account_snapshot_before_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("account_snapshot_after_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.ForeignKeyConstraint(["experiment_id"], ["shadow_experiments.id"]),
        sa.ForeignKeyConstraint(["security_id"], ["securities.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("client_order_id"),
    )
    op.create_index(op.f("ix_shadow_orders_experiment_id"), "shadow_orders", ["experiment_id"], unique=False)
    op.create_index(op.f("ix_shadow_orders_security_id"), "shadow_orders", ["security_id"], unique=False)
    op.create_index(op.f("ix_shadow_orders_client_order_id"), "shadow_orders", ["client_order_id"], unique=True)
    op.create_index(op.f("ix_shadow_orders_status"), "shadow_orders", ["status"], unique=False)
    op.create_index(op.f("ix_shadow_orders_submitted_at"), "shadow_orders", ["submitted_at"], unique=False)

    op.create_table(
        "shadow_fills",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("order_id", sa.UUID(), nullable=False),
        sa.Column("experiment_id", sa.UUID(), nullable=False),
        sa.Column("security_id", sa.UUID(), nullable=False),
        sa.Column("side", sa.String(), nullable=False),
        sa.Column("quantity", sa.Numeric(), nullable=False),
        sa.Column("price", sa.Numeric(), nullable=False),
        sa.Column("gross_notional", sa.Numeric(), nullable=False),
        sa.Column("fee", sa.Numeric(), nullable=False),
        sa.Column("slippage_bps", sa.Numeric(), nullable=False),
        sa.Column("filled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("quote_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("quote_session", sa.String(), nullable=True),
        sa.Column("cash_after", sa.Numeric(), nullable=False),
        sa.Column("position_quantity_after", sa.Numeric(), nullable=False),
        sa.ForeignKeyConstraint(["experiment_id"], ["shadow_experiments.id"]),
        sa.ForeignKeyConstraint(["order_id"], ["shadow_orders.id"]),
        sa.ForeignKeyConstraint(["security_id"], ["securities.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("order_id"),
    )
    op.create_index(op.f("ix_shadow_fills_order_id"), "shadow_fills", ["order_id"], unique=True)
    op.create_index(op.f("ix_shadow_fills_experiment_id"), "shadow_fills", ["experiment_id"], unique=False)
    op.create_index(op.f("ix_shadow_fills_security_id"), "shadow_fills", ["security_id"], unique=False)
    op.create_index(op.f("ix_shadow_fills_filled_at"), "shadow_fills", ["filled_at"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_shadow_fills_filled_at"), table_name="shadow_fills")
    op.drop_index(op.f("ix_shadow_fills_security_id"), table_name="shadow_fills")
    op.drop_index(op.f("ix_shadow_fills_experiment_id"), table_name="shadow_fills")
    op.drop_index(op.f("ix_shadow_fills_order_id"), table_name="shadow_fills")
    op.drop_table("shadow_fills")
    op.drop_index(op.f("ix_shadow_orders_submitted_at"), table_name="shadow_orders")
    op.drop_index(op.f("ix_shadow_orders_status"), table_name="shadow_orders")
    op.drop_index(op.f("ix_shadow_orders_client_order_id"), table_name="shadow_orders")
    op.drop_index(op.f("ix_shadow_orders_security_id"), table_name="shadow_orders")
    op.drop_index(op.f("ix_shadow_orders_experiment_id"), table_name="shadow_orders")
    op.drop_table("shadow_orders")
