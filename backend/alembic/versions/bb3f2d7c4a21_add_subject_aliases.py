"""add subject alias index

Revision ID: bb3f2d7c4a21
Revises: 9c6f4e2b18aa
Create Date: 2026-06-22 12:20:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "bb3f2d7c4a21"
down_revision: Union[str, None] = "9c6f4e2b18aa"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "subject_aliases",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("alias", sa.String(), nullable=False),
        sa.Column("normalized_alias", sa.String(), nullable=False),
        sa.Column("subject_type", sa.String(), nullable=False),
        sa.Column("subject_id", sa.UUID(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("reason", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("normalized_alias", "subject_type", "subject_id", name="uq_subject_alias_subject"),
    )
    op.create_index(op.f("ix_subject_aliases_alias"), "subject_aliases", ["alias"], unique=False)
    op.create_index(op.f("ix_subject_aliases_confidence"), "subject_aliases", ["confidence"], unique=False)
    op.create_index(op.f("ix_subject_aliases_normalized_alias"), "subject_aliases", ["normalized_alias"], unique=False)
    op.create_index(op.f("ix_subject_aliases_source"), "subject_aliases", ["source"], unique=False)
    op.create_index(op.f("ix_subject_aliases_subject_id"), "subject_aliases", ["subject_id"], unique=False)
    op.create_index(op.f("ix_subject_aliases_subject_type"), "subject_aliases", ["subject_type"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_subject_aliases_subject_type"), table_name="subject_aliases")
    op.drop_index(op.f("ix_subject_aliases_subject_id"), table_name="subject_aliases")
    op.drop_index(op.f("ix_subject_aliases_source"), table_name="subject_aliases")
    op.drop_index(op.f("ix_subject_aliases_normalized_alias"), table_name="subject_aliases")
    op.drop_index(op.f("ix_subject_aliases_confidence"), table_name="subject_aliases")
    op.drop_index(op.f("ix_subject_aliases_alias"), table_name="subject_aliases")
    op.drop_table("subject_aliases")
