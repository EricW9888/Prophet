"""Add durable evidence processing lifecycle state.

Revision ID: 4b8d2e6f9a10
Revises: 3f6a8c2d1b74
Create Date: 2026-09-08 12:00:00.000000
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "4b8d2e6f9a10"
down_revision: Union[str, None] = "3f6a8c2d1b74"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_MEDIA_TYPES = {
    "manual_transcript",
    "video_audio_transcript",
    "video_audio_transcript_supplement",
    "video_frame_ocr",
    "video_notes",
    "video_transcript",
}
_TRANSCRIPT_TYPES = {
    "manual_transcript",
    "video_audio_transcript",
    "video_audio_transcript_supplement",
    "video_transcript",
}
_COMPLETED_SOURCE_ITEM_STATUSES = {
    "processed",
    "processed_adjacent_context",
    "quarantined_uncertain",
    "rejected_adjacent",
    "rejected_irrelevant",
}


def upgrade() -> None:
    op.create_table(
        "evidence_processing_states",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("raw_evidence_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("content_status", sa.String(), nullable=False),
        sa.Column("transcript_status", sa.String(), nullable=False),
        sa.Column("extraction_status", sa.String(), nullable=False),
        sa.Column("investment_object_status", sa.String(), nullable=False),
        sa.Column("cleanup_status", sa.String(), nullable=False),
        sa.Column("extraction_attempt_count", sa.Integer(), nullable=False),
        sa.Column(
            "next_extraction_attempt_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column(
            "last_extraction_attempt_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column("extraction_completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("persisted_object_count", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "history_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["raw_evidence_id"], ["raw_evidence.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "raw_evidence_id", name="uq_evidence_processing_states_raw_evidence"
        ),
    )
    op.create_index(
        "ix_evidence_processing_states_extraction_status",
        "evidence_processing_states",
        ["extraction_status"],
        unique=False,
    )
    op.create_index(
        "ix_evidence_processing_states_next_extraction_attempt_at",
        "evidence_processing_states",
        ["next_extraction_attempt_at"],
        unique=False,
    )
    _backfill_existing_evidence(op.get_bind())


def _backfill_existing_evidence(connection) -> None:
    rows = connection.execute(sa.text("""
            SELECT
                evidence.id,
                evidence.source_item_type,
                evidence.raw_content_ref,
                evidence.metadata_json,
                evidence.is_processed,
                item.processing_status
            FROM raw_evidence AS evidence
            LEFT JOIN source_items AS item
              ON item.raw_evidence_id = evidence.id
            ORDER BY evidence.created_at ASC
            """)).mappings()
    now = datetime.now(UTC)
    batch: list[dict] = []
    retry_ids: list[uuid.UUID] = []
    blocked_unprocessed_ids: list[uuid.UUID] = []
    completed_ids: list[uuid.UUID] = []
    for row in rows:
        metadata = (
            row["metadata_json"] if isinstance(row["metadata_json"], dict) else {}
        )
        item_type = str(row["source_item_type"] or "")
        source_status = str(row["processing_status"] or "")
        processed = bool(row["is_processed"])
        is_media = item_type in _MEDIA_TYPES or bool(metadata.get("media_asset_id"))
        legacy_degraded = (
            source_status
            in {
                "extraction_deferred",
                "extraction_retry_scheduled",
                "processed_with_fallback",
            }
            or metadata.get("extraction_degraded") is True
            or metadata.get("knowledge_promotion_status") == "deferred"
        )
        known_missing_content = (
            source_status == "missing_raw_content"
            or metadata.get("storage_missing") is True
            or (not row["raw_content_ref"] and not processed)
        )
        skip_extraction = (
            item_type == "conversation_turn" or metadata.get("skip_extraction") is True
        )

        if skip_extraction:
            extraction_status = "not_applicable"
        elif legacy_degraded:
            extraction_status = "retry_scheduled"
        elif source_status == "missing_raw_content":
            extraction_status = "blocked_missing_content"
        elif source_status in {"extraction_retry_exhausted", "retry_exhausted"}:
            extraction_status = "retry_exhausted"
        elif processed or source_status in _COMPLETED_SOURCE_ITEM_STATUSES:
            extraction_status = "completed"
        elif known_missing_content:
            extraction_status = "blocked_missing_content"
        else:
            extraction_status = "pending"

        if extraction_status == "not_applicable":
            object_status = "not_applicable"
        elif extraction_status == "blocked_missing_content":
            object_status = "blocked"
        elif extraction_status == "completed" and source_status in {
            "rejected_adjacent",
            "rejected_irrelevant",
        }:
            object_status = "quarantined"
        elif extraction_status == "completed":
            object_status = "completed"
        else:
            object_status = "pending"

        representation = str(metadata.get("representation") or "")
        if not is_media:
            cleanup_status = "not_applicable"
        elif metadata.get("raw_media_persisted") is False:
            cleanup_status = "completed"
        elif representation in {"caption_transcript", "manual_transcript"}:
            cleanup_status = "not_required"
        else:
            cleanup_status = "completed"

        batch.append(
            {
                "id": uuid.uuid4(),
                "raw_evidence_id": row["id"],
                "content_status": (
                    "missing"
                    if known_missing_content or not row["raw_content_ref"]
                    else "completed"
                ),
                "transcript_status": (
                    "completed" if item_type in _TRANSCRIPT_TYPES else "not_applicable"
                ),
                "extraction_status": extraction_status,
                "investment_object_status": object_status,
                "cleanup_status": cleanup_status,
                "extraction_attempt_count": (1 if legacy_degraded else 0),
                "next_extraction_attempt_at": (
                    now if extraction_status == "retry_scheduled" else None
                ),
                "last_extraction_attempt_at": None,
                "extraction_completed_at": (
                    now
                    if extraction_status in {"completed", "not_applicable"}
                    else None
                ),
                "persisted_object_count": 0,
                "last_error": None,
                "history_json": [],
                "created_at": now,
                "updated_at": now,
            }
        )
        if extraction_status == "retry_scheduled":
            retry_ids.append(row["id"])
        elif extraction_status == "blocked_missing_content" and processed:
            blocked_unprocessed_ids.append(row["id"])
        elif extraction_status in {"completed", "not_applicable"} and not processed:
            completed_ids.append(row["id"])
        if len(batch) >= 1000:
            _insert_batch(connection, batch)
            _mark_legacy_evidence_retryable(connection, retry_ids)
            _mark_evidence_unprocessed(connection, blocked_unprocessed_ids)
            _mark_evidence_processed(connection, completed_ids)
            batch = []
            retry_ids = []
            blocked_unprocessed_ids = []
            completed_ids = []
    if batch:
        _insert_batch(connection, batch)
        _mark_legacy_evidence_retryable(connection, retry_ids)
        _mark_evidence_unprocessed(connection, blocked_unprocessed_ids)
        _mark_evidence_processed(connection, completed_ids)


def _insert_batch(connection, rows: list[dict]) -> None:
    table = sa.table(
        "evidence_processing_states",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("raw_evidence_id", postgresql.UUID(as_uuid=True)),
        sa.column("content_status", sa.String()),
        sa.column("transcript_status", sa.String()),
        sa.column("extraction_status", sa.String()),
        sa.column("investment_object_status", sa.String()),
        sa.column("cleanup_status", sa.String()),
        sa.column("extraction_attempt_count", sa.Integer()),
        sa.column("next_extraction_attempt_at", sa.DateTime(timezone=True)),
        sa.column("last_extraction_attempt_at", sa.DateTime(timezone=True)),
        sa.column("extraction_completed_at", sa.DateTime(timezone=True)),
        sa.column("persisted_object_count", sa.Integer()),
        sa.column("last_error", sa.Text()),
        sa.column("history_json", postgresql.JSONB()),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    connection.execute(table.insert(), rows)


def _mark_legacy_evidence_retryable(connection, evidence_ids: list[uuid.UUID]) -> None:
    if not evidence_ids:
        return
    _mark_evidence_unprocessed(connection, evidence_ids)
    source_items = sa.table(
        "source_items",
        sa.column("raw_evidence_id", postgresql.UUID(as_uuid=True)),
        sa.column("processing_status", sa.String()),
    )
    connection.execute(
        source_items.update()
        .where(source_items.c.raw_evidence_id.in_(evidence_ids))
        .values(processing_status="extraction_retry_scheduled")
    )


def _mark_evidence_unprocessed(connection, evidence_ids: list[uuid.UUID]) -> None:
    if not evidence_ids:
        return
    raw_evidence = sa.table(
        "raw_evidence",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("is_processed", sa.Boolean()),
    )
    connection.execute(
        raw_evidence.update()
        .where(raw_evidence.c.id.in_(evidence_ids))
        .values(is_processed=False)
    )


def _mark_evidence_processed(connection, evidence_ids: list[uuid.UUID]) -> None:
    if not evidence_ids:
        return
    raw_evidence = sa.table(
        "raw_evidence",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("is_processed", sa.Boolean()),
    )
    connection.execute(
        raw_evidence.update()
        .where(raw_evidence.c.id.in_(evidence_ids))
        .values(is_processed=True)
    )


def downgrade() -> None:
    op.drop_index(
        "ix_evidence_processing_states_next_extraction_attempt_at",
        table_name="evidence_processing_states",
    )
    op.drop_index(
        "ix_evidence_processing_states_extraction_status",
        table_name="evidence_processing_states",
    )
    op.drop_table("evidence_processing_states")
