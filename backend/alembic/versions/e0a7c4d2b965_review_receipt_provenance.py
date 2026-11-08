"""Retain original evidence independently from mailbox review deduplication.

Revision ID: e0a7c4d2b965
Revises: d9f6b3c1a854
"""

from collections import defaultdict
from uuid import NAMESPACE_URL, uuid5

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "e0a7c4d2b965"
down_revision = "d9f6b3c1a854"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "review_queue_items",
        sa.Column("evidence_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_review_queue_items_evidence_id",
        "review_queue_items",
        "raw_evidence",
        ["evidence_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_review_queue_items_evidence_id", "review_queue_items", ["evidence_id"]
    )
    connection = op.get_bind()
    connection.execute(sa.text("""
        UPDATE review_queue_items AS q SET evidence_id = r.id
        FROM raw_evidence AS r WHERE q.item_id = r.id AND q.item_type IN (
            'raw_evidence', 'mailbox_identity', 'mailbox_receipt',
            'corporate_action', 'account_transfer'
        )
    """))
    # The historical producer's UUIDv5 key is exact but external IDs were not
    # always account scoped. Never choose among conflicting legacy receipts.
    candidates = defaultdict(set)
    receipts = connection.execute(sa.text("""
        SELECT id, COALESCE(NULLIF(external_id, ''), metadata_json->>'external_id') AS external_key
        FROM raw_evidence WHERE source_item_type IN ('email', 'email_order_confirmation', 'email_skip')
    """)).mappings()
    for receipt in receipts:
        if receipt["external_key"]:
            key = uuid5(
                NAMESPACE_URL, f"mailbox-reconciliation:{receipt['external_key']}"
            )
            candidates[key].add(receipt["id"])
    reviews = connection.execute(sa.text("""
        SELECT id, item_id FROM review_queue_items
        WHERE evidence_id IS NULL AND item_type IN ('mailbox_receipt', 'corporate_action', 'account_transfer')
    """)).mappings().all()
    for review in reviews:
        matches = candidates.get(review["item_id"], set())
        if len(matches) == 1:
            connection.execute(
                sa.text(
                    "UPDATE review_queue_items SET evidence_id = :evidence_id WHERE id = :id"
                ),
                {"id": review["id"], "evidence_id": next(iter(matches))},
            )


def downgrade():
    op.drop_index("ix_review_queue_items_evidence_id", "review_queue_items")
    op.drop_constraint(
        "fk_review_queue_items_evidence_id", "review_queue_items", type_="foreignkey"
    )
    op.drop_column("review_queue_items", "evidence_id")
