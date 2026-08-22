"""Restore database-enforced canonical state and edge uniqueness.

Revision ID: c8f3e1d2a7b4
Revises: 9d5f2a7c1e30
Create Date: 2026-08-22 12:00:00.000000
"""

from __future__ import annotations

from collections import defaultdict
from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c8f3e1d2a7b4"
down_revision: Union[str, None] = "9d5f2a7c1e30"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _unique_constraint_names(bind: Connection, table_name: str) -> set[str]:
    return {
        constraint["name"]
        for constraint in sa.inspect(bind).get_unique_constraints(table_name)
    }


def _reconcile_coverage_duplicates(bind: Connection) -> None:
    rows = bind.execute(sa.text("""
            SELECT id, subject_type, subject_id, last_computed_at
            FROM coverage_maps
            ORDER BY
                subject_type,
                subject_id,
                last_computed_at DESC NULLS LAST,
                id DESC
            """)).mappings()
    grouped: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in rows:
        grouped[(row["subject_type"], str(row["subject_id"]))].append(row)

    for duplicates in grouped.values():
        keep = duplicates[0]["id"]
        for duplicate in duplicates[1:]:
            drop = duplicate["id"]
            bind.execute(
                sa.text(
                    "UPDATE missing_evidence_classes "
                    "SET coverage_map_id = :keep WHERE coverage_map_id = :drop"
                ),
                {"keep": keep, "drop": drop},
            )
            bind.execute(
                sa.text(
                    "UPDATE unresolved_questions "
                    "SET coverage_map_id = :keep WHERE coverage_map_id = :drop"
                ),
                {"keep": keep, "drop": drop},
            )
            bind.execute(
                sa.text("DELETE FROM coverage_maps WHERE id = :drop"),
                {"drop": drop},
            )


def _reconcile_conclusion_duplicates(bind: Connection) -> None:
    rows = bind.execute(sa.text("""
            SELECT id, subject_type, subject_id, last_updated_at, last_verified_at
            FROM conclusion_states
            ORDER BY
                subject_type,
                subject_id,
                last_updated_at DESC NULLS LAST,
                last_verified_at DESC NULLS LAST,
                id DESC
            """)).mappings()
    grouped: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in rows:
        grouped[(row["subject_type"], str(row["subject_id"]))].append(row)

    dependent_updates = (
        sa.text(
            "UPDATE conclusion_revisions SET conclusion_state_id = :keep "
            "WHERE conclusion_state_id = :drop"
        ),
        sa.text(
            "UPDATE verification_runs SET conclusion_state_id = :keep "
            "WHERE conclusion_state_id = :drop"
        ),
        sa.text(
            "UPDATE decision_journals SET conclusion_state_id = :keep "
            "WHERE conclusion_state_id = :drop"
        ),
        sa.text(
            "UPDATE theses SET conclusion_state_id = :keep "
            "WHERE conclusion_state_id = :drop"
        ),
    )
    for duplicates in grouped.values():
        keep = duplicates[0]["id"]
        for duplicate in duplicates[1:]:
            drop = duplicate["id"]
            for statement in dependent_updates:
                bind.execute(
                    statement,
                    {"keep": keep, "drop": drop},
                )
            bind.execute(
                sa.text("DELETE FROM conclusion_states WHERE id = :drop"),
                {"drop": drop},
            )


def _reconcile_edge_duplicates(bind: Connection) -> None:
    rows = bind.execute(sa.text("""
            SELECT
                id,
                source_type,
                source_id,
                target_type,
                target_id,
                relationship_type,
                confidence,
                created_at
            FROM edges
            ORDER BY
                source_type,
                source_id,
                target_type,
                target_id,
                relationship_type,
                confidence DESC NULLS LAST,
                created_at DESC NULLS LAST,
                id DESC
            """)).mappings()
    grouped: dict[tuple[str, str, str, str, str], list[dict]] = defaultdict(list)
    for row in rows:
        grouped[
            (
                row["source_type"],
                str(row["source_id"]),
                row["target_type"],
                str(row["target_id"]),
                row["relationship_type"],
            )
        ].append(row)

    for duplicates in grouped.values():
        for duplicate in duplicates[1:]:
            bind.execute(
                sa.text("DELETE FROM edges WHERE id = :drop"),
                {"drop": duplicate["id"]},
            )


def upgrade() -> None:
    bind = op.get_bind()
    _reconcile_coverage_duplicates(bind)
    _reconcile_conclusion_duplicates(bind)
    _reconcile_edge_duplicates(bind)

    constraints = (
        (
            "uq_coverage_maps_subject",
            "coverage_maps",
            ["subject_type", "subject_id"],
        ),
        (
            "uq_conclusion_states_subject",
            "conclusion_states",
            ["subject_type", "subject_id"],
        ),
        (
            "uq_edges_relationship_tuple",
            "edges",
            [
                "source_type",
                "source_id",
                "target_type",
                "target_id",
                "relationship_type",
            ],
        ),
    )
    for constraint_name, table_name, columns in constraints:
        if constraint_name not in _unique_constraint_names(bind, table_name):
            op.create_unique_constraint(
                constraint_name,
                table_name,
                columns,
            )


def downgrade() -> None:
    bind = op.get_bind()
    constraints = (
        ("uq_edges_relationship_tuple", "edges"),
        ("uq_conclusion_states_subject", "conclusion_states"),
        ("uq_coverage_maps_subject", "coverage_maps"),
    )
    for constraint_name, table_name in constraints:
        if constraint_name in _unique_constraint_names(bind, table_name):
            op.drop_constraint(constraint_name, table_name, type_="unique")
