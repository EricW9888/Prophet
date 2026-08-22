"""add profile_version_id to decision_journals

Revision ID: 4f2d283cbdb2
Revises: 58a15deebaf4
Create Date: 2026-05-12 16:10:37.150437

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '4f2d283cbdb2'
down_revision: Union[str, None] = '58a15deebaf4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('decision_journals', sa.Column('profile_version_id', sa.UUID(), nullable=True))


def downgrade() -> None:
    op.drop_column('decision_journals', 'profile_version_id')
