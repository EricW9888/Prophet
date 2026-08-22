"""reconcile legacy schema metadata

Revision ID: 7a1c3e5f9b20
Revises: 6d9e2a4f1c73
Create Date: 2026-07-14 19:48:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "7a1c3e5f9b20"
down_revision: Union[str, None] = "6d9e2a4f1c73"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            UPDATE decision_journals
            SET profile_version_id = COALESCE(profile_version_id, dossier_version_id)
            WHERE dossier_version_id IS NOT NULL
            """
        )
    )
    op.drop_column("decision_journals", "dossier_version_id")

    op.create_index("ix_lots_acquired_at", "lots", ["acquired_at"], unique=False)
    op.create_index(
        "ix_transactions_executed_at", "transactions", ["executed_at"], unique=False
    )

    # The unique indexes remain authoritative; these constraints duplicated them.
    op.drop_constraint("shadow_fills_order_id_key", "shadow_fills", type_="unique")
    op.drop_constraint(
        "shadow_orders_client_order_id_key", "shadow_orders", type_="unique"
    )


def downgrade() -> None:
    op.create_unique_constraint(
        "shadow_orders_client_order_id_key", "shadow_orders", ["client_order_id"]
    )
    op.create_unique_constraint(
        "shadow_fills_order_id_key", "shadow_fills", ["order_id"]
    )

    op.drop_index("ix_transactions_executed_at", table_name="transactions")
    op.drop_index("ix_lots_acquired_at", table_name="lots")

    op.add_column(
        "decision_journals",
        sa.Column("dossier_version_id", sa.UUID(), nullable=True),
    )
    op.execute(
        sa.text(
            """
            UPDATE decision_journals
            SET dossier_version_id = profile_version_id
            WHERE profile_version_id IS NOT NULL
            """
        )
    )
