"""Retain provenance-bound presentation translations.

Revision ID: b7d4f1a9e632
Revises: a6c9e2f5b831
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "b7d4f1a9e632"
down_revision = "a6c9e2f5b831"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "evidence_translations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "evidence_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("raw_evidence.id"),
            nullable=False,
        ),
        sa.Column("source_hash", sa.String(64), nullable=False),
        sa.Column("target_language", sa.String(), nullable=False),
        sa.Column("recipe_version", sa.String(), nullable=False),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("translated_text", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "evidence_id",
            "source_hash",
            "target_language",
            "recipe_version",
            name="uq_evidence_translation_version_language",
        ),
    )
    op.create_index(
        "ix_evidence_translations_evidence_id", "evidence_translations", ["evidence_id"]
    )


def downgrade():
    raise RuntimeError(
        "Translation derivatives retain source provenance; use a reviewed forward migration."
    )
