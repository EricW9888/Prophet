"""Separate investment attention from portfolio data integrity review.

Revision ID: d9f6b3c1a854
Revises: c8e5a2b0f743
"""

import sqlalchemy as sa

from alembic import op

revision = "d9f6b3c1a854"
down_revision = "c8e5a2b0f743"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "review_queue_items",
        sa.Column(
            "review_domain", sa.String(), nullable=False, server_default="investment"
        ),
    )
    # Backfill known producer contracts once. Runtime routing never parses prose.
    # Unknown legacy concerns remain visible in investment and unfiltered views.
    op.execute(sa.text("""
        UPDATE review_queue_items SET review_domain = 'portfolio_data'
        WHERE item_type IN (
            'mailbox_identity', 'mailbox_receipt', 'corporate_action',
            'account_transfer', 'reconciliation'
        ) OR (item_type = 'position'
            AND trigger_reason LIKE 'Reconciliation discrepancy for %: book=% vs broker=% (%)')
    """))
    op.create_check_constraint(
        "ck_review_queue_items_domain",
        "review_queue_items",
        "review_domain IN ('investment', 'portfolio_data')",
    )
    op.create_index(
        "ix_review_queue_items_domain_status",
        "review_queue_items",
        ["review_domain", "status"],
    )


def downgrade():
    op.drop_index("ix_review_queue_items_domain_status", "review_queue_items")
    op.drop_constraint(
        "ck_review_queue_items_domain", "review_queue_items", type_="check"
    )
    op.drop_column("review_queue_items", "review_domain")
