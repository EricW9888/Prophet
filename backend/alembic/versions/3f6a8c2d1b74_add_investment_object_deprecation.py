"""add investment object deprecation

Revision ID: 3f6a8c2d1b74
Revises: e7c3a1d9b5f2
Create Date: 2026-09-02 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "3f6a8c2d1b74"
down_revision: Union[str, None] = "e7c3a1d9b5f2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    for table_name in ("fundamental_metrics", "market_setup_signals"):
        op.add_column(
            table_name,
            sa.Column(
                "is_deprecated",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false"),
            ),
        )
        op.add_column(
            table_name,
            sa.Column("deprecated_reason", sa.String(), nullable=True),
        )


def downgrade() -> None:
    for table_name in ("market_setup_signals", "fundamental_metrics"):
        op.drop_column(table_name, "deprecated_reason")
        op.drop_column(table_name, "is_deprecated")
