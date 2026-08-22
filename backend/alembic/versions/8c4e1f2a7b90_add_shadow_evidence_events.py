"""add shadow evidence events

Revision ID: 8c4e1f2a7b90
Revises: 7a1c3e5f9b20
Create Date: 2026-07-15 12:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "8c4e1f2a7b90"
down_revision: Union[str, None] = "7a1c3e5f9b20"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "shadow_evidence_events",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("experiment_id", sa.UUID(), nullable=False),
        sa.Column("raw_evidence_id", sa.UUID(), nullable=True),
        sa.Column("subject_type", sa.String(), nullable=False),
        sa.Column("subject_id", sa.UUID(), nullable=False),
        sa.Column("security_id", sa.UUID(), nullable=True),
        sa.Column("trigger_reason", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("idempotency_key", sa.String(), nullable=False),
        sa.Column(
            "metadata_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("processing_detail", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["experiment_id"], ["shadow_experiments.id"]),
        sa.ForeignKeyConstraint(["raw_evidence_id"], ["raw_evidence.id"]),
        sa.ForeignKeyConstraint(["security_id"], ["securities.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_shadow_evidence_events_experiment_id"),
        "shadow_evidence_events",
        ["experiment_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_shadow_evidence_events_raw_evidence_id"),
        "shadow_evidence_events",
        ["raw_evidence_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_shadow_evidence_events_subject_type"),
        "shadow_evidence_events",
        ["subject_type"],
        unique=False,
    )
    op.create_index(
        op.f("ix_shadow_evidence_events_subject_id"),
        "shadow_evidence_events",
        ["subject_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_shadow_evidence_events_security_id"),
        "shadow_evidence_events",
        ["security_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_shadow_evidence_events_status"),
        "shadow_evidence_events",
        ["status"],
        unique=False,
    )
    op.create_index(
        op.f("ix_shadow_evidence_events_idempotency_key"),
        "shadow_evidence_events",
        ["idempotency_key"],
        unique=True,
    )
    op.create_index(
        op.f("ix_shadow_evidence_events_created_at"),
        "shadow_evidence_events",
        ["created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_shadow_evidence_events_created_at"),
        table_name="shadow_evidence_events",
    )
    op.drop_index(
        op.f("ix_shadow_evidence_events_idempotency_key"),
        table_name="shadow_evidence_events",
    )
    op.drop_index(
        op.f("ix_shadow_evidence_events_status"),
        table_name="shadow_evidence_events",
    )
    op.drop_index(
        op.f("ix_shadow_evidence_events_security_id"),
        table_name="shadow_evidence_events",
    )
    op.drop_index(
        op.f("ix_shadow_evidence_events_subject_id"),
        table_name="shadow_evidence_events",
    )
    op.drop_index(
        op.f("ix_shadow_evidence_events_subject_type"),
        table_name="shadow_evidence_events",
    )
    op.drop_index(
        op.f("ix_shadow_evidence_events_raw_evidence_id"),
        table_name="shadow_evidence_events",
    )
    op.drop_index(
        op.f("ix_shadow_evidence_events_experiment_id"),
        table_name="shadow_evidence_events",
    )
    op.drop_table("shadow_evidence_events")
