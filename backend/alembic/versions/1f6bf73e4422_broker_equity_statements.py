"""Persist dated manual broker equity statements and their reconciliation."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "1f6bf73e4422"
down_revision = "f1b8d5a0c274"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "broker_equity_statements",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("statement_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("statement", postgresql.JSONB(), nullable=False),
        sa.Column("reconciliation", postgresql.JSONB(), nullable=False),
    )
    op.create_index(
        "ix_broker_equity_statements_as_of", "broker_equity_statements", ["as_of"]
    )


def downgrade() -> None:
    op.drop_index(
        "ix_broker_equity_statements_as_of", table_name="broker_equity_statements"
    )
    op.drop_table("broker_equity_statements")
