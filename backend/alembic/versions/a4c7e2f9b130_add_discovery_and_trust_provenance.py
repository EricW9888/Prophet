"""Add provisional discovery and source trust provenance.

Revision ID: a4c7e2f9b130
Revises: e3b1c4d5f6a7
Create Date: 2026-08-25 16:00:00.000000
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "a4c7e2f9b130"
down_revision: Union[str, None] = "e3b1c4d5f6a7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "sources",
        sa.Column(
            "trust_origin",
            sa.String(),
            server_default="discovered",
            nullable=False,
        ),
    )
    op.add_column(
        "sources",
        sa.Column(
            "trust_review_status",
            sa.String(),
            server_default="current",
            nullable=False,
        ),
    )
    op.add_column(
        "sources", sa.Column("trust_review_reason", sa.String(), nullable=True)
    )
    op.add_column(
        "sources",
        sa.Column("trust_reviewed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute("UPDATE sources SET trust_origin = 'operator' WHERE is_trusted IS TRUE")

    op.create_table(
        "research_discovery_observations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("request_id", sa.String(), nullable=True),
        sa.Column("query", sa.String(), nullable=False),
        sa.Column("effective_query", sa.String(), nullable=False),
        sa.Column("search_title", sa.String(), nullable=False),
        sa.Column("result_rank", sa.Integer(), nullable=False),
        sa.Column("result_title", sa.String(), nullable=False),
        sa.Column("url", sa.String(), nullable=False),
        sa.Column("snippet", sa.Text(), nullable=True),
        sa.Column("content_kind", sa.String(), nullable=False),
        sa.Column("outcome", sa.String(), nullable=False),
        sa.Column("evidence_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("subject_type", sa.String(), nullable=True),
        sa.Column("subject_id", sa.String(), nullable=True),
        sa.Column("subject_name", sa.String(), nullable=True),
        sa.Column("error", sa.String(), nullable=True),
        sa.Column(
            "metadata_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["evidence_id"], ["raw_evidence.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in ("evidence_id", "observed_at", "outcome", "provider", "query", "url"):
        op.create_index(
            op.f(f"ix_research_discovery_observations_{column}"),
            "research_discovery_observations",
            [column],
        )


def downgrade() -> None:
    op.drop_table("research_discovery_observations")
    op.drop_column("sources", "trust_reviewed_at")
    op.drop_column("sources", "trust_review_reason")
    op.drop_column("sources", "trust_review_status")
    op.drop_column("sources", "trust_origin")
