"""add source claim assessment queue state

Revision ID: 2b7c9d4e6f10
Revises: 18d4b6a20c71
Create Date: 2026-07-13 00:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "2b7c9d4e6f10"
down_revision: Union[str, None] = "18d4b6a20c71"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "source_claim_records",
        sa.Column(
            "assessment_attempt_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )
    op.add_column(
        "source_claim_records",
        sa.Column(
            "last_assessment_attempt_at", sa.DateTime(timezone=True), nullable=True
        ),
    )
    op.add_column(
        "source_claim_records",
        sa.Column("next_assessment_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "source_claim_records",
        sa.Column(
            "assessment_metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_source_claim_records_next_assessment_at",
        "source_claim_records",
        ["next_assessment_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_source_claim_records_next_assessment_at",
        table_name="source_claim_records",
    )
    op.drop_column("source_claim_records", "assessment_metadata")
    op.drop_column("source_claim_records", "next_assessment_at")
    op.drop_column("source_claim_records", "last_assessment_attempt_at")
    op.drop_column("source_claim_records", "assessment_attempt_count")
