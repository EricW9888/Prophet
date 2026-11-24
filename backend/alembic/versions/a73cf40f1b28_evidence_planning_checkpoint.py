"""Remember which extraction versions reached strategic planning."""

import sqlalchemy as sa

from alembic import op

revision = "a73cf40f1b28"
down_revision = "ab27e48f9d63"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "evidence_processing_states",
        sa.Column("planning_seen_extraction_at", sa.DateTime(timezone=True)),
    )


def downgrade():
    op.drop_column("evidence_processing_states", "planning_seen_extraction_at")
