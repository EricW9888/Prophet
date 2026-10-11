"""Preserve shadow history and enforce one active run per family.

Revision ID: a6c9e2f5b831
Revises: 9f4b8d2e5c30
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "a6c9e2f5b831"
down_revision = "9f4b8d2e5c30"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "experiment_family_states",
        sa.Column("semantic_key", sa.String(), nullable=True),
    )
    op.create_unique_constraint(
        "uq_experiment_family_states_semantic_key",
        "experiment_family_states",
        ["semantic_key"],
    )
    op.add_column(
        "experiment_family_states",
        sa.Column(
            "identity_json",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.alter_column("experiment_family_states", "identity_json", server_default=None)
    for table, column, referenced in [
        ("experiment_family_states", "superseded_by_id", "experiment_family_states"),
        ("shadow_experiments", "superseded_by_id", "shadow_experiments"),
        ("shadow_experiments", "previous_run_id", "shadow_experiments"),
    ]:
        op.add_column(
            table, sa.Column(column, postgresql.UUID(as_uuid=True), nullable=True)
        )
        op.create_foreign_key(
            f"fk_{table}_{column}", table, referenced, [column], ["id"]
        )
    op.create_table(
        "shadow_lifecycle_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "experiment_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("shadow_experiments.id"),
            nullable=False,
        ),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("detail_json", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_shadow_lifecycle_events_experiment_id",
        "shadow_lifecycle_events",
        ["experiment_id"],
    )
    # Retain the original status and snapshot before resolving only exact existing
    # family membership. Prose is not enough to infer cross-family equivalence.
    op.execute("""
        INSERT INTO shadow_lifecycle_events (id, experiment_id, event_type, detail_json, created_at)
        SELECT gen_random_uuid(), id, 'legacy_state_retained',
               jsonb_build_object('run_status', run_status, 'family_id', family_id,
                 'initial_state', initial_portfolio_state_json, 'final_state', final_portfolio_state_json,
                 'identity_status', 'unclassified_legacy'), now()
        FROM shadow_experiments
    """)
    op.execute("""
        WITH ranked AS (
          SELECT id, first_value(id) OVER (
            PARTITION BY family_id ORDER BY
              CASE WHEN (final_portfolio_state_json #>> '{run_details,progress,step_count}') ~ '^[0-9]{1,9}$' THEN (final_portfolio_state_json #>> '{run_details,progress,step_count}')::integer ELSE 0 END DESC,
              created_at, id) AS retained_id,
            row_number() OVER (PARTITION BY family_id ORDER BY
              CASE WHEN (final_portfolio_state_json #>> '{run_details,progress,step_count}') ~ '^[0-9]{1,9}$' THEN (final_portfolio_state_json #>> '{run_details,progress,step_count}')::integer ELSE 0 END DESC,
              created_at, id) AS ordinal
          FROM shadow_experiments
          WHERE family_id IS NOT NULL AND run_status IN ('queued', 'pending', 'running')
        )
        UPDATE shadow_experiments e SET run_status = 'superseded', completed_at = now(),
          superseded_by_id = ranked.retained_id,
          skip_reason = 'Exact existing family duplicate; retained run has most observed checkpoints.'
        FROM ranked WHERE e.id = ranked.id AND ranked.ordinal > 1
    """)
    op.execute("""
        INSERT INTO shadow_lifecycle_events (id, experiment_id, event_type, detail_json, created_at)
        SELECT gen_random_uuid(), id, 'superseded',
          jsonb_build_object('retained_experiment_id', superseded_by_id, 'reason', skip_reason,
            'decision_basis', 'exact_existing_family_membership'), now()
        FROM shadow_experiments WHERE superseded_by_id IS NOT NULL
    """)
    # Cancel outstanding simulated orders, retaining the order rows and all fills.
    op.execute("""
        UPDATE shadow_orders SET status = 'canceled', canceled_at = now(), reserved_notional = 0
        WHERE status = 'accepted' AND experiment_id IN (
          SELECT id FROM shadow_experiments WHERE run_status = 'superseded')
    """)
    op.execute("""
        UPDATE shadow_experiments SET final_portfolio_state_json =
          jsonb_set(COALESCE(final_portfolio_state_json, initial_portfolio_state_json),
                    '{shadow_state,cash_reserved}', '0'::jsonb, true)
        WHERE run_status = 'superseded'
          AND COALESCE(final_portfolio_state_json, initial_portfolio_state_json) ? 'shadow_state'
    """)
    op.execute("""
        UPDATE shadow_evidence_events SET status = 'window_closed', processed_at = now(),
          processing_detail = 'Exact family duplicate superseded; receipt retained without policy consumption.'
        WHERE status IN ('queued', 'attached') AND experiment_id IN (
          SELECT id FROM shadow_experiments WHERE run_status = 'superseded')
    """)
    op.create_index(
        "uq_shadow_active_family",
        "shadow_experiments",
        ["family_id"],
        unique=True,
        postgresql_where=sa.text("run_status IN ('queued', 'pending', 'running')"),
    )
    op.execute("""
        CREATE FUNCTION guard_shadow_active_family() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE replaced_by uuid;
        BEGIN
          IF NEW.family_id IS NOT NULL AND NEW.run_status IN ('queued', 'pending', 'running') THEN
            SELECT superseded_by_id INTO replaced_by FROM experiment_family_states
              WHERE id = NEW.family_id FOR UPDATE;
            IF replaced_by IS NOT NULL THEN
              RAISE EXCEPTION 'Active shadow work must use the retained canonical family' USING ERRCODE = '23514';
            END IF;
          END IF;
          RETURN NEW;
        END $$;
    """)
    op.execute("""
        CREATE TRIGGER shadow_active_family_guard BEFORE INSERT OR UPDATE OF family_id, run_status
          ON shadow_experiments FOR EACH ROW EXECUTE FUNCTION guard_shadow_active_family();
    """)
    op.execute("""
        CREATE FUNCTION guard_shadow_family_supersession() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF NEW.superseded_by_id IS NOT NULL AND EXISTS (
              SELECT 1 FROM shadow_experiments WHERE family_id = NEW.id
                AND run_status IN ('queued', 'pending', 'running')) THEN
            RAISE EXCEPTION 'Superseded shadow families cannot retain active work' USING ERRCODE = '23514';
          END IF;
          RETURN NEW;
        END $$;
    """)
    op.execute("""
        CREATE CONSTRAINT TRIGGER shadow_family_supersession_guard AFTER UPDATE
          ON experiment_family_states DEFERRABLE INITIALLY DEFERRED
          FOR EACH ROW EXECUTE FUNCTION guard_shadow_family_supersession();
    """)


def downgrade():
    raise RuntimeError(
        "Shadow lifecycle and supersession provenance cannot be discarded."
    )
