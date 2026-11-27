"""Store each company's revenue drivers from its annual report."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "b5d2e8f41a07"
down_revision = "f23c8784cd9a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "business_drivers",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "entity_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("entities.id"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("member", sa.String(), nullable=False),
        sa.Column("label", sa.String(), nullable=False),
        sa.Column("revenue", sa.Float(), nullable=False),
        sa.Column("share", sa.Float(), nullable=False),
        sa.Column("total_revenue", sa.Float(), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("filing_url", sa.String(), nullable=False),
        sa.Column("accession", sa.String(), nullable=True),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "entity_id", "period_end", "kind", "member", name="uq_business_driver"
        ),
    )
    op.create_index("ix_business_drivers_entity_id", "business_drivers", ["entity_id"])
    op.create_index(
        "ix_business_drivers_period_end", "business_drivers", ["period_end"]
    )


def downgrade() -> None:
    op.drop_index("ix_business_drivers_period_end", table_name="business_drivers")
    op.drop_index("ix_business_drivers_entity_id", table_name="business_drivers")
    op.drop_table("business_drivers")
