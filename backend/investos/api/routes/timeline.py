from datetime import datetime
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from investos.db import get_session
from investos.models.evidence import RawEvidence, SourceItem
from investos.models.knowledge import Claim, Event, Fact
from investos.models.knowledge_mutation import KnowledgeMutation
from investos.models.source import Source
from investos.services.operating_state import OperatingStateService

router = APIRouter(prefix="/timeline", tags=["timeline"])


def _display_time(item) -> tuple[datetime, str]:
    if getattr(item, "event_time", None) is not None:
        return item.event_time, "happened"
    if getattr(item, "public_time", None) is not None:
        return item.public_time, "published"
    if getattr(item, "ingest_time", None) is not None:
        return item.ingest_time, "ingested"
    return item.created_at, "recorded"


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
    event_time: datetime | None = None
    public_time: datetime | None = None
    ingest_time: datetime | None = None
    display_time: datetime
    display_time_label: str
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
}

CONTRADICTION_BONUS = {
    "contradicts_consensus": 0.25,
    "ambiguous": 0.1,
    "supports_consensus": 0.0,
    "neutral": 0.0,
}


def _fact_signal_score(item: Fact | Claim) -> float:
    return round(
        (IMPORTANCE_SCORE.get(item.importance, 0.35) * 0.45)
        + (DIRECTNESS_SCORE.get(item.directness, 0.35) * 0.25)
        + (NOVELTY_SCORE.get(item.novelty, 0.2) * 0.2)
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


async def _source_context(
    session: AsyncSession,
    *,
    source_item_id: UUID | None,
) -> tuple[str | None, str | None, str | None]:
    if source_item_id is None:
        return None, None, None

    row = (
        await session.execute(
            select(SourceItem, RawEvidence, Source)
            .join(RawEvidence, SourceItem.raw_evidence_id == RawEvidence.id)
            .join(Source, SourceItem.source_id == Source.id)
            .where(SourceItem.id == source_item_id)
        )
    ).first()
    if row is None:
        return None, None, None

    _source_item, raw_evidence, source = row
    metadata = raw_evidence.metadata_json or {}
    subject_name = None
    raw_subject_id = metadata.get("subject_id")
    raw_subject_type = metadata.get("subject_type")
    if raw_subject_id and raw_subject_type:
        try:
            subject_name = await OperatingStateService(session).subject_name(
                UUID(str(raw_subject_id)),
                str(raw_subject_type),
            )
        except Exception as exc:
            import logging

            logging.getLogger(__name__).exception("Masked failure caught")
            subject_name = None

    return subject_name, source.name, source.source_type


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


@router.get("/", response_model=list[TimelineItemResponse])
async def get_timeline(
    limit: int = 50,
    item_type: Optional[str] = None,  # fact|claim|event
    session: AsyncSession = Depends(get_session),
):
    """Returns a unified timeline of recent knowledge extracted from evidence."""
    items = []

    if item_type in (None, "event"):
        events = (
            (
                await session.execute(
                    select(Event).order_by(desc(Event.created_at)).limit(limit)
                )
            )
            .scalars()
            .all()
        )
        for e in events:
            display_time, display_time_label = _display_time(e)
            items.append(
                TimelineItemResponse(
                    id=e.id,
                    item_type="event",
                    text=e.description or e.title,
                    tier="event",
                    importance="high" if e.is_evolving else "medium",
                    directness="primary",
                    novelty="breaking" if e.is_evolving else "confirming",
                    contradiction_role="neutral",
                    signal_score=_event_signal_score(e),
                    event_time=e.event_time,
                    public_time=e.public_time,
                    ingest_time=e.ingest_time,
                    display_time=display_time,
                    display_time_label=display_time_label,
                    created_at=e.created_at,
                )
            )

    if item_type in (None, "fact"):
        facts = (
            (
                await session.execute(
                    select(Fact).order_by(desc(Fact.created_at)).limit(limit)
                )
            )
            .scalars()
            .all()
        )
        for fact in facts:
            subject_name, source_name, source_type = await _source_context(
                session,
                source_item_id=fact.source_item_id,
            )
            display_time, display_time_label = _display_time(fact)
            items.append(
                TimelineItemResponse(
                    id=fact.id,
                    item_type="fact",
                    text=fact.statement,
                    tier=fact.tier,
                    importance=fact.importance,
                    directness=fact.directness,
                    novelty=fact.novelty,
                    contradiction_role=fact.contradiction_role,
                    signal_score=_fact_signal_score(fact),
                    subject_name=subject_name,
                    source_name=source_name,
                    source_type=source_type,
                    event_time=fact.event_time,
                    public_time=fact.public_time,
                    ingest_time=fact.ingest_time,
                    display_time=display_time,
                    display_time_label=display_time_label,
                    created_at=fact.created_at,
                )
            )

    if item_type in (None, "claim"):
        claims = (
            (
                await session.execute(
                    select(Claim).order_by(desc(Claim.created_at)).limit(limit)
                )
            )
            .scalars()
            .all()
        )
        for claim in claims:
            subject_name, source_name, source_type = await _source_context(
                session,
                source_item_id=claim.source_item_id,
            )
            display_time, display_time_label = _display_time(claim)
            items.append(
                TimelineItemResponse(
                    id=claim.id,
                    item_type="claim",
                    text=claim.statement,
                    tier=claim.tier,
                    importance=claim.importance,
                    directness=claim.directness,
                    novelty=claim.novelty,
                    contradiction_role=claim.contradiction_role,
                    signal_score=_fact_signal_score(claim),
                    subject_name=subject_name,
                    source_name=source_name,
                    source_type=source_type,
                    event_time=claim.event_time,
                    public_time=claim.public_time,
                    ingest_time=claim.ingest_time,
                    display_time=display_time,
                    display_time_label=display_time_label,
                    created_at=claim.created_at,
                )
            )

    items.sort(key=lambda x: (x.signal_score, x.display_time), reverse=True)

    deduped: list[TimelineItemResponse] = []
    seen: set[tuple[str, str, str | None, str | None]] = set()
    for item in items:
        fingerprint = (
            item.item_type,
            item.text.strip().lower(),
            item.subject_name,
            item.source_name,
        )
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        deduped.append(item)
        if len(deduped) >= limit:
            break

    return deduped
