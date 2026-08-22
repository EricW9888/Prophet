"""add_verification_runs

Revision ID: b7b6f7e6f7c1
Revises: ae3037d2d597
Create Date: 2026-03-25 23:55:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "b7b6f7e6f7c1"
down_revision: Union[str, None] = "ae3037d2d597"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "verification_runs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("conclusion_state_id", sa.UUID(), nullable=False),
        sa.Column("trigger", sa.String(), nullable=False),
        sa.Column("evidence_packet_id", sa.UUID(), nullable=False),
        sa.Column("higher_tier_evidence_checked", postgresql.ARRAY(sa.UUID()), nullable=True),
        sa.Column("contradiction_coverage_status", sa.String(), nullable=False),
        sa.Column("missing_classes_found", postgresql.ARRAY(sa.String()), nullable=True),
        sa.Column("prior_stance", sa.String(), nullable=False),
        sa.Column("verified_stance", sa.String(), nullable=False),
        sa.Column("conclusion_changed", sa.Boolean(), nullable=False),
        sa.Column("change_reasoning", sa.String(), nullable=False),
        sa.Column("conclusion_revision_id", sa.UUID(), nullable=True),
        sa.Column("reasoning_run_id", sa.UUID(), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["conclusion_revision_id"], ["conclusion_revisions.id"]),
        sa.ForeignKeyConstraint(["conclusion_state_id"], ["conclusion_states.id"]),
        sa.ForeignKeyConstraint(["evidence_packet_id"], ["evidence_packets.id"]),
        sa.ForeignKeyConstraint(["reasoning_run_id"], ["reasoning_runs.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_verification_runs_conclusion_state_id"), "verification_runs", ["conclusion_state_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_verification_runs_conclusion_state_id"), table_name="verification_runs")
    op.drop_table("verification_runs")
