"""add market setup currency

Revision ID: 18d4b6a20c71
Revises: f6a7b8c9d0e1
Create Date: 2026-07-13 00:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "18d4b6a20c71"
down_revision: Union[str, None] = "f6a7b8c9d0e1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "market_setup_signals",
        sa.Column("currency", sa.String(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("market_setup_signals", "currency")
