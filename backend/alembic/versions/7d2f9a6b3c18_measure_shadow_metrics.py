"""Retire shadow metric proxies while preserving their historical values.

Revision ID: 7d2f9a6b3c18
Revises: 6c1a4e8b2d90
"""

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "7d2f9a6b3c18"
down_revision: Union[str, None] = "6c1a4e8b2d90"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "experiment_results", "max_drawdown", existing_type=sa.Float(), nullable=True
    )
    op.add_column(
        "experiment_results",
        sa.Column(
            "performance_metrics_json",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    # Legacy checkpoints do not establish external-flow or sampling provenance.
    # Keep the old numbers for audit, but never present them as measurements.
    op.execute("""
        UPDATE experiment_results
        SET performance_metrics_json = jsonb_build_object(
            'version', 1,
            'source', 'legacy_terminal_return_proxies',
            'drawdown_status', 'unavailable',
            'sharpe_status', 'unavailable',
            'drawdown_reason', 'legacy_unverified_metrics',
            'sharpe_reason', 'legacy_unverified_metrics',
            'max_drawdown', NULL,
            'sharpe_ratio', NULL,
            'legacy_proxy_values', jsonb_build_object(
                'max_drawdown', max_drawdown, 'sharpe_ratio', sharpe_ratio
            )
        ), max_drawdown = NULL, sharpe_ratio = NULL
    """)
    op.alter_column(
        "experiment_results", "performance_metrics_json", server_default=None
    )


def downgrade() -> None:
    # The previous schema cannot represent unavailable drawdown. Refuse a
    # destructive downgrade instead of inventing zero or restoring proxy truth.
    raise RuntimeError(
        "Shadow metric provenance cannot be discarded; restore a pre-upgrade backup to use the older schema."
    )
