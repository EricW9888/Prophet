"""add_market_setup_signals

Revision ID: c1f2a3d4e5b6
Revises: bb3f2d7c4a21
Create Date: 2026-07-01 00:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "c1f2a3d4e5b6"
down_revision: Union[str, None] = "bb3f2d7c4a21"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "market_setup_signals",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("subject_type", sa.String(), nullable=True),
        sa.Column("subject_id", sa.UUID(), nullable=True),
        sa.Column("entity_id", sa.UUID(), nullable=True),
        sa.Column("security_id", sa.UUID(), nullable=True),
        sa.Column("ticker", sa.String(), nullable=True),
        sa.Column("event_id", sa.UUID(), nullable=True),
        sa.Column("raw_evidence_id", sa.UUID(), nullable=True),
        sa.Column("source_item_id", sa.UUID(), nullable=True),
        sa.Column("signal_name", sa.String(), nullable=False),
        sa.Column("signal_family", sa.String(), nullable=False),
        sa.Column("setup_context", sa.String(), nullable=True),
        sa.Column("actual_context", sa.String(), nullable=True),
        sa.Column("price_reaction", sa.String(), nullable=True),
        sa.Column("value_text", sa.String(), nullable=True),
        sa.Column("numeric_value", sa.Numeric(), nullable=True),
        sa.Column("unit", sa.String(), nullable=True),
        sa.Column("period_label", sa.String(), nullable=True),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=True),
        sa.Column("direction", sa.String(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("investment_relevance", sa.String(), nullable=True),
        sa.Column("next_test", sa.String(), nullable=True),
        sa.Column("source_kind", sa.String(), nullable=True),
        sa.Column("outcome_status", sa.String(), nullable=False),
        sa.Column("outcome_notes", sa.String(), nullable=True),
        sa.Column("outcome_score", sa.Float(), nullable=True),
        sa.Column("metadata_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("event_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("public_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ingest_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("first_reasoned_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("eligible_action_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("stale_after", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["entity_id"], ["entities.id"]),
        sa.ForeignKeyConstraint(["event_id"], ["events.id"]),
        sa.ForeignKeyConstraint(["raw_evidence_id"], ["raw_evidence.id"]),
        sa.ForeignKeyConstraint(["security_id"], ["securities.id"]),
        sa.ForeignKeyConstraint(["source_item_id"], ["source_items.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in (
        "subject_type",
        "subject_id",
        "entity_id",
        "security_id",
        "ticker",
        "event_id",
        "raw_evidence_id",
        "source_item_id",
        "signal_name",
        "signal_family",
        "direction",
        "outcome_status",
    ):
        op.create_index(
            op.f(f"ix_market_setup_signals_{column}"),
            "market_setup_signals",
            [column],
            unique=False,
        )


def downgrade() -> None:
    for column in (
        "outcome_status",
        "direction",
        "signal_family",
        "signal_name",
        "source_item_id",
        "raw_evidence_id",
        "event_id",
        "ticker",
        "security_id",
        "entity_id",
        "subject_id",
        "subject_type",
    ):
        op.drop_index(op.f(f"ix_market_setup_signals_{column}"), table_name="market_setup_signals")
    op.drop_table("market_setup_signals")
