"""Keep publisher subscriptions, fetch leases and coverage checkpoints."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "8e6a03c41f92"
down_revision = "7d2e9a14c608"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "publisher_feeds",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("url", sa.String(), nullable=False, unique=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("next_poll_at", sa.DateTime(timezone=True)),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("last_checked_at", sa.DateTime(timezone=True)),
        sa.Column("last_success_at", sa.DateTime(timezone=True)),
        sa.Column("last_status", sa.String(), nullable=False),
        sa.Column("last_error", sa.String()),
        sa.Column("entry_count", sa.Integer(), nullable=False),
        sa.Column("new_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_publisher_feeds_next_poll_at", "publisher_feeds", ["next_poll_at"]
    )


def downgrade():
    op.drop_table("publisher_feeds")
