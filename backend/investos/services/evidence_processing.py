from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import case, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from investos.config import settings
from investos.models.evidence import EvidenceProcessingState, RawEvidence, SourceItem

MEDIA_EVIDENCE_TYPES = {
    "manual_transcript",
    "video_audio_transcript",
    "video_audio_transcript_supplement",
    "video_frame_ocr",
    "video_notes",
    "video_transcript",
}
TRANSCRIPT_EVIDENCE_TYPES = {
    "manual_transcript",
    "video_audio_transcript",
    "video_audio_transcript_supplement",
    "video_transcript",
}
TERMINAL_EXTRACTION_STATUSES = {
    "completed",
    "not_applicable",
    "blocked_missing_content",
}
COMPLETED_SOURCE_ITEM_STATUSES = {
    "processed",
    "processed_adjacent_context",
    "quarantined_uncertain",
    "rejected_adjacent",
    "rejected_irrelevant",
}


@dataclass(frozen=True)
class ExtractionClaim:
    claimed: bool
    reason: str
    state: EvidenceProcessingState


class EvidenceProcessingService:
    """Own restartable processing state independently from transient job UI."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def ensure_for_evidence(
        self,
        evidence: RawEvidence,
        *,
        source_item: SourceItem | None = None,
    ) -> EvidenceProcessingState:
        values = self._initial_values(evidence, source_item=source_item)
        if evidence.is_processed and values["extraction_status"] in {
            "blocked_missing_content",
            "pending",
            "retry_scheduled",
        }:
            # Older fallback processing marked the whole receipt complete even
            # when only its transcript survived. Normalize that legacy state at
            # the lifecycle owner so the stored transcript can be enriched.
            evidence.is_processed = False
        elif not evidence.is_processed and values["extraction_status"] in {
            "completed",
            "not_applicable",
        }:
            evidence.is_processed = True
        await self.session.execute(
            pg_insert(EvidenceProcessingState)
            .values(raw_evidence_id=evidence.id, **values)
            .on_conflict_do_nothing(
                constraint="uq_evidence_processing_states_raw_evidence"
            )
        )
        return (
            await self.session.execute(
                select(EvidenceProcessingState).where(
                    EvidenceProcessingState.raw_evidence_id == evidence.id
                )
            )
        ).scalar_one()

    async def claim_extraction(
        self,
        evidence: RawEvidence,
        *,
        source_item: SourceItem | None = None,
        force: bool = False,
    ) -> ExtractionClaim:
        await self.ensure_for_evidence(evidence, source_item=source_item)
        state = (
            await self.session.execute(
                select(EvidenceProcessingState)
                .where(EvidenceProcessingState.raw_evidence_id == evidence.id)
                .with_for_update()
            )
        ).scalar_one()
        now = datetime.now(UTC)
        status = state.extraction_status

        if evidence.is_processed or status in TERMINAL_EXTRACTION_STATUSES:
            return ExtractionClaim(False, "terminal", state)
        if status == "retry_exhausted" and not force:
            return ExtractionClaim(False, "retry_exhausted", state)
        if (
            status == "retry_scheduled"
            and state.next_extraction_attempt_at is not None
            and state.next_extraction_attempt_at > now
            and not force
        ):
            return ExtractionClaim(False, "retry_not_due", state)
        stale_after = timedelta(seconds=max(settings.LLM_TIMEOUT_SECONDS * 2, 300))
        if (
            status == "running"
            and state.last_extraction_attempt_at is not None
            and state.last_extraction_attempt_at > now - stale_after
            and not force
        ):
            return ExtractionClaim(False, "already_running", state)

        previous_status = state.extraction_status
        state.extraction_status = "running"
        state.extraction_attempt_count = int(state.extraction_attempt_count or 0) + 1
        state.last_extraction_attempt_at = now
        state.next_extraction_attempt_at = None
        state.updated_at = now
        self._append_history(
            state,
            stage="structured_extraction",
            status="running",
            detail={
                "attempt": state.extraction_attempt_count,
                "resumed_from": previous_status,
            },
        )
        return ExtractionClaim(True, "claimed", state)

    async def next_due_evidence(self) -> RawEvidence | None:
        """Return the oldest evidence whose extraction stage may make progress."""

        now = datetime.now(UTC)
        stale_before = now - timedelta(
            seconds=max(settings.LLM_TIMEOUT_SECONDS * 2, 300)
        )
        return (
            (
                await self.session.execute(
                    select(RawEvidence)
                    .outerjoin(
                        EvidenceProcessingState,
                        EvidenceProcessingState.raw_evidence_id == RawEvidence.id,
                    )
                    .where(RawEvidence.is_processed.is_(False))
                    .where(RawEvidence.raw_content_ref.is_not(None))
                    .where(
                        or_(
                            EvidenceProcessingState.id.is_(None),
                            EvidenceProcessingState.extraction_status == "pending",
                            (
                                EvidenceProcessingState.extraction_status
                                == "retry_scheduled"
                            )
                            & or_(
                                EvidenceProcessingState.next_extraction_attempt_at.is_(
                                    None
                                ),
                                EvidenceProcessingState.next_extraction_attempt_at
                                <= now,
                            ),
                            (EvidenceProcessingState.extraction_status == "running")
                            & or_(
                                EvidenceProcessingState.last_extraction_attempt_at.is_(
                                    None
                                ),
                                EvidenceProcessingState.last_extraction_attempt_at
                                <= stale_before,
                            ),
                        )
                    )
                    .order_by(
                        case(
                            (
                                EvidenceProcessingState.extraction_status
                                == "retry_scheduled",
                                1,
                            ),
                            (
                                EvidenceProcessingState.extraction_status == "running",
                                2,
                            ),
                            else_=0,
                        ),
                        EvidenceProcessingState.next_extraction_attempt_at.asc().nullsfirst(),
                        RawEvidence.created_at.asc(),
                    )
                    .limit(1)
                )
            )
            .scalars()
            .first()
        )

    def record_extraction_failure(
        self,
        state: EvidenceProcessingState,
        *,
        error: str,
    ) -> str:
        now = datetime.now(UTC)
        attempts = int(state.extraction_attempt_count or 0)
        max_attempts = max(1, settings.EVIDENCE_EXTRACTION_MAX_ATTEMPTS)
        state.last_error = error
        if attempts >= max_attempts:
            state.extraction_status = "retry_exhausted"
            state.next_extraction_attempt_at = None
        else:
            delay = min(
                max(1, settings.EVIDENCE_EXTRACTION_RETRY_MAX_SECONDS),
                max(1, settings.EVIDENCE_EXTRACTION_RETRY_BASE_SECONDS)
                * (2 ** max(0, attempts - 1)),
            )
            state.extraction_status = "retry_scheduled"
            state.next_extraction_attempt_at = now + timedelta(seconds=delay)
        state.investment_object_status = "pending"
        state.updated_at = now
        self._append_history(
            state,
            stage="structured_extraction",
            status=state.extraction_status,
            message=error,
            detail={
                "attempt": attempts,
                "next_attempt_at": (
                    state.next_extraction_attempt_at.isoformat()
                    if state.next_extraction_attempt_at
                    else None
                ),
            },
        )
        return state.extraction_status

    def record_content_available(self, state: EvidenceProcessingState) -> None:
        if state.content_status == "completed":
            return
        previous_status = state.content_status
        state.content_status = "completed"
        state.updated_at = datetime.now(UTC)
        self._append_history(
            state,
            stage="content",
            status="completed",
            detail={"resumed_from": previous_status},
        )

    def record_extraction_completed(
        self,
        state: EvidenceProcessingState,
        *,
        investment_object_status: str,
        persisted_object_count: int,
    ) -> None:
        now = datetime.now(UTC)
        state.extraction_status = "completed"
        state.investment_object_status = investment_object_status
        state.persisted_object_count = max(0, persisted_object_count)
        state.next_extraction_attempt_at = None
        state.extraction_completed_at = now
        state.last_error = None
        state.updated_at = now
        self._append_history(
            state,
            stage="structured_extraction",
            status="completed",
            detail={"attempt": state.extraction_attempt_count},
        )
        self._append_history(
            state,
            stage="investment_objects",
            status=investment_object_status,
            detail={"persisted_object_count": state.persisted_object_count},
        )

    def record_not_applicable(
        self,
        state: EvidenceProcessingState,
        *,
        reason: str,
    ) -> None:
        now = datetime.now(UTC)
        state.extraction_status = "not_applicable"
        state.investment_object_status = "not_applicable"
        state.extraction_completed_at = now
        state.next_extraction_attempt_at = None
        state.updated_at = now
        self._append_history(
            state,
            stage="structured_extraction",
            status="not_applicable",
            message=reason,
        )

    def record_missing_content(
        self,
        state: EvidenceProcessingState,
        *,
        error: str,
    ) -> None:
        now = datetime.now(UTC)
        state.content_status = "missing"
        state.extraction_status = "blocked_missing_content"
        state.investment_object_status = "blocked"
        state.last_error = error
        state.next_extraction_attempt_at = None
        state.updated_at = now
        self._append_history(
            state,
            stage="content",
            status="missing",
            message=error,
        )

    async def schedule_operator_retry(self, evidence_id: UUID) -> dict[str, Any] | None:
        evidence = await self.session.get(RawEvidence, evidence_id)
        if evidence is None:
            return None
        source_item = (
            await self.session.execute(
                select(SourceItem).where(SourceItem.raw_evidence_id == evidence_id)
            )
        ).scalar_one_or_none()
        await self.ensure_for_evidence(evidence, source_item=source_item)
        state = (
            await self.session.execute(
                select(EvidenceProcessingState)
                .where(EvidenceProcessingState.raw_evidence_id == evidence_id)
                .with_for_update()
            )
        ).scalar_one()
        if evidence.is_processed or state.extraction_status in {
            "completed",
            "not_applicable",
        }:
            return {
                "scheduled": False,
                "reason": "already_complete",
                **self.summary(state),
            }
        if state.extraction_status == "running":
            return {
                "scheduled": False,
                "reason": "already_running",
                **self.summary(state),
            }
        previous_attempts = int(state.extraction_attempt_count or 0)
        state.extraction_status = "pending"
        state.extraction_attempt_count = 0
        state.next_extraction_attempt_at = datetime.now(UTC)
        state.updated_at = datetime.now(UTC)
        if source_item is not None:
            source_item.processing_status = "extraction_pending"
        self._append_history(
            state,
            stage="structured_extraction",
            status="pending",
            message="Operator scheduled a new bounded retry cycle.",
            detail={"previous_attempt_count": previous_attempts},
        )
        await self.session.commit()
        return {"scheduled": True, "reason": "operator_retry", **self.summary(state)}

    @classmethod
    def summary(cls, state: EvidenceProcessingState | None) -> dict[str, Any] | None:
        if state is None:
            return None
        status = state.extraction_status
        if status == "completed":
            overall = (
                "quarantined"
                if state.investment_object_status == "quarantined"
                else "complete"
            )
        elif status in {"retry_scheduled", "retry_exhausted", "running"}:
            overall = status
        elif state.transcript_status == "completed":
            overall = "transcript_available"
        else:
            overall = status
        next_action = {
            "retry_scheduled": "Prophet will retry structured extraction when due.",
            "retry_exhausted": "Schedule another retry after the provider is healthy.",
            "running": "Structured extraction is currently running.",
            "pending": "Structured extraction is waiting to run.",
            "blocked_missing_content": "Restore the stored source content before retrying.",
        }.get(status)
        return {
            "overall_status": overall,
            "content_status": state.content_status,
            "transcript_status": state.transcript_status,
            "extraction_status": state.extraction_status,
            "investment_object_status": state.investment_object_status,
            "cleanup_status": state.cleanup_status,
            "extraction_attempt_count": int(state.extraction_attempt_count or 0),
            "next_extraction_attempt_at": state.next_extraction_attempt_at,
            "last_extraction_attempt_at": state.last_extraction_attempt_at,
            "extraction_completed_at": state.extraction_completed_at,
            "persisted_object_count": int(state.persisted_object_count or 0),
            "last_error": state.last_error,
            "next_action": next_action,
            "history": list(state.history_json or []),
        }

    @classmethod
    def _initial_values(
        cls,
        evidence: RawEvidence,
        *,
        source_item: SourceItem | None,
    ) -> dict[str, Any]:
        metadata = (
            evidence.metadata_json if isinstance(evidence.metadata_json, dict) else {}
        )
        is_media = evidence.source_item_type in MEDIA_EVIDENCE_TYPES or bool(
            metadata.get("media_asset_id")
        )
        source_status = str(getattr(source_item, "processing_status", "") or "")
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
            or (not evidence.raw_content_ref and not evidence.is_processed)
        )
        extraction_status = "pending"
        skip_extraction = (
            evidence.source_item_type == "conversation_turn"
            or metadata.get("skip_extraction") is True
        )
        if skip_extraction:
            extraction_status = "not_applicable"
        elif legacy_degraded:
            extraction_status = "retry_scheduled"
        elif source_status == "missing_raw_content":
            extraction_status = "blocked_missing_content"
        elif source_status in {"extraction_retry_exhausted", "retry_exhausted"}:
            extraction_status = "retry_exhausted"
        elif evidence.is_processed or source_status in COMPLETED_SOURCE_ITEM_STATUSES:
            extraction_status = "completed"
        elif known_missing_content:
            extraction_status = "blocked_missing_content"

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

        now = datetime.now(UTC)
        return {
            "content_status": (
                "missing"
                if known_missing_content or not evidence.raw_content_ref
                else "completed"
            ),
            "transcript_status": (
                "completed"
                if evidence.source_item_type in TRANSCRIPT_EVIDENCE_TYPES
                else "not_applicable"
            ),
            "extraction_status": extraction_status,
            "investment_object_status": object_status,
            "cleanup_status": cleanup_status,
            "extraction_attempt_count": (1 if legacy_degraded else 0),
            "next_extraction_attempt_at": (now if legacy_degraded else None),
            "extraction_completed_at": (
                now if extraction_status in {"completed", "not_applicable"} else None
            ),
            "persisted_object_count": 0,
            "history_json": [],
            "created_at": now,
            "updated_at": now,
        }

    @staticmethod
    def _append_history(
        state: EvidenceProcessingState,
        *,
        stage: str,
        status: str,
        message: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        event = {
            "stage": stage,
            "status": status,
            "created_at": datetime.now(UTC).isoformat(),
        }
        if message:
            event["message"] = message
        if detail:
            event["detail"] = detail
        state.history_json = [*(state.history_json or []), event][-100:]
