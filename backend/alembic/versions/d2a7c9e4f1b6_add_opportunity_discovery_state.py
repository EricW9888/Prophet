"""Add bounded opportunity discovery state.

Revision ID: d2a7c9e4f1b6
Revises: c8f3e1d2a7b4
Create Date: 2026-08-24 12:00:00.000000
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "d2a7c9e4f1b6"
down_revision: Union[str, None] = "c8f3e1d2a7b4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "opportunity_universe_members",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("security_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("entity_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("priority", sa.Float(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column(
            "metadata_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column("last_inspected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_inspection_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["entity_id"], ["entities.id"]),
        sa.ForeignKeyConstraint(["security_id"], ["securities.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "security_id",
            name="uq_opportunity_universe_members_security",
        ),
    )
    op.create_index(
        op.f("ix_opportunity_universe_members_enabled"),
        "opportunity_universe_members",
        ["enabled"],
    )
    op.create_index(
        op.f("ix_opportunity_universe_members_entity_id"),
        "opportunity_universe_members",
        ["entity_id"],
    )
    op.create_index(
        op.f("ix_opportunity_universe_members_last_inspected_at"),
        "opportunity_universe_members",
        ["last_inspected_at"],
    )
    op.create_index(
        op.f("ix_opportunity_universe_members_next_inspection_at"),
        "opportunity_universe_members",
        ["next_inspection_at"],
    )
    op.create_index(
        op.f("ix_opportunity_universe_members_security_id"),
        "opportunity_universe_members",
        ["security_id"],
    )
    op.create_index(
        op.f("ix_opportunity_universe_members_source"),
        "opportunity_universe_members",
        ["source"],
    )

    op.create_table(
        "opportunity_discovery_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("active_key", sa.String(), nullable=True),
        sa.Column("owner_token", sa.String(), nullable=True),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("universe_size", sa.Integer(), nullable=False),
        sa.Column("planned_count", sa.Integer(), nullable=False),
        sa.Column("inspected_count", sa.Integer(), nullable=False),
        sa.Column("skipped_count", sa.Integer(), nullable=False),
        sa.Column("failed_count", sa.Integer(), nullable=False),
        sa.Column("estimated_credits", sa.Integer(), nullable=False),
        sa.Column(
            "remaining_member_ids_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "inspected_member_ids_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "skipped_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column(
            "failures_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column(
            "provider_attempts_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "limits_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column("detail", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "active_key",
            name="uq_opportunity_discovery_runs_active_key",
        ),
    )
    op.create_index(
        op.f("ix_opportunity_discovery_runs_captured_at"),
        "opportunity_discovery_runs",
        ["captured_at"],
    )
    op.create_index(
        op.f("ix_opportunity_discovery_runs_heartbeat_at"),
        "opportunity_discovery_runs",
        ["heartbeat_at"],
    )
    op.create_index(
        op.f("ix_opportunity_discovery_runs_started_at"),
        "opportunity_discovery_runs",
        ["started_at"],
    )
    op.create_index(
        op.f("ix_opportunity_discovery_runs_status"),
        "opportunity_discovery_runs",
        ["status"],
    )

    op.create_table(
        "opportunity_candidates",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("entity_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("security_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("shadow_experiment_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("ticker", sa.String(), nullable=False),
        sa.Column("fingerprint", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("family_key", sa.String(), nullable=True),
        sa.Column("priority_score", sa.Float(), nullable=False),
        sa.Column("signal_stage", sa.String(), nullable=True),
        sa.Column("why_now", sa.String(), nullable=False),
        sa.Column("investable_thesis", sa.String(), nullable=False),
        sa.Column("portfolio_transmission", sa.String(), nullable=False),
        sa.Column("expected_edge", sa.String(), nullable=False),
        sa.Column(
            "falsification_tests_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "assumptions_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column(
            "uncertainties_json",
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
        sa.Column(
            "ranking_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column(
            "discovery_profile_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("review_reason", sa.String(), nullable=True),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["entity_id"], ["entities.id"]),
        sa.ForeignKeyConstraint(["run_id"], ["opportunity_discovery_runs.id"]),
        sa.ForeignKeyConstraint(["security_id"], ["securities.id"]),
        sa.ForeignKeyConstraint(["shadow_experiment_id"], ["shadow_experiments.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "fingerprint",
            name="uq_opportunity_candidates_fingerprint",
        ),
    )
    for column in (
        "captured_at",
        "entity_id",
        "expires_at",
        "fingerprint",
        "last_seen_at",
        "priority_score",
        "run_id",
        "security_id",
        "shadow_experiment_id",
        "status",
        "ticker",
    ):
        op.create_index(
            op.f(f"ix_opportunity_candidates_{column}"),
            "opportunity_candidates",
            [column],
        )


def downgrade() -> None:
    op.drop_table("opportunity_candidates")
    op.drop_table("opportunity_discovery_runs")
    op.drop_table("opportunity_universe_members")
