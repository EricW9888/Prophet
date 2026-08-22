"""add knowledge mutation audit ledger

Revision ID: 9c6f4e2b18aa
Revises: 4f2d283cbdb2
Create Date: 2026-06-20 22:20:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "9c6f4e2b18aa"
down_revision: Union[str, None] = "4f2d283cbdb2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "knowledge_mutations",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("node_type", sa.String(), nullable=False),
        sa.Column("node_id", sa.UUID(), nullable=False),
        sa.Column("change_type", sa.String(), nullable=False),
        sa.Column("reason", sa.String(), nullable=True),
        sa.Column("actor", sa.String(), nullable=False),
        sa.Column("source_type", sa.String(), nullable=True),
        sa.Column("source_id", sa.UUID(), nullable=True),
        sa.Column("subject_type", sa.String(), nullable=True),
        sa.Column("subject_id", sa.UUID(), nullable=True),
        sa.Column("metadata_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_knowledge_mutations_actor"), "knowledge_mutations", ["actor"], unique=False)
    op.create_index(op.f("ix_knowledge_mutations_change_type"), "knowledge_mutations", ["change_type"], unique=False)
    op.create_index(op.f("ix_knowledge_mutations_created_at"), "knowledge_mutations", ["created_at"], unique=False)
    op.create_index(op.f("ix_knowledge_mutations_node_id"), "knowledge_mutations", ["node_id"], unique=False)
    op.create_index(op.f("ix_knowledge_mutations_node_type"), "knowledge_mutations", ["node_type"], unique=False)
    op.create_index(op.f("ix_knowledge_mutations_source_id"), "knowledge_mutations", ["source_id"], unique=False)
    op.create_index(op.f("ix_knowledge_mutations_source_type"), "knowledge_mutations", ["source_type"], unique=False)
    op.create_index(op.f("ix_knowledge_mutations_subject_id"), "knowledge_mutations", ["subject_id"], unique=False)
    op.create_index(op.f("ix_knowledge_mutations_subject_type"), "knowledge_mutations", ["subject_type"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_knowledge_mutations_subject_type"), table_name="knowledge_mutations")
    op.drop_index(op.f("ix_knowledge_mutations_subject_id"), table_name="knowledge_mutations")
    op.drop_index(op.f("ix_knowledge_mutations_source_type"), table_name="knowledge_mutations")
    op.drop_index(op.f("ix_knowledge_mutations_source_id"), table_name="knowledge_mutations")
    op.drop_index(op.f("ix_knowledge_mutations_node_type"), table_name="knowledge_mutations")
    op.drop_index(op.f("ix_knowledge_mutations_node_id"), table_name="knowledge_mutations")
    op.drop_index(op.f("ix_knowledge_mutations_created_at"), table_name="knowledge_mutations")
    op.drop_index(op.f("ix_knowledge_mutations_change_type"), table_name="knowledge_mutations")
    op.drop_index(op.f("ix_knowledge_mutations_actor"), table_name="knowledge_mutations")
    op.drop_table("knowledge_mutations")
