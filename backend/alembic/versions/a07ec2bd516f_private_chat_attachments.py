"""Retain private chat attachments separately from accepted research evidence."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "a07ec2bd516f"
down_revision = "9f71c05ae284"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "chat_attachments",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("content_type", sa.String(100), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("storage_path", sa.Text(), nullable=False),
        sa.Column("extracted_text", sa.Text(), nullable=False),
        sa.Column("representation_json", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade():
    op.drop_table("chat_attachments")
