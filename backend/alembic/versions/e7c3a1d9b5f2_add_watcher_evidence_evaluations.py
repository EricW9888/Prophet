"""Add auditable source-backed watcher evaluations.

Revision ID: e7c3a1d9b5f2
Revises: b2d4e6f8a1c3
Create Date: 2026-08-31 12:00:00.000000
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "e7c3a1d9b5f2"
down_revision: Union[str, None] = "b2d4e6f8a1c3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "watcher_evidence_evaluations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("watcher_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("raw_evidence_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(), server_default="pending", nullable=False),
        sa.Column(
            "evidence_refs_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["watcher_id"], ["active_watchers.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["raw_evidence_id"], ["raw_evidence.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "watcher_id",
            "raw_evidence_id",
            name="uq_watcher_evaluations_watcher_evidence",
        ),
    )
    op.create_index(
        "ix_watcher_evaluations_watcher_id",
        "watcher_evidence_evaluations",
        ["watcher_id"],
        unique=False,
    )
    op.create_index(
        "ix_watcher_evaluations_raw_evidence_id",
        "watcher_evidence_evaluations",
        ["raw_evidence_id"],
        unique=False,
    )
    op.create_index(
        "ix_watcher_evaluations_status",
        "watcher_evidence_evaluations",
        ["status"],
        unique=False,
    )
    op.execute(sa.text("""
            UPDATE active_watchers
            SET last_checked_at = NULL
            WHERE is_active = TRUE
              AND status = 'pending'
              AND condition_type NOT IN ('price_above', 'price_below', 'deadline', 'reminder')
            """))


def downgrade() -> None:
    op.drop_index(
        "ix_watcher_evaluations_status",
        table_name="watcher_evidence_evaluations",
    )
    op.drop_index(
        "ix_watcher_evaluations_raw_evidence_id",
        table_name="watcher_evidence_evaluations",
    )
    op.drop_index(
        "ix_watcher_evaluations_watcher_id",
        table_name="watcher_evidence_evaluations",
    )
    op.drop_table("watcher_evidence_evaluations")
