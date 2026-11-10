"""Retain observed provider session schedules across metadata rollover."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "6c9d2a805e13"
down_revision = "1f6bf73e4422"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "market_session_observations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("symbol", sa.String(), nullable=False),
        sa.Column("exchange", sa.String(), nullable=False),
        sa.Column("timezone", sa.String(), nullable=False),
        sa.Column("opens_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closes_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("closes_at > opens_at", name="ck_market_session_order"),
        sa.CheckConstraint("observed_at >= closes_at", name="ck_market_session_observed"),
    )
    op.create_index(
        "ix_market_session_provider_symbol",
        "market_session_observations",
        ["provider", "symbol", "opens_at"],
    )
    # Existing frozen packets already contain exact provider opening/closing
    # clocks. Preserve that evidence without changing any original packet.
    op.execute("""
        INSERT INTO market_session_observations
            (id, provider, symbol, exchange, timezone, opens_at, closes_at, observed_at)
        SELECT
            md5(a.id::text || ':' || s.key || ':' || (b.value->>'timestamp'))::uuid,
            s.value->>'provider', s.value->>'reported_symbol',
            coalesce(s.value->>'reported_exchange', ''),
            s.value->>'exchange_timezone',
            (b.value->>'session_open_at')::timestamptz,
            (b.value->>'completed_at')::timestamptz,
            (s.value->>'retrieved_at')::timestamptz
        FROM scenario_analyses a
        CROSS JOIN LATERAL jsonb_each(
            coalesce(a.shock_parameters_json->'packet'->'snapshots', '{}'::jsonb)
        ) s
        CROSS JOIN LATERAL jsonb_array_elements(
            coalesce(s.value->'daily_observations', '[]'::jsonb)
        ) b
        WHERE s.value->>'provider' = 'yahoo_finance'
          AND s.value->>'reported_symbol' = s.key
          AND s.value->>'exchange_timezone' IS NOT NULL
          AND b.value->>'session_open_at' IS NOT NULL
          AND b.value->>'session_open_at' = b.value->>'timestamp'
          AND (b.value->>'completed_at')::timestamptz
              > (b.value->>'session_open_at')::timestamptz
          AND (b.value->>'completed_at')::timestamptz
              <= (s.value->>'retrieved_at')::timestamptz
        ON CONFLICT (id) DO NOTHING
    """)


def downgrade() -> None:
    op.drop_index(
        "ix_market_session_provider_symbol", table_name="market_session_observations"
    )
    op.drop_table("market_session_observations")
