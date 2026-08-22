"""add_fundamental_metrics

Revision ID: d4e5f6a7b8c9
Revises: c1f2a3d4e5b6
Create Date: 2026-07-07 00:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "d4e5f6a7b8c9"
down_revision: Union[str, None] = "c1f2a3d4e5b6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "fundamental_metrics",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("subject_type", sa.String(), nullable=True),
        sa.Column("subject_id", sa.UUID(), nullable=True),
        sa.Column("entity_id", sa.UUID(), nullable=True),
        sa.Column("security_id", sa.UUID(), nullable=True),
        sa.Column("ticker", sa.String(), nullable=True),
        sa.Column("raw_evidence_id", sa.UUID(), nullable=True),
        sa.Column("source_item_id", sa.UUID(), nullable=True),
        sa.Column("metric_name", sa.String(), nullable=False),
        sa.Column("metric_family", sa.String(), nullable=False),
        sa.Column("value_text", sa.String(), nullable=True),
        sa.Column("numeric_value", sa.Numeric(), nullable=True),
        sa.Column("unit", sa.String(), nullable=True),
        sa.Column("currency", sa.String(), nullable=True),
        sa.Column("period_label", sa.String(), nullable=True),
        sa.Column("fiscal_year", sa.Integer(), nullable=True),
        sa.Column("fiscal_quarter", sa.String(), nullable=True),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=True),
        sa.Column("direction", sa.String(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("investment_relevance", sa.String(), nullable=True),
        sa.Column("next_test", sa.String(), nullable=True),
        sa.Column("source_kind", sa.String(), nullable=True),
        sa.Column("freshness_status", sa.String(), nullable=False),
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
        "raw_evidence_id",
        "source_item_id",
        "metric_name",
        "metric_family",
        "period_label",
        "as_of",
        "direction",
        "freshness_status",
    ):
        op.create_index(
            op.f(f"ix_fundamental_metrics_{column}"),
            "fundamental_metrics",
            [column],
            unique=False,
        )


def downgrade() -> None:
    for column in (
        "freshness_status",
        "direction",
        "as_of",
        "period_label",
        "metric_family",
        "metric_name",
        "source_item_id",
        "raw_evidence_id",
        "ticker",
        "security_id",
        "entity_id",
        "subject_id",
        "subject_type",
    ):
        op.drop_index(op.f(f"ix_fundamental_metrics_{column}"), table_name="fundamental_metrics")
    op.drop_table("fundamental_metrics")
