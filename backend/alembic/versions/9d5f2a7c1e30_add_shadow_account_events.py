"""add shadow account events

Revision ID: 9d5f2a7c1e30
Revises: 8c4e1f2a7b90
Create Date: 2026-07-16 12:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "9d5f2a7c1e30"
down_revision: Union[str, None] = "8c4e1f2a7b90"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "shadow_account_events",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("experiment_id", sa.UUID(), nullable=False),
        sa.Column("source_transaction_id", sa.UUID(), nullable=False),
        sa.Column("security_id", sa.UUID(), nullable=False),
        sa.Column("ticker", sa.String(), nullable=False),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("quantity_before", sa.Numeric(), nullable=False),
        sa.Column("quantity_after", sa.Numeric(), nullable=False),
        sa.Column("cash_before", sa.Numeric(), nullable=False),
        sa.Column("cash_after", sa.Numeric(), nullable=False),
        sa.Column("amount", sa.Numeric(), nullable=False),
        sa.Column("derivation", sa.String(), nullable=False),
        sa.Column("detail", sa.String(), nullable=True),
        sa.Column(
            "source_payload_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "account_snapshot_before_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "account_snapshot_after_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["experiment_id"], ["shadow_experiments.id"]),
        sa.ForeignKeyConstraint(["security_id"], ["securities.id"]),
        sa.ForeignKeyConstraint(["source_transaction_id"], ["transactions.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "experiment_id",
            "source_transaction_id",
            name="uq_shadow_account_event_source",
        ),
    )
    for column in (
        "experiment_id",
        "source_transaction_id",
        "security_id",
        "ticker",
        "event_type",
        "status",
        "occurred_at",
        "applied_at",
    ):
        op.create_index(
            op.f(f"ix_shadow_account_events_{column}"),
            "shadow_account_events",
            [column],
            unique=False,
        )


def downgrade() -> None:
    for column in reversed(
        (
            "experiment_id",
            "source_transaction_id",
            "security_id",
            "ticker",
            "event_type",
            "status",
            "occurred_at",
            "applied_at",
        )
    ):
        op.drop_index(
            op.f(f"ix_shadow_account_events_{column}"),
            table_name="shadow_account_events",
        )
    op.drop_table("shadow_account_events")
