"""Retain source-text extraction progress; legacy coverage remains unknown.

Revision ID: c291d8b352e4
Revises: b180c7a241d3
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "c291d8b352e4"
down_revision = "b180c7a241d3"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("evidence_processing_states", sa.Column("source_text_state_json", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")))


def downgrade():
    op.drop_column("evidence_processing_states", "source_text_state_json")
