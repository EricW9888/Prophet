from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import and_, delete, desc, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from investos.core.llm import call_llm_json
from investos.models.catalog import (
    SourceClaimRecord,
    SourcePerformanceHistory,
    SourceProfile,
    SourceTrustProfile,
    SourceValueProfile,
)
from investos.models.entity import Entity, Security
from investos.models.evidence import RawEvidence, SourceItem
from investos.models.graph import Edge
from investos.models.knowledge import Claim, Event, Fact
from investos.models.lesson import Lesson
from investos.models.portfolio import Position
from investos.models.source import Source, SourceQualitySegment
from investos.schemas.source import SourceCreate, SourceUpdate
from investos.services.knowledge_audit import KnowledgeAuditService
from investos.services.source_claim_policy import (
    source_claim_due_at,
    source_claim_priority,
)

USER_FEEDBACK_RATINGS = {"useful", "not_useful"}
SOURCE_CLAIM_ASSESSMENTS = {
    "pending",
    "correct",
    "incorrect",
    "partially_correct",
    "indeterminate",
}
SCORED_SOURCE_CLAIM_ASSESSMENTS = {"correct", "incorrect", "partially_correct"}
NOTE_SOURCE_ITEM_TYPES = {
    "manual_note",
    "user_note",
    "research_note",
    "cagr_test",
    "manual_transcript",
    "video_notes",
}
SOURCE_FEEDBACK_LESSON_TYPE = "source_reliability"
SOURCE_FEEDBACK_LESSON_TAG = "source_feedback_evidence_id"
SOURCE_CLAIM_AUTO_ASSESS_LIMIT = 5
SOURCE_CLAIM_ASSESSMENT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "assessment": {
            "type": "string",
            "enum": ["correct", "incorrect", "partially_correct", "indeterminate"],
        },
        "confidence": {"type": "number"},
        "rationale": {"type": "string"},
        "limitations": {"type": "string"},
        "assessment_evidence_ids": {
            "type": "array",
            "items": {"type": "string"},
        },
    },
    "required": [
        "assessment",
        "confidence",
        "rationale",
        "limitations",
        "assessment_evidence_ids",
    ],
}


def _bounded_excerpt(value: str | None, limit: int = 1200) -> str | None:
    if not value:
        return None
    text = re.sub(r"\s+", " ", value).strip()
    if not text:
        return None
    return text if len(text) <= limit else f"{text[:limit].rstrip()}..."


class SourceService:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def list_sources(self) -> list[dict]:
        await self._consolidate_duplicate_sources()
        sources = list(
            (
                await self.session.execute(
                    select(Source)
                    .options(selectinload(Source.quality_segments))
                    .order_by(desc(Source.is_trusted), desc(Source.updated_at))
                )
            )
            .scalars()
            .all()
        )
        source_ids = [source.id for source in sources]
        source_by_id = {source.id: source for source in sources}
        if not source_ids:
            return []

        evidence_counts = {
            source_id: count
            for source_id, count in (
                await self.session.execute(
                    select(RawEvidence.source_id, func.count(RawEvidence.id))
                    .where(RawEvidence.source_id.in_(source_ids))
                    .group_by(RawEvidence.source_id)
                )
            ).all()
        }
        trust_profiles = {
            profile.source_id: profile
            for profile in (
                await self.session.execute(
                    select(SourceTrustProfile).where(
                        SourceTrustProfile.source_id.in_(source_ids)
                    )
                )
            )
            .scalars()
            .all()
        }
        value_profiles = {
            profile.source_id: profile
            for profile in (
                await self.session.execute(
                    select(SourceValueProfile).where(
                        SourceValueProfile.source_id.in_(source_ids)
                    )
                )
            )
            .scalars()
            .all()
        }
        performance_rows = (
            (
                await self.session.execute(
                    select(SourcePerformanceHistory)
                    .where(SourcePerformanceHistory.source_id.in_(source_ids))
                    .order_by(desc(SourcePerformanceHistory.computed_at))
                )
            )
            .scalars()
            .all()
        )
        performance_by_source: dict[UUID, list[SourcePerformanceHistory]] = {}
        for history in performance_rows:
            bucket = performance_by_source.setdefault(history.source_id, [])
            if len(bucket) >= 3:
                continue
            bucket.append(history)
        claim_queue_rows = (
            await self.session.execute(
                select(
                    SourceClaimRecord.source_id,
                    func.count(SourceClaimRecord.id).label("total"),
                    func.count(SourceClaimRecord.id)
                    .filter(SourceClaimRecord.assessment == "pending")
                    .label("pending"),
                    func.count(SourceClaimRecord.id)
                    .filter(
                        and_(
                            SourceClaimRecord.assessment == "pending",
                            SourceClaimRecord.next_assessment_at > datetime.now(UTC),
                        )
                    )
                    .label("deferred"),
                    func.count(SourceClaimRecord.id)
                    .filter(SourceClaimRecord.assessment != "pending")
                    .label("assessed"),
                    func.max(SourceClaimRecord.assessment_time).label(
                        "last_assessment_at"
                    ),
                )
                .where(SourceClaimRecord.source_id.in_(source_ids))
                .group_by(SourceClaimRecord.source_id)
            )
        ).all()
        claim_queue_by_source = {
            row.source_id: {
                "total": int(row.total or 0),
                "pending": int(row.pending or 0),
                "deferred": int(row.deferred or 0),
                "assessed": int(row.assessed or 0),
                "last_assessment_at": row.last_assessment_at,
            }
            for row in claim_queue_rows
        }
        recent_rows = (
            (
                await self.session.execute(
                    select(RawEvidence)
                    .where(RawEvidence.source_id.in_(source_ids))
                    .order_by(desc(RawEvidence.created_at))
                )
            )
            .scalars()
            .all()
        )
        recent_items_by_source: dict[UUID, list[dict]] = {}
        for evidence in recent_rows:
            bucket = recent_items_by_source.setdefault(evidence.source_id, [])
            if len(bucket) >= 3:
                continue
            source = source_by_id.get(evidence.source_id)
            origin_summary = (
                self._evidence_origin_summary(evidence, source)
                if source is not None
                else {
                    "origin_kind": "catalog",
                    "origin_label": "Source catalog",
                    "origin_detail": None,
                }
            )
            bucket.append(
                {
                    "id": evidence.id,
                    "title": evidence.title,
                    "url": evidence.url,
                    "created_at": evidence.created_at,
                    "source_item_type": evidence.source_item_type,
                    "is_processed": evidence.is_processed,
                    **origin_summary,
                    "user_feedback": (evidence.metadata_json or {}).get(
                        "user_feedback"
                    ),
                }
            )

        rows: list[dict] = []
        for source in sources:
            trust = trust_profiles.get(source.id)
            value = value_profiles.get(source.id)
            rows.append(
                {
                    "id": source.id,
                    "name": source.name,
                    "source_type": source.source_type,
                    "url": source.url,
                    "description": source.description,
                    "is_trusted": source.is_trusted,
                    "origin": self._source_origin_summary(source),
                    "evidence_count": int(evidence_counts.get(source.id, 0)),
                    "trust_profile": (
                        None
                        if trust is None
                        else {
                            "factual_reliability": trust.factual_reliability,
                            "noise_ratio": trust.noise_ratio,
                            "trust_trajectory": trust.trust_trajectory,
                            "correction_quality": trust.correction_quality,
                        }
                    ),
                    "value_profile": (
                        None
                        if value is None
                        else {
                            "idea_generation_value": value.idea_generation_value,
                            "timing_value": value.timing_value,
                            "portfolio_relevance_value": value.portfolio_relevance_value,
                            "specificity": value.specificity,
                            "originality": value.originality,
                        }
                    ),
                    "quality_segments": [
                        {
                            "domain": segment.domain,
                            "ticker": segment.ticker,
                            "horizon": segment.horizon,
                            "regime": segment.regime,
                            "quality_score": float(segment.quality_score or 0),
                            "originality_score": float(segment.originality_score or 0),
                            "timing_usefulness": float(segment.timing_usefulness or 0),
                            "evidence_count": int(segment.evidence_count or 0),
                            "notes": segment.notes,
                        }
                        for segment in sorted(
                            source.quality_segments,
                            key=lambda item: (
                                float(item.quality_score or 0),
                                float(item.timing_usefulness or 0),
                                float(item.originality_score or 0),
                            ),
                            reverse=True,
                        )[:3]
                    ],
                    "performance_history": [
                        self._performance_history_summary(history)
                        for history in performance_by_source.get(source.id, [])[:3]
                    ],
                    "claim_queue": claim_queue_by_source.get(
                        source.id,
                        {
                            "total": 0,
                            "pending": 0,
                            "deferred": 0,
                            "assessed": 0,
                            "last_assessment_at": None,
                        },
                    ),
                    "recent_items": recent_items_by_source.get(source.id, []),
                    "created_at": source.created_at,
                    "updated_at": source.updated_at,
                }
            )
        return rows

    async def list_recent_evidence(self, limit: int = 80) -> list[dict]:
        rows = (
            await self.session.execute(
                select(RawEvidence, Source)
                .join(Source, RawEvidence.source_id == Source.id)
                .order_by(desc(RawEvidence.created_at))
                .limit(max(1, min(limit, 200)))
            )
        ).all()
        return [self._evidence_summary(evidence, source) for evidence, source in rows]

    async def list_notes(self, limit: int = 80) -> list[dict]:
        rows = (
            await self.session.execute(
                select(RawEvidence, Source)
                .join(Source, RawEvidence.source_id == Source.id)
                .where(RawEvidence.source_item_type.in_(NOTE_SOURCE_ITEM_TYPES))
                .order_by(desc(RawEvidence.created_at))
                .limit(max(1, min(limit, 200)))
            )
        ).all()
        return [self._evidence_summary(evidence, source) for evidence, source in rows]

    async def get_evidence_detail(self, evidence_id: UUID) -> dict | None:
        row = (
            await self.session.execute(
                select(RawEvidence, Source, SourceItem)
                .join(Source, RawEvidence.source_id == Source.id)
                .outerjoin(SourceItem, SourceItem.raw_evidence_id == RawEvidence.id)
                .where(RawEvidence.id == evidence_id)
            )
        ).one_or_none()
        if row is None:
            return None

        evidence, source, source_item = row
        detail = self._evidence_summary(evidence, source)
        metadata = (
            evidence.metadata_json if isinstance(evidence.metadata_json, dict) else {}
        )
        detail.update(
            {
                "author": evidence.author,
                "external_id": evidence.external_id,
                "raw_content_ref": evidence.raw_content_ref,
                "event_time": evidence.event_time,
                "public_time": evidence.public_time,
                "ingest_time": evidence.ingest_time,
                "eligible_action_time": evidence.eligible_action_time,
                "metadata": metadata,
                "source_item_summary": getattr(source_item, "summary", None),
                "source_item_excerpt": _bounded_excerpt(
                    getattr(source_item, "extracted_text", None)
                ),
                "source_item_processing_status": getattr(
                    source_item, "processing_status", None
                ),
            }
        )
        return detail

    async def list_feedback(self, limit: int = 80) -> list[dict]:
        await self._ensure_feedback_lesson_links()
        rows = (
            await self.session.execute(
                select(RawEvidence, Source)
                .join(Source, RawEvidence.source_id == Source.id)
                .where(RawEvidence.metadata_json.is_not(None))
                .order_by(desc(RawEvidence.updated_at))
                .limit(1000)
            )
        ).all()
        feedback_rows: list[dict] = []
        for evidence, source in rows:
            metadata = evidence.metadata_json or {}
            feedback = metadata.get("user_feedback")
            if not isinstance(feedback, dict):
                continue
            rating = str(feedback.get("rating") or "").strip()
            if rating not in USER_FEEDBACK_RATINGS:
                continue
            feedback_rows.append(self._feedback_summary(evidence, source, feedback))
            if len(feedback_rows) >= limit:
                break
        return feedback_rows

    async def update_evidence_feedback(
        self,
        *,
        evidence_id: UUID,
        rating: str,
        note: str | None = None,
        context: str | None = None,
    ) -> dict | None:
        clean_rating = rating.strip().lower()
        if clean_rating not in USER_FEEDBACK_RATINGS:
            raise ValueError("rating_must_be_useful_or_not_useful")

        row = (
            await self.session.execute(
                select(RawEvidence, Source)
                .join(Source, RawEvidence.source_id == Source.id)
                .where(RawEvidence.id == evidence_id)
            )
        ).one_or_none()
        if row is None:
            return None
        evidence, source = row
        metadata = dict(evidence.metadata_json or {})
        previous_feedback = metadata.get("user_feedback")
        if not isinstance(previous_feedback, dict):
            previous_feedback = {}
        feedback = {
            "rating": clean_rating,
            "note": note.strip() if note else None,
            "context": context.strip() if context else None,
            "flagged_at": datetime.now(UTC).isoformat(),
        }
        lesson = await self._upsert_feedback_lesson(
            evidence=evidence,
            source=source,
            feedback=feedback,
            previous_feedback=previous_feedback,
        )
        if lesson is not None:
            feedback["lesson_id"] = str(lesson.id)
            feedback["lesson_title"] = lesson.title
        metadata["user_feedback"] = feedback
        evidence.metadata_json = metadata
        evidence.mark_updated()
        await self.session.commit()
        await self.session.refresh(evidence)
        return self._feedback_summary(evidence, source, feedback)

    async def update_claim_assessment(
        self,
        *,
        claim_record_id: UUID,
        assessment: str,
        notes: str | None = None,
        assessment_evidence: list[UUID] | None = None,
        horizon_days: int | None = None,
    ) -> dict | None:
        clean_assessment = assessment.strip().lower()
        if clean_assessment not in SOURCE_CLAIM_ASSESSMENTS:
            raise ValueError(
                "assessment_must_be_pending_correct_incorrect_partially_correct_or_indeterminate"
            )
        record = (
            await self.session.execute(
                select(SourceClaimRecord).where(SourceClaimRecord.id == claim_record_id)
            )
        ).scalar_one_or_none()
        if record is None:
            return None
        record.assessment = clean_assessment
        record.notes = notes.strip() if notes else None
        record.assessment_evidence = assessment_evidence or None
        record.horizon_days = horizon_days
        record.assessment_time = (
            None if clean_assessment == "pending" else datetime.now(UTC)
        )
        record.next_assessment_at = None
        await self.session.flush()
        history = await self.recompute_source_performance(
            record.source_id, commit=False
        )
        await self.session.commit()
        return {
            "id": record.id,
            "source_id": record.source_id,
            "claim_id": record.claim_id,
            "assessment": record.assessment,
            "assessment_time": record.assessment_time,
            "horizon_days": record.horizon_days,
            "notes": record.notes,
            "assessment_attempt_count": int(record.assessment_attempt_count or 0),
            "last_assessment_attempt_at": record.last_assessment_attempt_at,
            "next_assessment_at": record.next_assessment_at,
            "assessment_metadata": record.assessment_metadata,
            "performance_history": history,
        }

    async def propose_claim_assessment(
        self,
        *,
        claim_record_id: UUID,
        apply: bool = False,
        min_confidence: float = 0.75,
    ) -> dict | None:
        bundle = await self._source_claim_assessment_bundle(claim_record_id)
        if bundle is None:
            return None
        record: SourceClaimRecord = bundle["record"]
        claim: Claim = bundle["claim"]
        source: Source = bundle["source"]
        follow_up_evidence = bundle["follow_up_evidence"]
        if not follow_up_evidence:
            recommended_query = self._source_claim_followup_query(record, claim)
            return {
                "id": record.id,
                "source_id": record.source_id,
                "claim_id": record.claim_id,
                "assessment": "indeterminate",
                "confidence": 0.0,
                "rationale": "No later graph evidence was found for the same subject.",
                "limitations": "Needs direct follow-up evidence before source performance can be scored.",
                "assessment_evidence": [],
                "should_apply": False,
                "applied": False,
                "notes": None,
                "performance_history": None,
                "follow_up_evidence_count": 0,
                "recommended_research_query": recommended_query,
            }

        prompt_payload = {
            "task": "Assess whether an older source claim was correct using only the provided later evidence.",
            "allowed_assessments": [
                "correct",
                "partially_correct",
                "incorrect",
                "indeterminate",
            ],
            "source": {
                "id": str(source.id),
                "name": source.name,
                "source_type": source.source_type,
            },
            "claim_record": {
                "id": str(record.id),
                "claim_time": (
                    record.claim_time.isoformat() if record.claim_time else None
                ),
                "due_at": (
                    bundle["due_at"].isoformat() if bundle.get("due_at") else None
                ),
                "ticker": record.ticker,
                "domain": record.domain,
                "sector": record.sector,
                "regime": record.regime,
            },
            "original_claim": {
                "id": str(claim.id),
                "statement": claim.statement,
                "claim_type": claim.claim_type,
                "confidence": float(claim.confidence or 0.0),
                "target_horizon": claim.target_horizon,
                "importance": claim.importance,
                "contradiction_role": claim.contradiction_role,
            },
            "subjects": bundle["subjects"],
            "later_evidence": follow_up_evidence,
            "instructions": [
                "Use only later_evidence IDs from this payload as assessment_evidence_ids.",
                "Return indeterminate if evidence is adjacent, too vague, or does not directly test the original claim.",
                "Do not reward a source for being directionally plausible unless the key mechanism, timing, or magnitude is supported.",
            ],
        }
        try:
            raw = await call_llm_json(
                system_prompt=(
                    "You are Prophet's source outcome assessor. Judge source claims conservatively. "
                    "Your job is to update source reliability only when later evidence directly tests the original claim."
                ),
                user_prompt=json.dumps(prompt_payload, ensure_ascii=True, default=str),
                schema=SOURCE_CLAIM_ASSESSMENT_SCHEMA,
                timeout_seconds=12,
            )
        except Exception as exc:
            return {
                "id": record.id,
                "source_id": record.source_id,
                "claim_id": record.claim_id,
                "assessment": "indeterminate",
                "confidence": 0.0,
                "rationale": "Automated assessment provider failed.",
                "limitations": str(exc),
                "assessment_evidence": [],
                "should_apply": False,
                "applied": False,
                "notes": None,
                "performance_history": None,
                "follow_up_evidence_count": len(follow_up_evidence),
                "recommended_research_query": self._source_claim_followup_query(
                    record, claim
                ),
            }

        proposal = self._sanitize_claim_assessment_proposal(
            raw,
            allowed_evidence_ids={UUID(item["id"]) for item in follow_up_evidence},
            min_confidence=min_confidence,
        )
        notes = self._claim_assessment_notes(
            proposal=proposal,
            source=source,
            claim=claim,
            evidence_lookup={UUID(item["id"]): item for item in follow_up_evidence},
        )
        result = {
            "id": record.id,
            "source_id": record.source_id,
            "claim_id": record.claim_id,
            "assessment": proposal["assessment"],
            "confidence": proposal["confidence"],
            "rationale": proposal["rationale"],
            "limitations": proposal["limitations"],
            "assessment_evidence": proposal["assessment_evidence"],
            "should_apply": proposal["should_apply"],
            "applied": False,
            "notes": notes,
            "performance_history": None,
            "follow_up_evidence_count": len(follow_up_evidence),
            "recommended_research_query": (
                None
                if proposal["should_apply"]
                else self._source_claim_followup_query(record, claim)
            ),
        }
        if apply and proposal["should_apply"]:
            applied = await self.update_claim_assessment(
                claim_record_id=record.id,
                assessment=proposal["assessment"],
                notes=notes,
                assessment_evidence=proposal["assessment_evidence"],
                horizon_days=record.horizon_days,
            )
            result["applied"] = applied is not None
            result["performance_history"] = (
                None if applied is None else applied.get("performance_history")
            )
        return result

    async def assess_due_source_claims(
        self,
        *,
        limit: int = SOURCE_CLAIM_AUTO_ASSESS_LIMIT,
        scan_limit: int = 500,
        apply: bool = True,
        min_confidence: float = 0.75,
        retry_hours: int = 24,
        retry_share: float = 0.25,
        research_missing_evidence: bool = False,
        research_limit: int = 1,
    ) -> dict:
        clean_limit = max(1, min(limit, 20))
        clean_scan_limit = max(clean_limit, min(scan_limit, 5000))
        clean_research_limit = max(0, min(research_limit, 5))
        now = datetime.now(UTC)
        deferred_count = int(
            (
                await self.session.execute(
                    select(func.count(SourceClaimRecord.id)).where(
                        SourceClaimRecord.assessment == "pending",
                        SourceClaimRecord.next_assessment_at > now,
                    )
                )
            ).scalar_one()
            or 0
        )
        clean_retry_share = max(0.0, min(float(retry_share or 0.0), 1.0))
        retry_scan_limit = max(
            clean_limit,
            int(round(clean_scan_limit * clean_retry_share)),
        )
        fresh_scan_limit = max(clean_limit, clean_scan_limit - retry_scan_limit)
        fresh_rows = (
            await self.session.execute(
                select(SourceClaimRecord, Claim)
                .join(Claim, SourceClaimRecord.claim_id == Claim.id)
                .where(
                    SourceClaimRecord.assessment == "pending",
                    SourceClaimRecord.assessment_attempt_count == 0,
                    or_(
                        SourceClaimRecord.next_assessment_at.is_(None),
                        SourceClaimRecord.next_assessment_at <= now,
                    ),
                )
                .order_by(
                    SourceClaimRecord.claim_time.asc(),
                )
                .limit(fresh_scan_limit)
            )
        ).all()
        retry_rows = (
            await self.session.execute(
                select(SourceClaimRecord, Claim)
                .join(Claim, SourceClaimRecord.claim_id == Claim.id)
                .where(
                    SourceClaimRecord.assessment == "pending",
                    SourceClaimRecord.assessment_attempt_count > 0,
                    SourceClaimRecord.next_assessment_at <= now,
                )
                .order_by(
                    SourceClaimRecord.next_assessment_at.asc(),
                    SourceClaimRecord.claim_time.asc(),
                )
                .limit(retry_scan_limit)
            )
        ).all()
        rows = [*fresh_rows, *retry_rows]
        due_rows: list[tuple[SourceClaimRecord, Claim, datetime]] = []
        for record, claim in rows:
            due_at = source_claim_due_at(record, claim)
            if due_at is None or due_at > now:
                continue
            due_rows.append((record, claim, due_at))
        portfolio_relevance = await self._portfolio_claim_relevance(due_rows)
        due_candidates: list[tuple[SourceClaimRecord, Claim, float]] = []
        for record, claim, due_at in due_rows:
            relevance = portfolio_relevance.get(claim.id, {})
            due_candidates.append(
                (
                    record,
                    claim,
                    source_claim_priority(
                        record,
                        claim,
                        due_at,
                        now,
                        portfolio_relevant=bool(relevance.get("is_portfolio_relevant")),
                        portfolio_weight_pct=float(relevance.get("weight_pct") or 0.0),
                    ),
                )
            )
        due_pairs = self._select_fair_claim_batch(
            due_candidates,
            limit=clean_limit,
            retry_share=clean_retry_share,
        )
        selected_claim_ids = {claim.id for _record, claim in due_pairs}
        portfolio_relevant_eligible = sum(
            1
            for _record, claim, _due_at in due_rows
            if portfolio_relevance.get(claim.id, {}).get("is_portfolio_relevant")
        )
        selected_portfolio_relevant = sum(
            1
            for claim_id in selected_claim_ids
            if portfolio_relevance.get(claim_id, {}).get("is_portfolio_relevant")
        )
        results: list[dict] = []
        research_attempted = 0
        research_started = 0
        for record, claim in due_pairs:
            result = await self.propose_claim_assessment(
                claim_record_id=record.id,
                apply=apply,
                min_confidence=min_confidence,
            )
            if result is not None:
                if (
                    apply
                    and research_missing_evidence
                    and research_attempted < clean_research_limit
                    and not result.get("should_apply")
                    and result.get("recommended_research_query")
                ):
                    research_attempted += 1
                    try:
                        followup = await self._run_source_claim_followup_research(
                            record=record,
                            claim=claim,
                            query=str(result["recommended_research_query"]),
                        )
                    except Exception as exc:
                        followup = {
                            "started": False,
                            "reason": f"research_followup_failed: {exc}",
                            "evidence_id": None,
                            "processed": False,
                            "query": str(result["recommended_research_query"]),
                            "title": self._source_claim_followup_title(record, claim),
                        }
                    result["research_followup"] = followup
                    if followup.get("started"):
                        research_started += 1
                if apply and not result.get("applied"):
                    await self._record_claim_assessment_attempt(
                        record=record,
                        result=result,
                        retry_hours=retry_hours,
                    )
                results.append(result)
        return {
            "scanned": len(rows),
            "due": len(due_pairs),
            "eligible": len(due_candidates),
            "portfolio_relevant_eligible": portfolio_relevant_eligible,
            "selected_portfolio_relevant": selected_portfolio_relevant,
            "deferred": deferred_count,
            "proposed": len(results),
            "applied": sum(1 for result in results if result.get("applied")),
            "research_attempted": research_attempted,
            "research_started": research_started,
            "results": results,
        }

    async def _portfolio_claim_relevance(
        self,
        due_rows: list[tuple[SourceClaimRecord, Claim, datetime]],
    ) -> dict[UUID, dict[str, float | bool]]:
        """Map due claims to live holdings without relying on ticker-specific rules."""
        if not due_rows:
            return {}
        holding_rows = (
            await self.session.execute(
                select(
                    Entity.id,
                    Entity.name,
                    Entity.aliases,
                    Security.ticker,
                    Position.weight_pct,
                )
                .join(Security, Security.entity_id == Entity.id)
                .join(Position, Position.security_id == Security.id)
                .where(
                    Position.list_type == "holding",
                    Position.quantity != 0,
                    Security.is_active.is_(True),
                )
            )
        ).all()
        if not holding_rows:
            return {}

        entity_weights: dict[UUID, float] = {}
        label_weights: dict[str, float] = {}
        for entity_id, entity_name, aliases, ticker, weight_pct in holding_rows:
            weight = max(0.0, abs(float(weight_pct or 0.0)))
            entity_weights[entity_id] = max(entity_weights.get(entity_id, 0.0), weight)
            labels = [entity_name, ticker, *(aliases or [])]
            for label in labels:
                normalized = self._normalized_subject_label(label)
                if normalized:
                    label_weights[normalized] = max(
                        label_weights.get(normalized, 0.0), weight
                    )

        claim_ids = {claim.id for _record, claim, _due_at in due_rows}
        entity_ids = set(entity_weights)
        linked_weights: dict[UUID, float] = {}
        if claim_ids and entity_ids:
            edges = (
                (
                    await self.session.execute(
                        select(Edge).where(
                            or_(
                                and_(
                                    Edge.source_type == "claim",
                                    Edge.source_id.in_(claim_ids),
                                    Edge.target_type == "entity",
                                    Edge.target_id.in_(entity_ids),
                                ),
                                and_(
                                    Edge.target_type == "claim",
                                    Edge.target_id.in_(claim_ids),
                                    Edge.source_type == "entity",
                                    Edge.source_id.in_(entity_ids),
                                ),
                            )
                        )
                    )
                )
                .scalars()
                .all()
            )
            for edge in edges:
                if edge.source_type == "claim":
                    claim_id, entity_id = edge.source_id, edge.target_id
                else:
                    claim_id, entity_id = edge.target_id, edge.source_id
                linked_weights[claim_id] = max(
                    linked_weights.get(claim_id, 0.0),
                    entity_weights.get(entity_id, 0.0),
                )

        relevance: dict[UUID, dict[str, float | bool]] = {
            claim_id: {
                "is_portfolio_relevant": True,
                "weight_pct": weight,
            }
            for claim_id, weight in linked_weights.items()
        }
        for record, claim, _due_at in due_rows:
            if claim.id in relevance:
                continue
            matched_weight = max(
                (
                    label_weights.get(candidate, 0.0)
                    for candidate in self._subject_label_candidates(record.ticker)
                ),
                default=0.0,
            )
            if matched_weight > 0.0:
                relevance[claim.id] = {
                    "is_portfolio_relevant": True,
                    "weight_pct": matched_weight,
                }
        return relevance

    @staticmethod
    def _select_fair_claim_batch(
        candidates: list[tuple[SourceClaimRecord, Claim, float]],
        *,
        limit: int,
        retry_share: float,
    ) -> list[tuple[SourceClaimRecord, Claim]]:
        if not candidates or limit <= 0:
            return []
        fresh = sorted(
            (
                item
                for item in candidates
                if int(getattr(item[0], "assessment_attempt_count", 0) or 0) == 0
            ),
            key=lambda item: item[2],
            reverse=True,
        )
        retries = sorted(
            (
                item
                for item in candidates
                if int(getattr(item[0], "assessment_attempt_count", 0) or 0) > 0
            ),
            key=lambda item: item[2],
            reverse=True,
        )
        if not fresh:
            selected = retries[:limit]
        elif not retries:
            selected = fresh[:limit]
        else:
            retry_slots = (
                0
                if retry_share <= 0 or limit == 1
                else min(
                    len(retries),
                    max(1, min(limit - 1, int(round(limit * retry_share)))),
                )
            )
            fresh_slots = min(len(fresh), limit - retry_slots)
            selected = [*fresh[:fresh_slots], *retries[:retry_slots]]
            remaining = limit - len(selected)
            if remaining > 0:
                overflow = [*fresh[fresh_slots:], *retries[retry_slots:]]
                selected.extend(
                    sorted(overflow, key=lambda item: item[2], reverse=True)[:remaining]
                )
        return [(record, claim) for record, claim, _priority in selected]

    async def _record_claim_assessment_attempt(
        self,
        *,
        record: SourceClaimRecord,
        result: dict,
        retry_hours: int,
    ) -> None:
        now = datetime.now(UTC)
        retry_delay = timedelta(hours=max(1, min(int(retry_hours or 24), 24 * 30)))
        next_assessment_at = now + retry_delay
        record.assessment_attempt_count = int(record.assessment_attempt_count or 0) + 1
        record.last_assessment_attempt_at = now
        record.next_assessment_at = next_assessment_at
        record.assessment_metadata = {
            "attempt_count": record.assessment_attempt_count,
            "attempted_at": now.isoformat(),
            "next_assessment_at": next_assessment_at.isoformat(),
            "assessment": result.get("assessment"),
            "confidence": float(result.get("confidence") or 0.0),
            "rationale": _bounded_excerpt(result.get("rationale"), 1000),
            "limitations": _bounded_excerpt(result.get("limitations"), 700),
            "recommended_research_query": _bounded_excerpt(
                result.get("recommended_research_query"), 500
            ),
            "research_followup": (
                json.loads(json.dumps(result.get("research_followup"), default=str))
                if result.get("research_followup")
                else None
            ),
        }
        result["assessment_attempt_count"] = record.assessment_attempt_count
        result["last_assessment_attempt_at"] = now
        result["next_assessment_at"] = next_assessment_at
        await self.session.commit()

    async def _run_source_claim_followup_research(
        self,
        *,
        record: SourceClaimRecord,
        claim: Claim,
        query: str,
    ) -> dict:
        from investos.services.research import ResearchService

        title = self._source_claim_followup_title(record, claim)
        result = await ResearchService(self.session).run_ad_hoc_request(
            query=query,
            title=title,
            source_item_type="source_claim_followup",
            metadata_json={
                "trigger": "source_claim_assessment_followup",
                "source_claim_record_id": str(record.id),
                "source_id": str(record.source_id),
                "claim_id": str(claim.id),
                "claim_statement": _bounded_excerpt(claim.statement, 1000),
                "claim_time": (
                    record.claim_time.isoformat() if record.claim_time else None
                ),
                "ticker": record.ticker,
                "domain": record.domain,
                "sector": record.sector,
                "regime": record.regime,
                "query": query[:400],
            },
            process_after_ingest=False,
        )
        return {
            "started": result.started,
            "reason": result.reason,
            "evidence_id": result.evidence_id,
            "processed": result.processed,
            "query": result.query,
            "title": result.title,
        }

    async def recompute_source_performance(
        self,
        source_id: UUID,
        *,
        commit: bool = True,
    ) -> dict | None:
        source = await self.get_source(source_id)
        if source is None:
            return None
        records = (
            (
                await self.session.execute(
                    select(SourceClaimRecord)
                    .where(
                        SourceClaimRecord.source_id == source_id,
                        SourceClaimRecord.assessment.in_(
                            SCORED_SOURCE_CLAIM_ASSESSMENTS
                        ),
                        SourceClaimRecord.assessment_time.is_not(None),
                    )
                    .order_by(SourceClaimRecord.claim_time.asc())
                )
            )
            .scalars()
            .all()
        )
        payload = self._performance_history_payload(records)
        await self.session.execute(
            delete(SourcePerformanceHistory).where(
                SourcePerformanceHistory.source_id == source_id,
                SourcePerformanceHistory.domain.is_(None),
                SourcePerformanceHistory.sector.is_(None),
                SourcePerformanceHistory.regime.is_(None),
            )
        )
        if payload is None:
            if commit:
                await self.session.commit()
            return None
        history = SourcePerformanceHistory(source_id=source_id, **payload)
        self.session.add(history)
        await self.session.flush()
        await self._apply_performance_to_trust_profiles(source, history)
        if commit:
            await self.session.commit()
            await self.session.refresh(history)
        return self._performance_history_summary(history)

    async def clear_evidence_feedback(self, evidence_id: UUID) -> bool:
        evidence = (
            await self.session.execute(
                select(RawEvidence).where(RawEvidence.id == evidence_id)
            )
        ).scalar_one_or_none()
        if evidence is None:
            return False
        metadata = dict(evidence.metadata_json or {})
        feedback = metadata.pop("user_feedback", None)
        if isinstance(feedback, dict):
            lesson = await self._lesson_from_feedback(feedback)
            if lesson is not None:
                await self.session.delete(lesson)
        evidence.metadata_json = metadata or None
        evidence.mark_updated()
        await self.session.commit()
        return True

    async def get_source(self, source_id: UUID) -> Source | None:
        return (
            await self.session.execute(select(Source).where(Source.id == source_id))
        ).scalar_one_or_none()

    async def create_source(self, payload: SourceCreate) -> dict:
        existing = await self._find_duplicate_source(
            name=payload.name,
            source_type=payload.source_type,
            url=payload.url,
        )
        if existing:
            updated = False
            cleaned_url = self._normalize_source_url(payload.url)
            cleaned_description = (
                payload.description.strip() if payload.description else None
            )
            cleaned_name = payload.name.strip()
            existing_name = existing.name.strip()
            if (
                cleaned_name
                and self._normalize_source_name(cleaned_name)
                != self._normalize_source_name(existing_name)
                and len(cleaned_name) > len(existing_name)
            ):
                existing.name = cleaned_name
                updated = True
            if cleaned_url and not existing.url:
                existing.url = cleaned_url
                updated = True
            if cleaned_description and not existing.description:
                existing.description = cleaned_description
                updated = True
            if payload.is_trusted and not existing.is_trusted:
                existing.is_trusted = True
                updated = True
            if updated:
                await self.session.commit()
                await self.session.refresh(existing)
            return await self._source_response_by_id(existing.id)
        source = Source(
            name=payload.name.strip(),
            source_type=payload.source_type.strip(),
            url=self._normalize_source_url(payload.url),
            description=payload.description.strip() if payload.description else None,
            is_trusted=payload.is_trusted,
        )
        self.session.add(source)
        await self.session.commit()
        await self.session.refresh(source)
        return await self._source_response_by_id(source.id)

    async def update_source(
        self, source_id: UUID, payload: SourceUpdate
    ) -> dict | None:
        source = await self.get_source(source_id)
        if not source:
            return None
        if payload.name is not None:
            source.name = payload.name.strip()
        if payload.source_type is not None:
            source.source_type = payload.source_type.strip()
        if payload.url is not None:
            source.url = self._normalize_source_url(payload.url)
        if payload.description is not None:
            source.description = payload.description.strip() or None
        if payload.is_trusted is not None:
            source.is_trusted = payload.is_trusted
        duplicate = await self._find_duplicate_source(
            name=source.name,
            source_type=source.source_type,
            url=source.url,
            exclude_id=source.id,
        )
        if duplicate:
            merged = await self._merge_source_group([duplicate, source])
            await self.session.commit()
            await self.session.refresh(merged)
            return await self._source_response_by_id(merged.id)
        await self.session.commit()
        await self.session.refresh(source)
        return await self._source_response_by_id(source.id)

    async def _source_response_by_id(self, source_id: UUID) -> dict:
        for source in await self.list_sources():
            if source.get("id") == source_id:
                return source
        row = (
            await self.session.execute(
                select(Source)
                .options(selectinload(Source.quality_segments))
                .where(Source.id == source_id)
            )
        ).scalar_one()
        return {
            "id": row.id,
            "name": row.name,
            "source_type": row.source_type,
            "url": row.url,
            "description": row.description,
            "is_trusted": row.is_trusted,
            "origin": self._source_origin_summary(row),
            "evidence_count": 0,
            "trust_profile": None,
            "value_profile": None,
            "quality_segments": [],
            "performance_history": [],
            "recent_items": [],
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }

    @staticmethod
    def _normalize_source_name(name: str | None) -> str:
        if not name:
            return ""
        return re.sub(r"\s+", " ", name).strip().lower()

    @staticmethod
    def _normalize_source_url(url: str | None) -> str | None:
        if not url:
            return None
        cleaned = url.strip()
        if not cleaned:
            return None
        cleaned = cleaned.rstrip("/")
        if "://" in cleaned:
            scheme, remainder = cleaned.split("://", 1)
            cleaned = f"{scheme.lower()}://{remainder}"
        return cleaned

    def _canonical_key(self, source: Source) -> tuple[str, str]:
        normalized_url = self._normalize_source_url(source.url)
        if normalized_url:
            return ("url", normalized_url.lower())
        return (
            "name",
            f"{source.source_type.strip().lower()}::{self._normalize_source_name(source.name)}",
        )

    async def _find_duplicate_source(
        self,
        *,
        name: str,
        source_type: str,
        url: str | None,
        exclude_id: UUID | None = None,
    ) -> Source | None:
        sources = (
            (
                await self.session.execute(
                    select(Source).order_by(
                        desc(Source.updated_at), desc(Source.is_trusted)
                    )
                )
            )
            .scalars()
            .all()
        )
        target_url = self._normalize_source_url(url)
        target_name = self._normalize_source_name(name)
        target_type = source_type.strip().lower()
        for candidate in sources:
            if exclude_id and candidate.id == exclude_id:
                continue
            candidate_url = self._normalize_source_url(candidate.url)
            if (
                target_url
                and candidate_url
                and candidate_url.lower() == target_url.lower()
            ):
                return candidate
            if (
                not target_url
                and not candidate_url
                and candidate.source_type.strip().lower() == target_type
                and self._normalize_source_name(candidate.name) == target_name
            ):
                return candidate
        return None

    async def _consolidate_duplicate_sources(self) -> None:
        sources = (
            (
                await self.session.execute(
                    select(Source).order_by(
                        desc(Source.is_trusted), desc(Source.updated_at)
                    )
                )
            )
            .scalars()
            .all()
        )
        groups: dict[tuple[str, str], list[Source]] = {}
        for source in sources:
            groups.setdefault(self._canonical_key(source), []).append(source)

        mutated = False
        for group in groups.values():
            if len(group) <= 1:
                continue
            await self._merge_source_group(group)
            mutated = True
        if mutated:
            await self.session.commit()

    async def _merge_source_group(self, group: list[Source]) -> Source:
        ordered = sorted(
            group,
            key=lambda source: (
                int(source.is_trusted),
                int(bool(source.url)),
                source.updated_at,
                source.created_at,
            ),
            reverse=True,
        )
        canonical = ordered[0]
        duplicates = ordered[1:]
        duplicate_ids = [source.id for source in duplicates]
        if not duplicate_ids:
            return canonical

        if not canonical.url:
            canonical.url = next(
                (source.url for source in duplicates if source.url), canonical.url
            )
        if not canonical.description:
            canonical.description = next(
                (source.description for source in duplicates if source.description),
                canonical.description,
            )
        if any(source.is_trusted for source in duplicates):
            canonical.is_trusted = True

        move_models = [
            RawEvidence,
            SourceItem,
            SourceClaimRecord,
            SourcePerformanceHistory,
        ]
        for model in move_models:
            await self.session.execute(
                update(model)
                .where(model.source_id.in_(duplicate_ids))
                .values(source_id=canonical.id)
            )

        await self.session.execute(
            update(SourceQualitySegment)
            .where(SourceQualitySegment.source_id.in_(duplicate_ids))
            .values(source_id=canonical.id)
        )

        unique_profile_models = [SourceProfile, SourceTrustProfile, SourceValueProfile]
        for model in unique_profile_models:
            canonical_profile = (
                await self.session.execute(
                    select(model).where(model.source_id == canonical.id)
                )
            ).scalar_one_or_none()
            duplicate_profiles = (
                (
                    await self.session.execute(
                        select(model).where(model.source_id.in_(duplicate_ids))
                    )
                )
                .scalars()
                .all()
            )
            for profile in duplicate_profiles:
                if canonical_profile is None:
                    profile.source_id = canonical.id
                    canonical_profile = profile
                else:
                    await self.session.delete(profile)

        for duplicate in duplicates:
            await KnowledgeAuditService(self.session).record_change(
                node_type="source",
                node_id=duplicate.id,
                change_type="merged_duplicate_source",
                reason="Source catalog consolidation merged a duplicate source into its canonical record.",
                actor="source_catalog",
                source_type="source",
                source_id=canonical.id,
                metadata={
                    "old_source_id": str(duplicate.id),
                    "old_name": duplicate.name,
                    "old_type": duplicate.source_type,
                    "old_url": duplicate.url,
                    "canonical_source_id": str(canonical.id),
                    "canonical_name": canonical.name,
                    "canonical_type": canonical.source_type,
                    "canonical_url": canonical.url,
                },
            )
            await self.session.delete(duplicate)
        await self.session.flush()
        return canonical

    async def _source_claim_assessment_bundle(
        self, claim_record_id: UUID
    ) -> dict | None:
        row = (
            await self.session.execute(
                select(SourceClaimRecord, Claim, Source)
                .join(Claim, SourceClaimRecord.claim_id == Claim.id)
                .join(Source, SourceClaimRecord.source_id == Source.id)
                .where(SourceClaimRecord.id == claim_record_id)
            )
        ).one_or_none()
        if row is None:
            return None
        record, claim, source = row
        subjects = await self._source_claim_subjects(record, claim)
        follow_up_evidence = await self.follow_up_graph_evidence(
            subjects=subjects,
            after_time=record.claim_time,
            exclude_nodes={("claim", claim.id)},
        )
        return {
            "record": record,
            "claim": claim,
            "source": source,
            "subjects": subjects,
            "due_at": source_claim_due_at(record, claim),
            "follow_up_evidence": follow_up_evidence,
        }

    async def _source_claim_subjects(
        self, record: SourceClaimRecord, claim: Claim
    ) -> list[dict]:
        subjects: list[dict] = []
        seen: set[tuple[str, UUID]] = set()
        edges = (
            (
                await self.session.execute(
                    select(Edge).where(
                        Edge.source_type == "claim",
                        Edge.source_id == claim.id,
                        Edge.target_type.in_(["entity", "theme"]),
                    )
                )
            )
            .scalars()
            .all()
        )
        for edge in edges:
            key = (edge.target_type, edge.target_id)
            if key in seen:
                continue
            seen.add(key)
            subjects.append(
                {
                    "subject_type": edge.target_type,
                    "subject_id": str(edge.target_id),
                    "relationship_type": edge.relationship_type,
                    "confidence": float(edge.confidence or 0.0),
                }
            )
        ticker = self._ticker_token(record.ticker)
        if ticker and not subjects:
            security = (
                await self.session.execute(
                    select(Security)
                    .where(Security.ticker.ilike(ticker))
                    .order_by(desc(Security.is_active))
                    .limit(1)
                )
            ).scalar_one_or_none()
            if security is not None:
                subjects.append(
                    {
                        "subject_type": "entity",
                        "subject_id": str(security.entity_id),
                        "ticker": security.ticker,
                        "relationship_type": "ticker_match",
                        "confidence": 0.8,
                    }
                )
        return subjects

    async def follow_up_graph_evidence(
        self,
        *,
        subjects: list[dict],
        after_time: datetime | None,
        exclude_nodes: set[tuple[str, UUID]] | None = None,
        limit: int = 24,
    ) -> list[dict]:
        """Collect later subject-linked graph evidence for outcome assessment."""
        if not subjects:
            return []
        excluded = exclude_nodes or set()
        fact_ids: set[UUID] = set()
        claim_ids: set[UUID] = set()
        event_ids: set[UUID] = set()
        for subject in subjects:
            try:
                subject_id = UUID(str(subject["subject_id"]))
            except (KeyError, TypeError, ValueError):
                continue
            edges = (
                (
                    await self.session.execute(
                        select(Edge).where(
                            Edge.target_type == subject.get("subject_type"),
                            Edge.target_id == subject_id,
                            Edge.source_type.in_(["fact", "claim", "event"]),
                        )
                    )
                )
                .scalars()
                .all()
            )
            for edge in edges:
                if (edge.source_type, edge.source_id) in excluded:
                    continue
                if edge.source_type == "fact":
                    fact_ids.add(edge.source_id)
                elif edge.source_type == "claim":
                    claim_ids.add(edge.source_id)
                elif edge.source_type == "event":
                    event_ids.add(edge.source_id)

        nodes: list[dict] = []
        if fact_ids:
            facts = (
                (
                    await self.session.execute(
                        select(Fact).where(
                            Fact.id.in_(list(fact_ids)), Fact.is_deprecated.is_(False)
                        )
                    )
                )
                .scalars()
                .all()
            )
            nodes.extend(
                self._source_claim_node_payload("fact", fact) for fact in facts
            )
        if claim_ids:
            claims = (
                (
                    await self.session.execute(
                        select(Claim).where(
                            Claim.id.in_(list(claim_ids)),
                            Claim.is_deprecated.is_(False),
                        )
                    )
                )
                .scalars()
                .all()
            )
            nodes.extend(
                self._source_claim_node_payload("claim", claim) for claim in claims
            )
        if event_ids:
            events = (
                (
                    await self.session.execute(
                        select(Event).where(
                            Event.id.in_(list(event_ids)),
                            Event.is_deprecated.is_(False),
                        )
                    )
                )
                .scalars()
                .all()
            )
            nodes.extend(
                self._source_claim_node_payload("event", event) for event in events
            )

        after_dt = self._as_utc(after_time)
        filtered = [
            node
            for node in nodes
            if after_dt is None
            or self._as_utc(node.get("date_obj")) is None
            or self._as_utc(node.get("date_obj")) > after_dt
        ]
        filtered.sort(
            key=lambda item: self._as_utc(item.get("date_obj"))
            or datetime.min.replace(tzinfo=UTC),
            reverse=True,
        )
        payloads = []
        for node in filtered[:limit]:
            node = dict(node)
            node.pop("date_obj", None)
            payloads.append(node)
        return payloads

    @staticmethod
    def _source_claim_node_payload(node_type: str, node) -> dict:
        date_obj = SourceService._best_node_time(node)
        text = getattr(node, "statement", None) or getattr(node, "title", None) or ""
        return {
            "id": str(node.id),
            "node_type": node_type,
            "text": _bounded_excerpt(text, 500),
            "date": None if date_obj is None else date_obj.isoformat(),
            "date_obj": date_obj,
            "tier": getattr(node, "tier", None),
            "importance": getattr(node, "importance", None),
            "contradiction_role": getattr(node, "contradiction_role", None),
            "target_horizon": getattr(node, "target_horizon", None),
        }

    @staticmethod
    def _best_node_time(node) -> datetime | None:
        for attr in ("event_time", "public_time", "ingest_time", "created_at"):
            value = getattr(node, attr, None)
            if value is not None:
                return SourceService._as_utc(value)
        return None

    @staticmethod
    def _as_utc(dt: datetime | None) -> datetime | None:
        if dt is None:
            return None
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)

    @staticmethod
    def _ticker_token(value: str | None) -> str | None:
        cleaned = " ".join((value or "").split()).strip()
        if not cleaned:
            return None
        token = cleaned.split("·", 1)[0].split(" ", 1)[0].strip().upper()
        return token or None

    @staticmethod
    def _normalized_subject_label(value: str | None) -> str:
        return re.sub(r"[^a-z0-9]+", " ", (value or "").casefold()).strip()

    @classmethod
    def _subject_label_candidates(cls, value: str | None) -> set[str]:
        raw = " ".join((value or "").split()).strip()
        if not raw:
            return set()
        candidates = {cls._normalized_subject_label(raw)}
        candidates.update(
            cls._normalized_subject_label(part) for part in re.split(r"[·|/()]", raw)
        )
        candidates.update(cls._normalized_subject_label(token) for token in raw.split())
        return {candidate for candidate in candidates if candidate}

    @staticmethod
    def _sanitize_claim_assessment_proposal(
        raw: dict,
        *,
        allowed_evidence_ids: set[UUID],
        min_confidence: float,
    ) -> dict:
        assessment = str(raw.get("assessment") or "indeterminate").strip().lower()
        if assessment not in {
            "correct",
            "incorrect",
            "partially_correct",
            "indeterminate",
        }:
            assessment = "indeterminate"
        try:
            confidence = float(raw.get("confidence") or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        confidence = max(0.0, min(1.0, confidence))
        min_confidence = max(0.0, min(1.0, float(min_confidence or 0.0)))
        evidence_ids: list[UUID] = []
        for raw_id in raw.get("assessment_evidence_ids") or []:
            try:
                evidence_id = UUID(str(raw_id))
            except (TypeError, ValueError):
                continue
            if evidence_id in allowed_evidence_ids and evidence_id not in evidence_ids:
                evidence_ids.append(evidence_id)
        should_apply = confidence >= min_confidence and (
            assessment == "indeterminate" or bool(evidence_ids)
        )
        return {
            "assessment": assessment,
            "confidence": confidence,
            "rationale": str(raw.get("rationale") or "").strip(),
            "limitations": str(raw.get("limitations") or "").strip(),
            "assessment_evidence": evidence_ids,
            "should_apply": should_apply,
        }

    @staticmethod
    def _claim_assessment_notes(
        *,
        proposal: dict,
        source: Source,
        claim: Claim,
        evidence_lookup: dict[UUID, dict],
    ) -> str:
        evidence_bits = []
        for evidence_id in proposal.get("assessment_evidence") or []:
            evidence = evidence_lookup.get(evidence_id)
            if evidence is None:
                continue
            label = _bounded_excerpt(evidence.get("text"), 120) or str(evidence_id)
            evidence_bits.append(f"{evidence.get('node_type')}:{evidence_id} {label}")
        parts = [
            "Auto-assessed by source outcome assessor.",
            f"source={source.name}",
            f"claim={_bounded_excerpt(claim.statement, 240)}",
            f"assessment={proposal.get('assessment')}",
            f"confidence={float(proposal.get('confidence') or 0.0):.2f}",
        ]
        rationale = proposal.get("rationale")
        limitations = proposal.get("limitations")
        if rationale:
            parts.append(f"rationale={_bounded_excerpt(rationale, 500)}")
        if limitations:
            parts.append(f"limitations={_bounded_excerpt(limitations, 300)}")
        if evidence_bits:
            parts.append("evidence=" + " || ".join(evidence_bits[:5]))
        return " | ".join(parts)

    @staticmethod
    def _source_claim_followup_title(record: SourceClaimRecord, claim: Claim) -> str:
        ticker = SourceService._ticker_token(getattr(record, "ticker", None))
        statement = (
            _bounded_excerpt(getattr(claim, "statement", None), 86) or "claim outcome"
        )
        subject = ticker or "source claim"
        return f"Source outcome follow-up for {subject}: {statement}"

    @staticmethod
    def _source_claim_followup_query(record: SourceClaimRecord, claim: Claim) -> str:
        ticker = SourceService._ticker_token(getattr(record, "ticker", None))
        subject = ticker or "the subject"
        date = getattr(record, "claim_time", None)
        date_text = (
            date.date().isoformat() if isinstance(date, datetime) else "the claim date"
        )
        statement = (
            _bounded_excerpt(getattr(claim, "statement", None), 220)
            or "the source claim"
        )
        return (
            f'Find later primary or high-quality evidence after {date_text} that tests whether "{statement}" '
            f"was correct for {subject}; include direct outcome, timing, mechanism, and magnitude evidence."
        )

    @staticmethod
    def _evidence_summary(evidence: RawEvidence, source: Source) -> dict:
        return {
            "id": evidence.id,
            "source_id": source.id,
            "source_name": source.name,
            "source_type": source.source_type,
            "title": evidence.title,
            "url": evidence.url,
            "source_item_type": evidence.source_item_type,
            "is_processed": evidence.is_processed,
            **SourceService._evidence_origin_summary(evidence, source),
            "user_feedback": (evidence.metadata_json or {}).get("user_feedback"),
            "created_at": evidence.created_at,
            "updated_at": evidence.updated_at,
        }

    @staticmethod
    def _source_origin_summary(source: Source) -> dict:
        source_type = (source.source_type or "").strip().lower()
        name = (source.name or "").strip().lower()
        description = (source.description or "").strip().lower()
        if source_type == "email":
            return {
                "origin_kind": "email",
                "origin_label": "Email ingestion",
                "origin_detail": "Created for mailbox and broker-confirmation evidence.",
            }
        if source_type in {"video", "youtube"}:
            return {
                "origin_kind": "manual",
                "origin_label": "YouTube transcript source",
                "origin_detail": "Tracked for transcript-based YouTube research. Video frames and audio are not analyzed by this source record alone.",
            }
        if source_type in {"filing", "ownership_tracker"}:
            return {
                "origin_kind": "disclosure",
                "origin_label": "Disclosure tracking",
                "origin_detail": "Created from regulatory, ownership, insider, or institutional-flow disclosure evidence.",
            }
        if (
            source_type in {"web_research", "peer_research"}
            or "auto-discovered" in description
        ):
            return {
                "origin_kind": "discovery",
                "origin_label": "Research discovery",
                "origin_detail": "Created from external research results.",
            }
        if name == "prophet agent conversation":
            return {
                "origin_kind": "chat",
                "origin_label": "Prophet conversation",
                "origin_detail": "Created from user and assistant turns.",
            }
        if source_type == "manual":
            return {
                "origin_kind": "manual",
                "origin_label": "Manual add",
                "origin_detail": "Created from a user-managed source or note.",
            }
        if source.is_trusted:
            return {
                "origin_kind": "manual",
                "origin_label": "Trusted source list",
                "origin_detail": "Tracked in the user-controlled source catalog.",
            }
        return {
            "origin_kind": "catalog",
            "origin_label": "Source catalog",
            "origin_detail": f"Tracked as {source.source_type.replace('_', ' ')}.",
        }

    @staticmethod
    def _evidence_origin_summary(evidence: RawEvidence, source: Source) -> dict:
        raw_metadata = getattr(evidence, "metadata_json", None)
        metadata = raw_metadata if isinstance(raw_metadata, dict) else {}
        source_type = (source.source_type or "").strip().lower()
        item_type = (evidence.source_item_type or "").strip().lower()
        trigger = str(metadata.get("trigger") or "").strip()
        origin = str(metadata.get("origin") or "").strip()
        sender = str(metadata.get("sender") or "").strip()
        query = str(
            metadata.get("effective_query")
            or metadata.get("normalized_query")
            or metadata.get("query")
            or ""
        ).strip()

        if (
            metadata.get("operational_mailbox")
            or sender
            or source_type == "email"
            or item_type.startswith("email")
        ):
            detail = sender or "Mailbox/backfill evidence"
            return {
                "origin_kind": "email",
                "origin_label": "Email ingestion",
                "origin_detail": detail,
            }
        if (
            trigger == "manual_youtube_ingest"
            or item_type
            in {
                "video_transcript",
                "video_audio_transcript",
                "manual_transcript",
                "video_notes",
            }
            or source_type in {"video", "youtube"}
            or str(metadata.get("media_source_type") or "").strip().lower()
            in {"video", "youtube"}
        ):
            video_ref = str(
                metadata.get("video_url")
                or metadata.get("video_id")
                or evidence.url
                or ""
            ).strip()
            if item_type == "manual_transcript":
                label = "Manual YouTube transcript"
                detail = (
                    video_ref
                    or "User supplied transcript text for a video Prophet could not extract automatically."
                )
            elif item_type == "video_notes":
                label = "YouTube video notes"
                detail = (
                    video_ref
                    or "User supplied notes from a video Prophet could not extract automatically."
                )
            elif item_type == "video_audio_transcript":
                label = "Local YouTube audio transcript"
                detail = video_ref or "Local speech-to-text transcript only."
            else:
                label = "YouTube transcript ingest"
                detail = video_ref or "Transcript text only."
            return {
                "origin_kind": "manual",
                "origin_label": label,
                "origin_detail": detail,
            }
        if origin == "source_workspace" or item_type in NOTE_SOURCE_ITEM_TYPES:
            label_by_type = {
                "cagr_test": "Manual CAGR test",
                "research_note": "Research note",
                "user_note": "Manual note",
                "manual_note": "Manual note",
            }
            return {
                "origin_kind": "manual",
                "origin_label": label_by_type.get(item_type, "Manual note"),
                "origin_detail": "Saved from the Sources workspace.",
            }
        if origin == "agent_chat":
            return {
                "origin_kind": "chat",
                "origin_label": "Chat turn",
                "origin_detail": "Saved from a Prophet chat session.",
            }
        if origin == "agent_reflection":
            return {
                "origin_kind": "automation",
                "origin_label": "Autonomous reflection",
                "origin_detail": "Saved by Prophet's background reflection loop.",
            }
        if source_type in {"filing", "ownership_tracker"} or item_type in {
            "insider_disclosure",
            "ownership_disclosure",
            "institutional_flow",
            "congressional_trade_disclosure",
        }:
            detail = (
                query
                or str(metadata.get("disclosure_url") or evidence.url or "").strip()
            )
            return {
                "origin_kind": "disclosure",
                "origin_label": "Disclosure evidence",
                "origin_detail": detail
                or "Ownership, insider, political, or institutional-flow disclosure.",
            }
        if trigger in {"research_loop", "source_claim_assessment_followup"}:
            return {
                "origin_kind": "automation",
                "origin_label": "Autonomous research",
                "origin_detail": query or trigger.replace("_", " "),
            }
        if trigger in {"chat_research_start", "chat_auto_research_gap"}:
            return {
                "origin_kind": "discovery",
                "origin_label": "Chat-started research",
                "origin_detail": query or "Started from a chat request.",
            }
        if source_type in {"web_research", "peer_research"} or item_type in {
            "web_research",
            "source_claim_followup",
        }:
            return {
                "origin_kind": "discovery",
                "origin_label": "External research",
                "origin_detail": query or evidence.url or "Research-provider result.",
            }
        if trigger:
            return {
                "origin_kind": "automation",
                "origin_label": trigger.replace("_", " "),
                "origin_detail": query or None,
            }
        return {
            "origin_kind": SourceService._source_origin_summary(source)["origin_kind"],
            "origin_label": SourceService._source_origin_summary(source)[
                "origin_label"
            ],
            "origin_detail": SourceService._source_origin_summary(source)[
                "origin_detail"
            ],
        }

    @staticmethod
    def _feedback_summary(
        evidence: RawEvidence, source: Source, feedback: dict
    ) -> dict:
        flagged_at = feedback.get("flagged_at")
        parsed_flagged_at = None
        if isinstance(flagged_at, str):
            try:
                parsed_flagged_at = datetime.fromisoformat(flagged_at)
            except ValueError:
                parsed_flagged_at = None
        return {
            "evidence_id": evidence.id,
            "source_id": source.id,
            "source_name": source.name,
            "source_type": source.source_type,
            "title": evidence.title,
            "url": evidence.url,
            "source_item_type": evidence.source_item_type,
            **SourceService._evidence_origin_summary(evidence, source),
            "rating": feedback.get("rating") or "useful",
            "note": feedback.get("note"),
            "context": feedback.get("context"),
            "flagged_at": parsed_flagged_at,
            "lesson_id": feedback.get("lesson_id"),
            "lesson_title": feedback.get("lesson_title"),
            "created_at": evidence.created_at,
        }

    @staticmethod
    def _performance_history_summary(history: SourcePerformanceHistory) -> dict:
        return {
            "id": history.id,
            "source_id": history.source_id,
            "domain": history.domain,
            "sector": history.sector,
            "regime": history.regime,
            "period_start": history.period_start,
            "period_end": history.period_end,
            "total_claims": int(history.total_claims or 0),
            "correct_claims": int(history.correct_claims or 0),
            "incorrect_claims": int(history.incorrect_claims or 0),
            "accuracy_rate": float(history.accuracy_rate or 0.0),
            "originality_rate": float(history.originality_rate or 0.0),
            "timing_score": float(history.timing_score or 0.0),
            "computed_at": history.computed_at,
        }

    @staticmethod
    def _performance_history_payload(records: list[SourceClaimRecord]) -> dict | None:
        scored_records = [
            record
            for record in records
            if record.assessment in SCORED_SOURCE_CLAIM_ASSESSMENTS
            and record.assessment_time is not None
        ]
        if not scored_records:
            return None
        total = len(scored_records)
        correct = sum(1 for record in scored_records if record.assessment == "correct")
        partial = sum(
            1 for record in scored_records if record.assessment == "partially_correct"
        )
        incorrect = sum(
            1 for record in scored_records if record.assessment == "incorrect"
        )
        scored_correct = correct + 0.5 * partial
        accuracy_rate = scored_correct / total if total else 0.0
        originality_hits = sum(
            1
            for record in scored_records
            if SourceService._claim_notes_suggest_originality(record.notes)
        )
        timed_records = [
            record for record in scored_records if record.horizon_days is not None
        ]
        if timed_records:
            timed_correct = sum(
                (
                    1.0
                    if record.assessment == "correct"
                    else 0.5 if record.assessment == "partially_correct" else 0.0
                )
                for record in timed_records
            )
            timing_score = timed_correct / len(timed_records)
        else:
            timing_score = accuracy_rate
        period_start = min(record.claim_time for record in scored_records)
        period_end = max(
            record.assessment_time
            for record in scored_records
            if record.assessment_time is not None
        )
        return {
            "domain": None,
            "sector": None,
            "regime": None,
            "period_start": period_start,
            "period_end": period_end,
            "total_claims": total,
            "correct_claims": correct,
            "incorrect_claims": incorrect,
            "accuracy_rate": round(accuracy_rate, 4),
            "originality_rate": round(originality_hits / total, 4),
            "timing_score": round(timing_score, 4),
        }

    @staticmethod
    def _claim_notes_suggest_originality(notes: str | None) -> bool:
        normalized = " ".join((notes or "").lower().split())
        if not normalized:
            return False
        return any(
            marker in normalized
            for marker in (
                "original",
                "primary",
                "exclusive",
                "first-hand",
                "first hand",
                "non-consensus",
                "not consensus",
            )
        )

    async def _apply_performance_to_trust_profiles(
        self,
        source: Source,
        history: SourcePerformanceHistory,
    ) -> None:
        total_claims = int(history.total_claims or 0)
        if total_claims < 2:
            return
        reliability = self._performance_reliability_label(
            float(history.accuracy_rate or 0.0)
        )
        trajectory = self._performance_trajectory_label(
            float(history.accuracy_rate or 0.0), total_claims
        )
        trust = (
            await self.session.execute(
                select(SourceTrustProfile).where(
                    SourceTrustProfile.source_id == source.id
                )
            )
        ).scalar_one_or_none()
        if trust is None:
            trust = SourceTrustProfile(
                source_id=source.id,
                factual_reliability=reliability,
                calibration="calibrated",
                correction_quality="slow_corrects",
                noise_ratio="moderate",
                trust_trajectory=trajectory,
            )
            self.session.add(trust)
        else:
            trust.factual_reliability = reliability
            trust.trust_trajectory = trajectory
            trust.last_evaluated_at = datetime.now(UTC)
        profile = (
            await self.session.execute(
                select(SourceProfile).where(SourceProfile.source_id == source.id)
            )
        ).scalar_one_or_none()
        performance_note = (
            f"Outcome history: {total_claims} assessed claims, "
            f"{float(history.accuracy_rate or 0.0):.0%} weighted accuracy, "
            f"{float(history.timing_score or 0.0):.0%} timing score."
        )
        if profile is None:
            profile = SourceProfile(
                source_id=source.id,
                specialization_domains=[],
                known_weaknesses=[],
                trust_trajectory=trajectory,
                first_tracked_at=datetime.now(UTC),
                total_claims_tracked=total_claims,
                active_since=datetime.now(UTC),
                notes=performance_note,
            )
            self.session.add(profile)
        else:
            profile.total_claims_tracked = max(
                int(profile.total_claims_tracked or 0), total_claims
            )
            profile.trust_trajectory = trajectory
            profile.notes = performance_note
            profile.last_reviewed_at = datetime.now(UTC)

    @staticmethod
    def _performance_reliability_label(accuracy_rate: float) -> str:
        if accuracy_rate >= 0.85:
            return "very_high"
        if accuracy_rate >= 0.7:
            return "high"
        if accuracy_rate >= 0.55:
            return "medium"
        if accuracy_rate >= 0.4:
            return "low"
        return "very_low"

    @staticmethod
    def _performance_trajectory_label(accuracy_rate: float, total_claims: int) -> str:
        if total_claims < 3:
            return "stable"
        if accuracy_rate >= 0.7:
            return "improving"
        if accuracy_rate < 0.45:
            return "degrading"
        return "stable"

    async def _upsert_feedback_lesson(
        self,
        *,
        evidence: RawEvidence,
        source: Source,
        feedback: dict,
        previous_feedback: dict,
    ) -> Lesson | None:
        payload = self._feedback_lesson_payload(
            evidence=evidence,
            source=source,
            feedback=feedback,
        )
        lesson = await self._lesson_from_feedback(previous_feedback)
        if lesson is None:
            tag = f"{SOURCE_FEEDBACK_LESSON_TAG}={evidence.id}"
            lesson = (
                await self.session.execute(
                    select(Lesson)
                    .where(
                        Lesson.lesson_type == SOURCE_FEEDBACK_LESSON_TYPE,
                        Lesson.summary.contains(tag),
                    )
                    .order_by(desc(Lesson.created_at))
                    .limit(1)
                )
            ).scalar_one_or_none()
        if lesson is None:
            lesson = Lesson(**payload)
            self.session.add(lesson)
        else:
            lesson.title = payload["title"]
            lesson.summary = payload["summary"]
            lesson.lesson_type = payload["lesson_type"]
            lesson.applicable_sectors = payload["applicable_sectors"]
            lesson.applicable_regimes = payload["applicable_regimes"]
        await self.session.flush()
        return lesson

    async def _lesson_from_feedback(self, feedback: dict) -> Lesson | None:
        raw_lesson_id = feedback.get("lesson_id")
        if not raw_lesson_id:
            return None
        try:
            lesson_id = UUID(str(raw_lesson_id))
        except (TypeError, ValueError):
            return None
        return (
            await self.session.execute(select(Lesson).where(Lesson.id == lesson_id))
        ).scalar_one_or_none()

    @staticmethod
    def _feedback_lesson_payload(
        *,
        evidence: RawEvidence,
        source: Source,
        feedback: dict,
    ) -> dict:
        rating = str(feedback.get("rating") or "useful").strip().lower()
        useful = rating == "useful"
        rating_label = "useful" if useful else "not useful"
        evidence_label = evidence.title or evidence.url or "Untitled evidence"
        note = str(feedback.get("note") or "").strip()
        context = str(feedback.get("context") or "").strip()
        flagged_at = str(feedback.get("flagged_at") or "").strip()
        guidance = (
            "Prefer similarly specific evidence from this source when it is relevant to the active question."
            if useful
            else "Down-rank similar evidence from this source unless stronger direct corroboration supports it."
        )
        summary_parts = [
            f"{source.name} was marked {rating_label} for evidence titled '{evidence_label}'.",
        ]
        if note:
            summary_parts.append(f"User note: {note}")
        summary_parts.append(f"Guidance: {guidance}")
        trace_parts = [
            f"{SOURCE_FEEDBACK_LESSON_TAG}={evidence.id}",
            f"source={source.name}",
            f"source_type={source.source_type}",
            f"rating={rating_label}",
        ]
        if context:
            trace_parts.append(f"context={context}")
        if flagged_at:
            trace_parts.append(f"flagged_at={flagged_at}")
        if evidence.url:
            trace_parts.append(f"url={evidence.url}")
        summary_parts.append(f"Trace: {'; '.join(trace_parts)}.")
        return {
            "title": f"Source feedback: {source.name} was {rating_label}",
            "summary": " ".join(summary_parts),
            "lesson_type": SOURCE_FEEDBACK_LESSON_TYPE,
            "applicable_sectors": [],
            "applicable_regimes": ["source_feedback", rating, source.source_type],
            "originating_decision_review_id": None,
            "originating_experiment_result_id": None,
        }

    async def _ensure_feedback_lesson_links(self, limit: int = 1000) -> None:
        rows = (
            await self.session.execute(
                select(RawEvidence, Source)
                .join(Source, RawEvidence.source_id == Source.id)
                .where(RawEvidence.metadata_json.is_not(None))
                .order_by(desc(RawEvidence.updated_at))
                .limit(limit)
            )
        ).all()
        mutated = False
        for evidence, source in rows:
            metadata = dict(evidence.metadata_json or {})
            feedback = metadata.get("user_feedback")
            if not isinstance(feedback, dict):
                continue
            rating = str(feedback.get("rating") or "").strip()
            if rating not in USER_FEEDBACK_RATINGS:
                continue
            feedback = dict(feedback)
            existing_lesson = await self._lesson_from_feedback(feedback)
            if existing_lesson is not None and feedback.get("lesson_title"):
                continue
            lesson = await self._upsert_feedback_lesson(
                evidence=evidence,
                source=source,
                feedback=feedback,
                previous_feedback=feedback,
            )
            if lesson is None:
                continue
            feedback["lesson_id"] = str(lesson.id)
            feedback["lesson_title"] = lesson.title
            metadata["user_feedback"] = feedback
            evidence.metadata_json = metadata
            evidence.mark_updated()
            mutated = True
        if mutated:
            await self.session.commit()
