"""Give unfinished questions explicit ownership, outcomes and bounded next work.

Revision ID: 9f4b8d2e5c30
Revises: 8e3a7c1d4b29
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "9f4b8d2e5c30"
down_revision = "8e3a7c1d4b29"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "unresolved_questions", sa.Column("semantic_key", sa.String(), nullable=True)
    )
    op.create_index(
        "ix_unresolved_questions_semantic_key",
        "unresolved_questions",
        ["semantic_key"],
        unique=True,
    )
    for name, type_, default in [
        ("uncertainty_json", postgresql.JSONB(), sa.text("'{}'::jsonb")),
        ("owner", sa.String(), "research_automation"),
        ("last_material_outcome", sa.String(), "legacy_unassessed"),
        ("research_attempt_count", sa.Integer(), "0"),
        ("resolution_attempt_count", sa.Integer(), "0"),
    ]:
        op.add_column(
            "unresolved_questions",
            sa.Column(name, type_, nullable=False, server_default=default),
        )
        op.alter_column("unresolved_questions", name, server_default=None)
    op.add_column(
        "unresolved_questions", sa.Column("next_action", sa.String(), nullable=True)
    )
    op.add_column(
        "unresolved_questions",
        sa.Column("next_review_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_unresolved_questions_next_review_at",
        "unresolved_questions",
        ["next_review_at"],
    )
    op.add_column(
        "unresolved_questions",
        sa.Column(
            "superseded_by_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("unresolved_questions.id"),
            nullable=True,
        ),
    )
    op.create_table(
        "question_history",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "question_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("unresolved_questions.id"),
            nullable=False,
        ),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("detail_json", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_question_history_question_id", "question_history", ["question_id"]
    )
    # Preserve the old state explicitly. A fetched ID is not evidence that a
    # useful investigation is running, so unfinished rows get a new assessment.
    op.execute("""
        INSERT INTO question_history (id, question_id, event_type, detail_json, created_at)
        SELECT gen_random_uuid(), id, 'legacy_lifecycle_migration',
            jsonb_build_object('previous_status', status, 'question_text', question_text,
                               'originating_evidence_id', originating_evidence_id), now()
        FROM unresolved_questions
    """)
    op.execute("""
        UPDATE unresolved_questions SET status='archived', owner='history',
            last_material_outcome='legacy_stale_unprioritized'
        WHERE status='open' AND originating_evidence_id IS NULL AND created_at < now() - interval '30 days'
    """)
    op.execute("""
        UPDATE unresolved_questions SET
            next_action = CASE WHEN originating_evidence_id IS NOT NULL THEN 'assess_evidence' ELSE 'research' END,
            next_review_at = now(),
            status = CASE WHEN originating_evidence_id IS NOT NULL THEN 'awaiting_resolution' ELSE 'needs_research' END
        WHERE status IN ('open', 'investigating')
    """)
    op.execute(
        "UPDATE unresolved_questions SET owner='history' WHERE status IN ('answered', 'obsolete')"
    )


def downgrade():
    raise RuntimeError(
        "Question lifecycle history cannot be discarded; restore a pre-upgrade backup to use the older schema."
    )
