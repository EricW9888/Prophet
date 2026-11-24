"""Persist follow-up subject work independently of evidence extraction."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "f23c8784cd9a"
down_revision = "a73cf40f1b28"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "evidence_processing_states",
        sa.Column(
            "subject_refresh_json",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column(
        "evidence_processing_states",
        sa.Column("next_subject_refresh_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_evidence_processing_states_next_subject_refresh_at",
        "evidence_processing_states",
        ["next_subject_refresh_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_evidence_processing_states_next_subject_refresh_at",
        table_name="evidence_processing_states",
    )
    op.drop_column("evidence_processing_states", "next_subject_refresh_at")
    op.drop_column("evidence_processing_states", "subject_refresh_json")
