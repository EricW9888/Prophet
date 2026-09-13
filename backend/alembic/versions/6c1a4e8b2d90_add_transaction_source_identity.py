"""Add durable transaction source identity.

Revision ID: 6c1a4e8b2d90
Revises: 4b8d2e6f9a10
Create Date: 2026-09-09 15:00:00.000000
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from typing import Sequence, Union
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from alembic import op

revision: str = "6c1a4e8b2d90"
down_revision: Union[str, None] = "4b8d2e6f9a10"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_MAX_IDENTITY_LENGTH = 512


def _clean_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _source_identity(provenance: object) -> str | None:
    payload = provenance if isinstance(provenance, dict) else {}
    source_type = _clean_text(payload.get("source_type") or payload.get("source"))
    external_id = _clean_text(payload.get("external_id"))
    if source_type is not None and external_id is not None:
        identity = f"{source_type.casefold()}:{external_id}"
        if len(identity) <= _MAX_IDENTITY_LENGTH:
            return identity
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        return f"{source_type.casefold()}:sha256:{digest}"

    raw_evidence_id = payload.get("raw_evidence_id") or payload.get("evidence_id")
    if raw_evidence_id:
        try:
            return f"evidence:{UUID(str(raw_evidence_id))}"
        except ValueError:
            pass
    return None


def _backfill_unique_source_identities(bind: Connection) -> None:
    rows = bind.execute(
        sa.text("SELECT id, provenance_json FROM transactions")
    ).mappings()
    grouped: dict[str, list[object]] = defaultdict(list)
    for row in rows:
        identity = _source_identity(row["provenance_json"])
        if identity is not None:
            grouped[identity].append(row["id"])

    # Ambiguous historical duplicates remain unclaimed for explicit repair.
    # A nullable unique column protects every unambiguous and future source item.
    for identity, transaction_ids in grouped.items():
        if len(transaction_ids) != 1:
            continue
        bind.execute(
            sa.text(
                "UPDATE transactions SET source_identity = :identity WHERE id = :id"
            ),
            {"identity": identity, "id": transaction_ids[0]},
        )


def upgrade() -> None:
    op.add_column(
        "transactions",
        sa.Column("source_identity", sa.String(length=512), nullable=True),
    )
    _backfill_unique_source_identities(op.get_bind())
    op.create_unique_constraint(
        "uq_transactions_source_identity",
        "transactions",
        ["source_identity"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_transactions_source_identity", "transactions", type_="unique"
    )
    op.drop_column("transactions", "source_identity")
