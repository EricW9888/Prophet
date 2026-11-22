"""Keep a source receipt for investigation outside tracked holdings."""

import sqlalchemy as sa

from alembic import op

revision = "ab27e48f9d63"
down_revision = "c291d8b352e4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("unresolved_questions", sa.Column("lead_evidence_id", sa.Uuid()))
    op.create_foreign_key(
        "fk_unresolved_questions_lead_evidence",
        "unresolved_questions",
        "raw_evidence",
        ["lead_evidence_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_unresolved_questions_lead_evidence_id",
        "unresolved_questions",
        ["lead_evidence_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_unresolved_questions_lead_evidence_id", "unresolved_questions")
    op.drop_constraint(
        "fk_unresolved_questions_lead_evidence",
        "unresolved_questions",
        type_="foreignkey",
    )
    op.drop_column("unresolved_questions", "lead_evidence_id")
