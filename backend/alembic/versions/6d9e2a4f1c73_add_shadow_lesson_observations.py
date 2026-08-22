"""Add auditable shadow lesson observations and maturity state.

Revision ID: 6d9e2a4f1c73
Revises: 2b7c9d4e6f10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "6d9e2a4f1c73"
down_revision: str | None = "2b7c9d4e6f10"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "lessons",
        sa.Column("experiment_family_id", sa.UUID(), nullable=True),
    )
    op.add_column(
        "lessons",
        sa.Column(
            "maturity_status", sa.String(), server_default="active", nullable=False
        ),
    )
    op.add_column(
        "lessons",
        sa.Column("confidence_score", sa.Float(), server_default="0", nullable=False),
    )
    op.add_column(
        "lessons",
        sa.Column(
            "supporting_observations", sa.Integer(), server_default="0", nullable=False
        ),
    )
    op.add_column(
        "lessons",
        sa.Column(
            "contradicting_observations",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
    )
    op.add_column(
        "lessons",
        sa.Column(
            "neutral_observations", sa.Integer(), server_default="0", nullable=False
        ),
    )
    op.add_column(
        "lessons",
        sa.Column("last_validated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "lessons",
        sa.Column("stale_after", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "lessons",
        sa.Column(
            "metadata_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
    op.create_foreign_key(
        "fk_lessons_experiment_family_id",
        "lessons",
        "experiment_family_states",
        ["experiment_family_id"],
        ["id"],
    )
    op.create_index(
        "ix_lessons_experiment_family_id",
        "lessons",
        ["experiment_family_id"],
    )
    op.create_index("ix_lessons_maturity_status", "lessons", ["maturity_status"])
    op.create_index("ix_lessons_stale_after", "lessons", ["stale_after"])

    op.create_table(
        "lesson_observations",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("lesson_id", sa.UUID(), nullable=False),
        sa.Column("experiment_result_id", sa.UUID(), nullable=False),
        sa.Column("relationship", sa.String(), nullable=False),
        sa.Column("observed_alpha", sa.Float(), nullable=False),
        sa.Column("rationale", sa.String(), nullable=False),
        sa.Column(
            "evidence_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["experiment_result_id"], ["experiment_results.id"]),
        sa.ForeignKeyConstraint(["lesson_id"], ["lessons.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "lesson_id",
            "experiment_result_id",
            name="uq_lesson_observation_result",
        ),
        sa.UniqueConstraint("experiment_result_id"),
    )
    op.create_index(
        "ix_lesson_observations_lesson_id",
        "lesson_observations",
        ["lesson_id"],
    )
    op.create_index(
        "ix_lesson_observations_experiment_result_id",
        "lesson_observations",
        ["experiment_result_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_lesson_observations_experiment_result_id",
        table_name="lesson_observations",
    )
    op.drop_index("ix_lesson_observations_lesson_id", table_name="lesson_observations")
    op.drop_table("lesson_observations")

    op.drop_index("ix_lessons_stale_after", table_name="lessons")
    op.drop_index("ix_lessons_maturity_status", table_name="lessons")
    op.drop_index("ix_lessons_experiment_family_id", table_name="lessons")
    op.drop_constraint(
        "fk_lessons_experiment_family_id",
        "lessons",
        type_="foreignkey",
    )
    op.drop_column("lessons", "metadata_json")
    op.drop_column("lessons", "stale_after")
    op.drop_column("lessons", "last_validated_at")
    op.drop_column("lessons", "neutral_observations")
    op.drop_column("lessons", "contradicting_observations")
    op.drop_column("lessons", "supporting_observations")
    op.drop_column("lessons", "confidence_score")
    op.drop_column("lessons", "maturity_status")
    op.drop_column("lessons", "experiment_family_id")
