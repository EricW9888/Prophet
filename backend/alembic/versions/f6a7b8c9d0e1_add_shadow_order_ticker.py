"""add_shadow_order_ticker

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-07-10 01:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f6a7b8c9d0e1"
down_revision: Union[str, None] = "e5f6a7b8c9d0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("shadow_orders")}
    if "ticker" not in columns:
        op.add_column("shadow_orders", sa.Column("ticker", sa.String(), nullable=True))
    op.execute(
        """
        UPDATE shadow_orders AS orders
        SET ticker = securities.ticker
        FROM securities
        WHERE securities.id = orders.security_id
        """
    )
    op.alter_column("shadow_orders", "ticker", nullable=False)
    indexes = {index["name"] for index in sa.inspect(bind).get_indexes("shadow_orders")}
    if op.f("ix_shadow_orders_ticker") not in indexes:
        op.create_index(
            op.f("ix_shadow_orders_ticker"),
            "shadow_orders",
            ["ticker"],
            unique=False,
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    indexes = {index["name"] for index in inspector.get_indexes("shadow_orders")}
    if op.f("ix_shadow_orders_ticker") in indexes:
        op.drop_index(op.f("ix_shadow_orders_ticker"), table_name="shadow_orders")
    columns = {column["name"] for column in sa.inspect(bind).get_columns("shadow_orders")}
    if "ticker" in columns:
        op.drop_column("shadow_orders", "ticker")
