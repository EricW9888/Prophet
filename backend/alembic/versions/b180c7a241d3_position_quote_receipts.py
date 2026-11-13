"""Retain quote provenance; legacy cached marks remain unverified.

Revision ID: b180c7a241d3
Revises: a07ec2bd516f
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "b180c7a241d3"
down_revision = "a07ec2bd516f"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "positions", sa.Column("quote_receipt_json", postgresql.JSONB(), nullable=True)
    )


def downgrade():
    op.drop_column("positions", "quote_receipt_json")
