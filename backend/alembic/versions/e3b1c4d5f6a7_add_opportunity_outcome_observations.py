"""Add point-in-time opportunity outcome observations.

Revision ID: e3b1c4d5f6a7
Revises: d2a7c9e4f1b6
Create Date: 2026-08-25 10:00:00.000000
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "e3b1c4d5f6a7"
down_revision: Union[str, None] = "d2a7c9e4f1b6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "opportunity_candidate_observations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("candidate_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("security_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("ticker", sa.String(), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("horizon_label", sa.String(), nullable=False),
        sa.Column("horizon_days", sa.Integer(), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expected_relative_direction", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column(
            "profile_snapshot_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "evidence_refs_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "evidence_snapshot_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("benchmark_ticker", sa.String(), nullable=False),
        sa.Column("market_data_provider", sa.String(), nullable=False),
        sa.Column("candidate_start_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("candidate_start_price", sa.Float(), nullable=True),
        sa.Column("benchmark_start_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("benchmark_start_price", sa.Float(), nullable=True),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("candidate_end_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("candidate_end_price", sa.Float(), nullable=True),
        sa.Column("benchmark_end_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("benchmark_end_price", sa.Float(), nullable=True),
        sa.Column("candidate_return_pct", sa.Float(), nullable=True),
        sa.Column("benchmark_return_pct", sa.Float(), nullable=True),
        sa.Column("excess_return_pct", sa.Float(), nullable=True),
        sa.Column("cash_return_pct", sa.Float(), nullable=False),
        sa.Column("result_label", sa.String(), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(), nullable=True),
        sa.Column(
            "evaluation_policy_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["candidate_id"],
            ["opportunity_candidates.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["run_id"], ["opportunity_discovery_runs.id"]),
        sa.ForeignKeyConstraint(["security_id"], ["securities.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "candidate_id",
            "run_id",
            name="uq_opportunity_candidate_observations_candidate_run",
        ),
    )
    for column in (
        "candidate_id",
        "captured_at",
        "due_at",
        "evaluated_at",
        "run_id",
        "security_id",
        "status",
        "ticker",
    ):
        op.create_index(
            op.f(f"ix_opportunity_candidate_observations_{column}"),
            "opportunity_candidate_observations",
            [column],
        )


def downgrade() -> None:
    op.drop_table("opportunity_candidate_observations")
