from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from investos.db import get_session
from investos.models.catalog import SourceClaimRecord
from investos.models.evidence import RawEvidence, SourceItem
from investos.models.graph import Edge
from investos.models.knowledge import Claim, Event, Fact
from investos.models.knowledge_mutation import KnowledgeMutation
from investos.models.source import Source
from investos.schemas.provenance import EvidenceSourceReferenceResponse
from investos.services.evidence_provenance import build_evidence_source_reference
from investos.services.knowledge_time import (
    KnowledgeTemporalContext,
    assess_knowledge_time,
    is_legacy_synthetic_event_time,
)
from investos.services.operating_state import OperatingStateService
from investos.services.source_claim_policy import source_claim_due_at

router = APIRouter(prefix="/timeline", tags=["timeline"])


def _display_time(item) -> tuple[datetime, str]:
    event_time = _effective_event_time(item)
    if event_time is not None:
        return event_time, "happened"
    if getattr(item, "public_time", None) is not None:
        return item.public_time, "published"
    if getattr(item, "ingest_time", None) is not None:
        return item.ingest_time, "ingested"
    return item.created_at, "recorded"


def _effective_event_time(item) -> datetime | None:
    event_time = getattr(item, "event_time", None)
    if isinstance(item, (Fact, Claim)) and is_legacy_synthetic_event_time(
        event_time,
        public_time=getattr(item, "public_time", None),
        ingest_time=getattr(item, "ingest_time", None),
    ):
        return None
    return event_time


def _temporal_context(item, *, item_type: str) -> KnowledgeTemporalContext:
    text = (
        getattr(item, "statement", None)
        or getattr(item, "description", None)
        or getattr(item, "title", None)
    )
    return assess_knowledge_time(
        text,
        event_time=_effective_event_time(item),
        public_time=getattr(item, "public_time", None),
        ingest_time=getattr(item, "ingest_time", None),
        valid_until=getattr(item, "valid_until", None),
        item_type=item_type,
    )


class TimelineItemResponse(BaseModel):
    id: UUID
    item_type: str  # fact|claim|event
    text: str
    tier: str
    importance: str | None = None
    directness: str | None = None
    novelty: str | None = None
    contradiction_role: str | None = None
    signal_score: float = 0.0
    subject_name: str | None = None
    source_name: str | None = None
    source_type: str | None = None
    sources: list[EvidenceSourceReferenceResponse] = Field(default_factory=list)
    event_time: datetime | None = None
    public_time: datetime | None = None
    ingest_time: datetime | None = None
    display_time: datetime
    display_time_label: str
    temporal_status: str
    temporal_explanation: str
    referenced_years: list[int] = Field(default_factory=list)
    valid_until: datetime | None = None
    outcome_status: str | None = None
    outcome_due_at: datetime | None = None
    outcome_assessed_at: datetime | None = None
    outcome_notes: str | None = None
    created_at: datetime


class KnowledgeChangeResponse(BaseModel):
    id: UUID
    change_id: UUID | None = None
    node_type: str
    text: str
    change_type: str
    change_source: str = "derived_state"
    changed_at: datetime
    created_at: datetime
    updated_at: datetime
    is_deprecated: bool
    deprecated_reason: str | None = None
    superseded_by_id: UUID | None = None
    reason: str | None = None
    actor: str | None = None
    source_type: str | None = None
    source_id: UUID | None = None
    subject_type: str | None = None
    subject_id: UUID | None = None
    metadata: dict | None = None


class KnowledgeChangeSummaryResponse(BaseModel):
    active_facts: int
    active_claims: int
    active_events: int
    deprecated_facts: int
    deprecated_claims: int
    deprecated_events: int
    changes: list[KnowledgeChangeResponse]


IMPORTANCE_SCORE = {
    "critical": 1.0,
    "high": 0.8,
    "medium": 0.55,
    "low": 0.3,
    "trivial": 0.1,
}

DIRECTNESS_SCORE = {
    "primary": 1.0,
    "secondary": 0.65,
    "tertiary": 0.35,
}

NOVELTY_SCORE = {
    "breaking": 1.0,
    "confirming": 0.55,
    "redundant": 0.2,
    "stale": 0.1,
    "historical": 0.1,
}

CONTRADICTION_BONUS = {
    "contradicts_consensus": 0.25,
    "ambiguous": 0.1,
    "supports_consensus": 0.0,
    "neutral": 0.0,
}


def _fact_signal_score(
    item: Fact | Claim,
    temporal: KnowledgeTemporalContext | None = None,
) -> float:
    novelty = temporal.novelty if temporal is not None else item.novelty
    return round(
        (IMPORTANCE_SCORE.get(item.importance, 0.35) * 0.45)
        + (DIRECTNESS_SCORE.get(item.directness, 0.35) * 0.25)
        + (NOVELTY_SCORE.get(novelty, 0.2) * 0.2)
        + CONTRADICTION_BONUS.get(item.contradiction_role, 0.0)
        + (0.1 if item.tier in {"hard_fact", "strong_derived"} else 0.0),
        3,
    )


def _event_signal_score(item: Event) -> float:
    return round(
        (0.8 if item.is_evolving else 0.45)
        + (
            0.15
            if item.event_type in {"earnings", "filing", "guidance", "regulatory"}
            else 0.0
        ),
        3,
    )


def _knowledge_change_type(item: Fact | Claim | Event) -> str:
    if getattr(item, "is_deprecated", False):
        return "deprecated"
    if getattr(item, "updated_at", None) and item.updated_at > item.created_at:
        return "updated"
    return "created"


def _knowledge_change_payload(
    node_type: str,
    item: Fact | Claim | Event,
    *,
    mutation: KnowledgeMutation | None = None,
) -> KnowledgeChangeResponse:
    text = (
        getattr(item, "statement", None)
        or getattr(item, "description", None)
        or getattr(item, "title", "")
    )
    change_type = (
        mutation.change_type if mutation is not None else _knowledge_change_type(item)
    )
    changed_at = (
        mutation.created_at
        if mutation is not None
        else item.updated_at if change_type != "created" else item.created_at
    )
    return KnowledgeChangeResponse(
        id=item.id,
        change_id=mutation.id if mutation is not None else None,
        node_type=node_type,
        text=text,
        change_type=change_type,
        change_source="audit_event" if mutation is not None else "derived_state",
        changed_at=changed_at,
        created_at=item.created_at,
        updated_at=item.updated_at,
        is_deprecated=bool(getattr(item, "is_deprecated", False)),
        deprecated_reason=getattr(item, "deprecated_reason", None),
        superseded_by_id=getattr(item, "superseded_by_id", None),
        reason=mutation.reason if mutation is not None else None,
        actor=mutation.actor if mutation is not None else None,
        source_type=mutation.source_type if mutation is not None else None,
        source_id=mutation.source_id if mutation is not None else None,
        subject_type=mutation.subject_type if mutation is not None else None,
        subject_id=mutation.subject_id if mutation is not None else None,
        metadata=mutation.metadata_json if mutation is not None else None,
    )


def _mutation_tombstone_payload(mutation: KnowledgeMutation) -> KnowledgeChangeResponse:
    metadata = mutation.metadata_json or {}
    label = (
        metadata.get("label")
        or metadata.get("relationship_type")
        or metadata.get("summary")
        or mutation.reason
        or f"{mutation.node_type} {mutation.change_type}"
    )
    if mutation.node_type == "edge" and metadata.get("relationship_type"):
        label = (
            f"{metadata.get('source_type')}:{metadata.get('source_id')} "
            f"{metadata.get('relationship_type')} "
            f"{metadata.get('target_type')}:{metadata.get('target_id')}"
        )
    return KnowledgeChangeResponse(
        id=mutation.node_id,
        change_id=mutation.id,
        node_type=mutation.node_type,
        text=str(label),
        change_type=mutation.change_type,
        change_source="audit_event",
        changed_at=mutation.created_at,
        created_at=mutation.created_at,
        updated_at=mutation.created_at,
        is_deprecated=mutation.change_type
        in {
            "deprecated",
            "deleted",
            "deleted_orphan",
            "deleted_duplicate",
            "deleted_artifact",
            "deleted_unusable",
            "obsoleted_artifact",
            "closed_stale_zero_holding",
            "reset_corrupt",
        },
        deprecated_reason=mutation.reason,
        superseded_by_id=None,
        reason=mutation.reason,
        actor=mutation.actor,
        source_type=mutation.source_type,
        source_id=mutation.source_id,
        subject_type=mutation.subject_type,
        subject_id=mutation.subject_id,
        metadata=metadata,
    )


@dataclass
class _TimelineContextReads:
    sources: dict[
        tuple[UUID, ...], list[tuple[EvidenceSourceReferenceResponse, dict]]
    ] = field(default_factory=dict)
    subjects: dict[tuple[UUID, str], str | None] = field(default_factory=dict)


async def _source_context(
    session: AsyncSession,
    *,
    node_type: str,
    node_id: UUID,
    source_item_id: UUID | None,
    reads: _TimelineContextReads | None = None,
) -> tuple[str | None, list[EvidenceSourceReferenceResponse]]:
    reads = reads if reads is not None else _TimelineContextReads()
    if source_item_id is None:
        source_item_ids = list(
            (
                await session.execute(
                    select(Edge.target_id).where(
                        Edge.source_type == node_type,
                        Edge.source_id == node_id,
                        Edge.target_type == "source_item",
                        Edge.relationship_type == "extracted_from",
                    )
                )
            ).scalars()
        )
    else:
        source_item_ids = [source_item_id]
    if not source_item_ids:
        return None, []

    key = tuple(source_item_ids)
    if key not in reads.sources:
        rows = (
            await session.execute(
                select(SourceItem, RawEvidence, Source)
                .join(RawEvidence, SourceItem.raw_evidence_id == RawEvidence.id)
                .join(Source, RawEvidence.source_id == Source.id)
                .where(SourceItem.id.in_(source_item_ids))
            )
        ).all()
        reads.sources[key] = [
            (
                build_evidence_source_reference(
                    source_item_id=resolved_source_item.id,
                    raw_evidence=raw_evidence,
                    source=source,
                ),
                raw_evidence.metadata_json or {},
            )
            for resolved_source_item, raw_evidence, source in rows
        ]
    contexts = reads.sources[key]
    if not contexts:
        return None, []

    sources = [source for source, _ in contexts]
    subject_name = None
    subject_edge = (
        await session.execute(
            select(Edge.target_type, Edge.target_id)
            .where(
                Edge.source_type == node_type,
                Edge.source_id == node_id,
                Edge.target_type.in_(("entity", "theme")),
                Edge.relationship_type.in_(("supports", "contradicts", "mentions")),
            )
            .order_by(desc(Edge.confidence), Edge.created_at.asc())
            .limit(1)
        )
    ).first()
    raw_subject_type = subject_edge[0] if subject_edge is not None else None
    raw_subject_id = subject_edge[1] if subject_edge is not None else None
    if raw_subject_id is None or raw_subject_type is None:
        for _, metadata in contexts:
            raw_subject_id = metadata.get("subject_id")
            raw_subject_type = metadata.get("subject_type")
            if raw_subject_id and raw_subject_type:
                break
    if raw_subject_id and raw_subject_type:
        try:
            subject_key = (UUID(str(raw_subject_id)), str(raw_subject_type))
            if subject_key not in reads.subjects:
                reads.subjects[subject_key] = await OperatingStateService(
                    session
                ).subject_name(*subject_key)
            subject_name = reads.subjects[subject_key]
        except Exception as exc:
            import logging

            logging.getLogger(__name__).exception("Masked failure caught")
            subject_name = None

    return subject_name, sources


async def _knowledge_count(session: AsyncSession, model, deprecated: bool) -> int:
    return int(
        (
            await session.execute(
                select(func.count())
                .select_from(model)
                .where(model.is_deprecated.is_(deprecated))
            )
        ).scalar_one()
    )


async def _node_for_mutation(
    session: AsyncSession,
    mutation: KnowledgeMutation,
) -> Fact | Claim | Event | None:
    model_map = {"fact": Fact, "claim": Claim, "event": Event}
    model = model_map.get(mutation.node_type)
    if model is None:
        return None
    return (
        await session.execute(select(model).where(model.id == mutation.node_id))
    ).scalar_one_or_none()


async def _derived_knowledge_changes(
    session: AsyncSession,
    *,
    limit: int,
    skip: set[tuple[str, UUID]] | None = None,
) -> list[KnowledgeChangeResponse]:
    skip = skip or set()
    raw_changes: list[KnowledgeChangeResponse] = []
    for node_type, model in (("fact", Fact), ("claim", Claim), ("event", Event)):
        rows = (
            (
                await session.execute(
                    select(model)
                    .order_by(desc(model.updated_at), desc(model.created_at))
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        raw_changes.extend(
            _knowledge_change_payload(node_type, item)
            for item in rows
            if (node_type, item.id) not in skip
        )

    raw_changes.sort(key=lambda item: item.changed_at, reverse=True)
    return raw_changes[:limit]


@router.get("/knowledge-changes", response_model=KnowledgeChangeSummaryResponse)
async def get_knowledge_changes(
    limit: int = 30,
    session: AsyncSession = Depends(get_session),
):
    """Recent knowledge lifecycle events with a fallback for pre-ledger rows."""
    audit_rows = (
        (
            await session.execute(
                select(KnowledgeMutation)
                .order_by(desc(KnowledgeMutation.created_at))
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    changes: list[KnowledgeChangeResponse] = []
    seen: set[tuple[str, UUID]] = set()
    for mutation in audit_rows:
        node = await _node_for_mutation(session, mutation)
        if node is None:
            changes.append(_mutation_tombstone_payload(mutation))
        else:
            changes.append(
                _knowledge_change_payload(mutation.node_type, node, mutation=mutation)
            )
        seen.add((mutation.node_type, mutation.node_id))

    if len(changes) < limit:
        changes.extend(
            await _derived_knowledge_changes(
                session,
                limit=limit - len(changes),
                skip=seen,
            )
        )

    changes.sort(key=lambda item: item.changed_at, reverse=True)
    return KnowledgeChangeSummaryResponse(
        active_facts=await _knowledge_count(session, Fact, False),
        active_claims=await _knowledge_count(session, Claim, False),
        active_events=await _knowledge_count(session, Event, False),
        deprecated_facts=await _knowledge_count(session, Fact, True),
        deprecated_claims=await _knowledge_count(session, Claim, True),
        deprecated_events=await _knowledge_count(session, Event, True),
        changes=changes[:limit],
    )


@router.get("", response_model=list[TimelineItemResponse])
async def get_timeline(
    limit: int = 50,
    item_type: Optional[str] = None,  # fact|claim|event
    session: AsyncSession = Depends(get_session),
):
    """Returns a unified timeline of recent knowledge extracted from evidence."""
    if limit <= 0:
        return []
    candidates = []
    for kind, model in (("event", Event), ("fact", Fact), ("claim", Claim)):
        if item_type not in (None, kind):
            continue
        rows = await session.scalars(
            select(model)
            .where(model.is_deprecated.is_(False))
            .order_by(desc(model.created_at))
            .limit(limit)
        )
        for node in rows:
            temporal = _temporal_context(node, item_type=kind)
            display_time, label = _display_time(node)
            score = (
                _event_signal_score(node)
                if isinstance(node, Event)
                else _fact_signal_score(node, temporal)
            )
            candidates.append((score, display_time, kind, node, label, temporal))

    # Rank before reading receipt and subject detail. Enrichment is still
    # required for the deduplication key, so stop only after enough unique rows.
    candidates.sort(key=lambda row: (row[0], row[1]), reverse=True)
    claim_ids = [node.id for _, _, kind, node, _, _ in candidates if kind == "claim"]
    claim_records = {}
    if claim_ids:
        claim_records = {
            record.claim_id: record
            for record in await session.scalars(
                select(SourceClaimRecord).where(
                    SourceClaimRecord.claim_id.in_(claim_ids)
                )
            )
        }

    items = []
    reads = _TimelineContextReads()
    seen: set[tuple[str, str, str | None, str | None]] = set()
    for score, display_time, kind, node, display_label, temporal in candidates:
        event = isinstance(node, Event)
        text = (node.description or node.title) if event else node.statement
        subject, sources = await _source_context(
            session,
            node_type=kind,
            node_id=node.id,
            source_item_id=None if event else node.source_item_id,
            reads=reads,
        )
        source = sources[0] if sources else None
        source_name = source.source_name if source else None
        fingerprint = (kind, text.strip().lower(), subject, source_name)
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        outcome = claim_records.get(node.id) if kind == "claim" else None
        due_at = source_claim_due_at(outcome, node) if outcome else None
        outcome_status = outcome.assessment if outcome else None
        if (
            outcome_status == "pending"
            and due_at is not None
            and due_at > datetime.now(UTC)
        ):
            outcome_status = "scheduled"
        items.append(
            TimelineItemResponse(
                id=node.id,
                item_type=kind,
                text=text,
                tier="event" if event else node.tier,
                importance=(
                    ("high" if node.is_evolving else "medium")
                    if event
                    else node.importance
                ),
                directness="primary" if event else node.directness,
                novelty=temporal.novelty,
                contradiction_role="neutral" if event else node.contradiction_role,
                signal_score=score,
                subject_name=subject,
                source_name=source_name,
                source_type=source.source_type if source else None,
                sources=sources,
                event_time=_effective_event_time(node),
                public_time=node.public_time,
                ingest_time=node.ingest_time,
                display_time=display_time,
                display_time_label=display_label,
                temporal_status=temporal.status,
                temporal_explanation=temporal.explanation,
                referenced_years=list(temporal.referenced_years),
                valid_until=node.valid_until,
                outcome_status=outcome_status,
                outcome_due_at=due_at,
                outcome_assessed_at=outcome.assessment_time if outcome else None,
                outcome_notes=outcome.notes if outcome else None,
                created_at=node.created_at,
            )
        )
        if len(items) >= limit:
            break
    return items
