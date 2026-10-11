"""Namespace mailbox identities without guessing legacy account ownership.

Revision ID: 8e3a7c1d4b29
Revises: 7d2f9a6b3c18
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "8e3a7c1d4b29"
down_revision = "7d2f9a6b3c18"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "mailbox_receipt_identities",
        sa.Column("identity_key", sa.String(), primary_key=True),
        sa.Column("identity_kind", sa.String(), nullable=False),
        sa.Column("account_namespace", sa.String(), nullable=False),
        sa.Column(
            "raw_evidence_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("raw_evidence.id"),
            nullable=False,
        ),
        sa.Column("provenance_json", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    for column in ("identity_kind", "account_namespace", "raw_evidence_id"):
        op.create_index(
            f"ix_mailbox_receipt_identities_{column}",
            "mailbox_receipt_identities",
            [column],
        )
    # No login namespace/UIDVALIDITY can be recovered from a bare UID. Keep all
    # old external IDs, corrections and ledger links intact for audited review.
    op.execute("""
        UPDATE raw_evidence SET metadata_json = COALESCE(metadata_json, '{}'::jsonb)
            || jsonb_build_object('mailbox_identity_migration', jsonb_build_object(
                'version', 2, 'status', 'legacy_unverified',
                'reason', 'account_and_uidvalidity_not_established'))
        WHERE source_item_type IN ('email_order_confirmation', 'email_skip')
           OR metadata_json ? 'operational_mailbox'
    """)


def downgrade():
    raise RuntimeError(
        "Mailbox identity bindings cannot be discarded; restore a pre-upgrade backup to use the older schema."
    )
