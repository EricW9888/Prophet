"""Index the generic notification feed by category and stable event order."""

from alembic import op

revision = "7d2e9a14c608"
down_revision = "6c9d2a805e13"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index(
        "ix_push_notification_events_feed",
        "push_notification_events",
        ["category", "created_at", "id"],
    )


def downgrade():
    op.drop_index(
        "ix_push_notification_events_feed", table_name="push_notification_events"
    )
