"""Enforce canonical explicit graph edges per relationship tuple.

Revision ID: 1a2c5b4d9e77
Revises: f4d7e2c9a1b3
Create Date: 2026-04-11 09:20:00.000000
"""

from __future__ import annotations

from collections import defaultdict
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "1a2c5b4d9e77"
down_revision: Union[str, None] = "f4d7e2c9a1b3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _unique_constraint_names(table_name: str) -> set[str]:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return {constraint["name"] for constraint in inspector.get_unique_constraints(table_name)}


def _edge_rows():
    bind = op.get_bind()
    return bind.execute(
        sa.text(
            """
            SELECT id, source_type, source_id, target_type, target_id, relationship_type, confidence, created_at
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
            """
        )
    ).mappings().all()


def _reconcile_edge_duplicates() -> None:
    bind = op.get_bind()
    grouped: dict[tuple[str, str, str, str, str], list[dict]] = defaultdict(list)
    for row in _edge_rows():
        grouped[
            (
                row["source_type"],
                str(row["source_id"]),
                row["target_type"],
                str(row["target_id"]),
                row["relationship_type"],
            )
        ].append(row)

    for rows in grouped.values():
        keep = rows[0]["id"]
        for duplicate in rows[1:]:
            bind.execute(sa.text("DELETE FROM edges WHERE id = :drop"), {"drop": duplicate["id"]})


def upgrade() -> None:
    _reconcile_edge_duplicates()

    edge_constraint = "uq_edges_relationship_tuple"
    if edge_constraint not in _unique_constraint_names("edges"):
        op.create_unique_constraint(
            edge_constraint,
            "edges",
            ["source_type", "source_id", "target_type", "target_id", "relationship_type"],
        )


def downgrade() -> None:
    edge_constraint = "uq_edges_relationship_tuple"
    if edge_constraint in _unique_constraint_names("edges"):
        op.drop_constraint(edge_constraint, "edges", type_="unique")
