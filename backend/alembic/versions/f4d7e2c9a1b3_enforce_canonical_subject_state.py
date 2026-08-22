"""Enforce canonical current coverage and conclusion state per subject.

Revision ID: f4d7e2c9a1b3
Revises: e921fa634761
Create Date: 2026-04-09 03:40:00.000000
"""

from __future__ import annotations

from collections import defaultdict
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "f4d7e2c9a1b3"
down_revision: Union[str, None] = "e921fa634761"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _unique_constraint_names(table_name: str) -> set[str]:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return {constraint["name"] for constraint in inspector.get_unique_constraints(table_name)}


def _coverage_rows():
    bind = op.get_bind()
    return bind.execute(
        sa.text(
            """
            SELECT id, subject_type, subject_id, last_computed_at
            FROM coverage_maps
            ORDER BY subject_type, subject_id, last_computed_at DESC NULLS LAST, id DESC
            """
        )
    ).mappings().all()


def _conclusion_rows():
    bind = op.get_bind()
    return bind.execute(
        sa.text(
            """
            SELECT id, subject_type, subject_id, last_updated_at, last_verified_at
            FROM conclusion_states
            ORDER BY subject_type, subject_id, last_updated_at DESC NULLS LAST, last_verified_at DESC NULLS LAST, id DESC
            """
        )
    ).mappings().all()


def _reconcile_coverage_duplicates() -> None:
    bind = op.get_bind()
    grouped: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in _coverage_rows():
        grouped[(row["subject_type"], str(row["subject_id"]))].append(row)

    for rows in grouped.values():
        keep = rows[0]["id"]
        for duplicate in rows[1:]:
            drop = duplicate["id"]
            bind.execute(
                sa.text(
                    "UPDATE missing_evidence_classes SET coverage_map_id = :keep WHERE coverage_map_id = :drop"
                ),
                {"keep": keep, "drop": drop},
            )
            bind.execute(
                sa.text(
                    "UPDATE unresolved_questions SET coverage_map_id = :keep WHERE coverage_map_id = :drop"
                ),
                {"keep": keep, "drop": drop},
            )
            bind.execute(sa.text("DELETE FROM coverage_maps WHERE id = :drop"), {"drop": drop})


def _reconcile_conclusion_duplicates() -> None:
    bind = op.get_bind()
    grouped: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in _conclusion_rows():
        grouped[(row["subject_type"], str(row["subject_id"]))].append(row)

    for rows in grouped.values():
        keep = rows[0]["id"]
        for duplicate in rows[1:]:
            drop = duplicate["id"]
            bind.execute(
                sa.text(
                    "UPDATE conclusion_revisions SET conclusion_state_id = :keep WHERE conclusion_state_id = :drop"
                ),
                {"keep": keep, "drop": drop},
            )
            bind.execute(
                sa.text(
                    "UPDATE verification_runs SET conclusion_state_id = :keep WHERE conclusion_state_id = :drop"
                ),
                {"keep": keep, "drop": drop},
            )
            bind.execute(
                sa.text(
                    "UPDATE decision_journals SET conclusion_state_id = :keep WHERE conclusion_state_id = :drop"
                ),
                {"keep": keep, "drop": drop},
            )
            bind.execute(
                sa.text("UPDATE theses SET conclusion_state_id = :keep WHERE conclusion_state_id = :drop"),
                {"keep": keep, "drop": drop},
            )
            bind.execute(sa.text("DELETE FROM conclusion_states WHERE id = :drop"), {"drop": drop})


def upgrade() -> None:
    _reconcile_coverage_duplicates()
    _reconcile_conclusion_duplicates()

    coverage_constraint = "uq_coverage_maps_subject"
    if coverage_constraint not in _unique_constraint_names("coverage_maps"):
        op.create_unique_constraint(
            coverage_constraint,
            "coverage_maps",
            ["subject_type", "subject_id"],
        )

    conclusion_constraint = "uq_conclusion_states_subject"
    if conclusion_constraint not in _unique_constraint_names("conclusion_states"):
        op.create_unique_constraint(
            conclusion_constraint,
            "conclusion_states",
            ["subject_type", "subject_id"],
        )


def downgrade() -> None:
    coverage_constraint = "uq_coverage_maps_subject"
    if coverage_constraint in _unique_constraint_names("coverage_maps"):
        op.drop_constraint(coverage_constraint, "coverage_maps", type_="unique")

    conclusion_constraint = "uq_conclusion_states_subject"
    if conclusion_constraint in _unique_constraint_names("conclusion_states"):
        op.drop_constraint(conclusion_constraint, "conclusion_states", type_="unique")
