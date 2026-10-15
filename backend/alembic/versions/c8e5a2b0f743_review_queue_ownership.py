"""Preserve retained connector reviews during computed queue refresh.

Revision ID: c8e5a2b0f743
Revises: b7d4f1a9e632
"""

import sqlalchemy as sa

from alembic import op

revision = "c8e5a2b0f743"
down_revision = "b7d4f1a9e632"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "review_queue_items",
        sa.Column(
            "queue_owner", sa.String(), nullable=False, server_default="retained"
        ),
    )
    # Recognize only the existing generator's type/format pairs. Unknown legacy
    # concerns keep their identities and are never assumed safe to regenerate.
    op.execute(sa.text("""
        UPDATE review_queue_items SET queue_owner = 'review_refresh'
        WHERE
            (item_type IN ('entity', 'theme', 'position')
                AND trigger_reason LIKE 'Open research question: %')
            OR (item_type = 'position' AND (
                trigger_reason = 'Holding lacks a strong accepted state.'
                OR trigger_reason LIKE 'Holding stance is % confidence.'))
            OR (item_type = 'shadow_experiment' AND (
                trigger_reason LIKE 'Evidence-linked shadow opportunity: %'
                OR trigger_reason LIKE 'Shadow experiment produced material divergence versus actual portfolio. alpha=%'))
            OR (item_type = 'source_claim_record'
                AND trigger_reason LIKE 'Pending source claim due for outcome assessment: %')
            OR (item_type = 'raw_evidence'
                AND trigger_reason LIKE '%Verify the disclosure and test whether it changes the thesis mechanism.%')
    """))
    op.create_index(
        "ix_review_queue_items_owner_status",
        "review_queue_items",
        ["queue_owner", "status"],
    )


def downgrade():
    raise RuntimeError(
        "Review ownership protects retained human concerns; restore a verified backup instead of discarding it."
    )
