import re
from uuid import UUID

from sqlalchemy import desc, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from investos.models.catalog import SourceTrustProfile, SourceValueProfile
from investos.models.conclusion import ConclusionState
from investos.models.coverage import CoverageMap
from investos.models.entity import Entity, Security
from investos.models.evidence import RawEvidence, SourceItem
from investos.models.graph import Edge
from investos.models.knowledge import Claim, Event, Fact
from investos.models.lesson import Lesson
from investos.models.portfolio import CashLedgerEntry, Lot, Position, Transaction
from investos.models.reasoning import EvidencePacket
from investos.models.source import Source, SourceQualitySegment
from investos.models.theme import Theme
from investos.models.watcher import ActiveWatcher
from investos.services.canonical_state import CanonicalStateService
from investos.services.corroboration import build_source_provenance
from investos.services.fundamentals import FundamentalMetricService
from investos.services.historical import HistoricalEpisodeService
from investos.services.market_setup import MarketSetupSignalService
from investos.services.operating_state import OperatingStateService
from investos.services.portfolio_peers import PortfolioPeerContextService
from investos.services.risk import RiskService


class RetrievalService:
    def __init__(self, session: AsyncSession):
        self.session = session
        self.canonical = CanonicalStateService(session)
        self.operating = OperatingStateService(session)

    async def retrieve_evidence(
        self,
        query: str,
        subject_id: UUID,
        subject_type: str,
        max_depth: int = 5,
    ) -> EvidencePacket:
        lesson_ids = await self._relevant_lesson_ids(
            query=query,
            subject_id=subject_id,
            subject_type=subject_type,
            max_lessons=min(4, max_depth),
        )
        if subject_type == "portfolio":
            return await self._retrieve_portfolio_packet(
                query=query,
                subject_id=subject_id,
                max_depth=max_depth,
                lesson_ids=lesson_ids,
            )
        direct_edges = list(
            (
                await self.session.execute(
                    select(Edge)
                    .where(
                        Edge.target_type == subject_type,
                        Edge.target_id == subject_id,
                        Edge.source_type.in_(["fact", "claim", "event"]),
                    )
                    .order_by(Edge.created_at.desc())
                )
            )
            .scalars()
            .all()
        )
        # Filter out deprecated nodes
        raw_fact_ids = [
            edge.source_id for edge in direct_edges if edge.source_type == "fact"
        ]
        raw_claim_ids = [
            edge.source_id for edge in direct_edges if edge.source_type == "claim"
        ]
        raw_event_ids = [
            edge.source_id for edge in direct_edges if edge.source_type == "event"
        ]

        # 1. Visionary Anchors (Permanent relevance)
        visionary_fact_ids = list(
            (
                await self.session.execute(
                    select(Fact.id).where(
                        Fact.id.in_(raw_fact_ids),
                        Fact.is_deprecated.is_(False),
                        Fact.target_horizon == "visionary",
                    )
                )
            )
            .scalars()
            .all()
        )
        visionary_claim_ids = list(
            (
                await self.session.execute(
                    select(Claim.id).where(
                        Claim.id.in_(raw_claim_ids),
                        Claim.is_deprecated.is_(False),
                        Claim.target_horizon == "visionary",
                    )
                )
            )
            .scalars()
            .all()
        )
        visionary_event_ids = list(
            (
                await self.session.execute(
                    select(Event.id).where(
                        Event.id.in_(raw_event_ids),
                        Event.is_deprecated.is_(False),
                        Event.target_horizon == "visionary",
                    )
                )
            )
            .scalars()
            .all()
        )
        anchors = (visionary_fact_ids + visionary_claim_ids + visionary_event_ids)[
            :max_depth
        ]

        # 2. Modern Context (Recency-biased)
        modern_fact_ids = list(
            (
                await self.session.execute(
                    select(Fact.id)
                    .where(
                        Fact.id.in_(raw_fact_ids),
                        Fact.is_deprecated.is_(False),
                        Fact.target_horizon != "visionary",
                    )
                    .order_by(Fact.created_at.desc())
                )
            )
            .scalars()
            .all()
        )
        modern_claim_ids = list(
            (
                await self.session.execute(
                    select(Claim.id)
                    .where(
                        Claim.id.in_(raw_claim_ids),
                        Claim.is_deprecated.is_(False),
                        Claim.target_horizon != "visionary",
                    )
                    .order_by(Claim.created_at.desc())
                )
            )
            .scalars()
            .all()
        )
        modern_event_ids = list(
            (
                await self.session.execute(
                    select(Event.id)
                    .where(
                        Event.id.in_(raw_event_ids),
                        Event.is_deprecated.is_(False),
                        Event.target_horizon != "visionary",
                    )
                    .order_by(Event.created_at.desc())
                )
            )
            .scalars()
            .all()
        )

        query_expansion_ids = await self._query_expansion_ids(
            query=query,
            subject_id=subject_id,
            subject_type=subject_type,
            seed_node_ids=raw_fact_ids + raw_claim_ids + raw_event_ids,
            max_items=max_depth,
        )

        direct_ids = (anchors + modern_fact_ids + modern_claim_ids + modern_event_ids)[
            : max_depth * 3
        ]
        connected_ids = (
            modern_fact_ids[:max_depth]
            + modern_claim_ids[:max_depth]
            + query_expansion_ids[:max_depth]
        )[: max_depth * 3]
        historical_ids = anchors if anchors else list(reversed(direct_ids[-max_depth:]))

        fact_ids = visionary_fact_ids + modern_fact_ids
        claim_ids = visionary_claim_ids + modern_claim_ids

        contradiction_ids = []
        if claim_ids:
            contradiction_ids = [
                claim.id
                for claim in (
                    await self.session.execute(
                        select(Claim).where(Claim.id.in_(claim_ids))
                    )
                )
                .scalars()
                .all()
                if claim.contradiction_role == "contradicts_consensus"
            ]

        coverage = await self.canonical.get_coverage_map(
            subject_type=subject_type,
            subject_id=subject_id,
        )
        conclusion = await self.canonical.get_conclusion_state(
            subject_type=subject_type,
            subject_id=subject_id,
        )

        packet = EvidencePacket(
            query_text=query,
            subject_type=subject_type,
            subject_id=subject_id,
            direct_evidence_ids=direct_ids,
            connected_evidence_ids=connected_ids,
            historical_evidence_ids=historical_ids,
            contradiction_evidence_ids=contradiction_ids,
            lesson_ids=lesson_ids,
            portfolio_context_json=await self._portfolio_context(
                subject_type,
                subject_id=subject_id,
                query=query,
            ),
            coverage_map_snapshot_json=self._coverage_snapshot(coverage, conclusion),
            retrieval_layers_used=[
                "direct_evidence",
                "connected_evidence",
                "query_expansion",
                "historical_context",
                "contradiction_evidence",
                "lessons",
                "portfolio_relevance",
            ],
            gap_flags=self._gap_flags(coverage),
            total_token_estimate=max(
                1, len(direct_ids) + len(connected_ids) + len(lesson_ids)
            )
            * 120,
        )
        self.session.add(packet)
        await self.session.commit()
        await self.session.refresh(packet)
        return packet

    async def _retrieve_portfolio_packet(
        self,
        *,
        query: str,
        subject_id: UUID,
        max_depth: int,
        lesson_ids: list[UUID],
    ) -> EvidencePacket:
        terms = self._query_terms(query)
        fact_filter = Fact.is_deprecated.is_(False)
        claim_filter = Claim.is_deprecated.is_(False)
        event_filter = Event.is_deprecated.is_(False)

        if terms:
            fact_filter = fact_filter & or_(
                *[Fact.statement.ilike(f"%{t}%") for t in terms]
            )
            claim_filter = claim_filter & or_(
                *[Claim.statement.ilike(f"%{t}%") for t in terms]
            )
            event_filter = event_filter & or_(
                *[Event.title.ilike(f"%{t}%") for t in terms],
                *[Event.description.ilike(f"%{t}%") for t in terms],
            )

        facts = (
            (
                await self.session.execute(
                    select(Fact)
                    .where(fact_filter)
                    .order_by(desc(Fact.created_at))
                    .limit(max_depth)
                )
            )
            .scalars()
            .all()
        )
        claims = (
            (
                await self.session.execute(
                    select(Claim)
                    .where(claim_filter)
                    .order_by(desc(Claim.created_at))
                    .limit(max_depth)
                )
            )
            .scalars()
            .all()
        )
        events = (
            (
                await self.session.execute(
                    select(Event)
                    .where(event_filter)
                    .order_by(desc(Event.created_at))
                    .limit(max_depth)
                )
            )
            .scalars()
            .all()
        )

        # If no matches found, we return empty so the reasoning layer can correctly identify a thin packet
        # and provide a thoughtful 'no evidence found' response instead of seeing irrelevant recent data.

        contradiction_ids = [
            claim.id
            for claim in claims
            if claim.contradiction_role == "contradicts_consensus"
        ][:max_depth]
        direct_ids = [
            *(item.id for item in facts[:max_depth]),
            *(item.id for item in claims[:max_depth]),
            *(item.id for item in events[:max_depth]),
        ]
        connected_ids = [
            *(item.id for item in claims[:max_depth]),
            *(item.id for item in facts[:max_depth]),
        ]
        historical_ids = [
            *(item.id for item in events[:max_depth]),
            *(item.id for item in facts[:max_depth]),
        ]
        conclusion = await self.canonical.get_conclusion_state(
            subject_type="portfolio",
            subject_id=subject_id,
        )
        packet = EvidencePacket(
            query_text=query,
            subject_type="portfolio",
            subject_id=subject_id,
            direct_evidence_ids=direct_ids[: max_depth * 3],
            connected_evidence_ids=connected_ids[: max_depth * 2],
            historical_evidence_ids=historical_ids[: max_depth * 2],
            contradiction_evidence_ids=contradiction_ids,
            lesson_ids=lesson_ids,
            portfolio_context_json=await self._portfolio_context(
                "portfolio",
                subject_id=subject_id,
                query=query,
            ),
            coverage_map_snapshot_json=self._coverage_snapshot(None, conclusion),
            retrieval_layers_used=[
                "direct_evidence",
                "connected_evidence",
                "historical_context",
                "contradiction_evidence",
                "lessons",
                "portfolio_relevance",
            ],
            gap_flags=["portfolio_scope"],
            total_token_estimate=max(
                1, len(direct_ids) + len(connected_ids) + len(lesson_ids)
            )
            * 120,
        )
        self.session.add(packet)
        await self.session.commit()
        await self.session.refresh(packet)
        return packet

    async def _query_expansion_ids(
        self,
        *,
        query: str,
        subject_id: UUID,
        subject_type: str,
        seed_node_ids: list[UUID],
        max_items: int,
    ) -> list[UUID]:
        terms = self._query_terms(query)
        if not terms or subject_type == "portfolio":
            return []

        subject_neighbors = await self._subject_neighbor_refs(
            subject_id=subject_id, subject_type=subject_type
        )
        candidate_fact_ids = await self._matching_node_ids(
            Fact,
            or_(*[Fact.statement.ilike(f"%{term}%") for term in terms]),
            limit=max_items * 3,
        )
        candidate_claim_ids = await self._matching_node_ids(
            Claim,
            or_(*[Claim.statement.ilike(f"%{term}%") for term in terms]),
            limit=max_items * 3,
        )
        candidate_event_ids = await self._matching_node_ids(
            Event,
            (
                or_(
                    Event.title.ilike(f"%{terms[0]}%"),
                    Event.description.ilike(f"%{terms[0]}%"),
                )
                if len(terms) == 1
                else or_(
                    *[Event.title.ilike(f"%{term}%") for term in terms],
                    *[Event.description.ilike(f"%{term}%") for term in terms],
                )
            ),
            limit=max_items * 3,
        )

        expanded: list[UUID] = []
        seen = set(seed_node_ids)
        for node_type, node_ids in (
            ("fact", candidate_fact_ids),
            ("claim", candidate_claim_ids),
            ("event", candidate_event_ids),
        ):
            for node_id in node_ids:
                if node_id in seen:
                    continue
                if await self._node_touches_subject_context(
                    node_type=node_type,
                    node_id=node_id,
                    subject_id=subject_id,
                    subject_type=subject_type,
                    subject_neighbors=subject_neighbors,
                ):
                    expanded.append(node_id)
                    seen.add(node_id)
                    if len(expanded) >= max_items:
                        return expanded
        return expanded

    def _query_terms(self, query: str) -> list[str]:
        tokens = re.findall(r"[A-Za-z0-9][A-Za-z0-9+./-]*", (query or "").lower())
        stopwords = {
            "a",
            "an",
            "and",
            "are",
            "as",
            "at",
            "be",
            "because",
            "by",
            "do",
            "for",
            "from",
            "how",
            "i",
            "if",
            "in",
            "into",
            "is",
            "it",
            "its",
            "of",
            "on",
            "or",
            "over",
            "plan",
            "sometime",
            "someday",
            "should",
            "that",
            "the",
            "their",
            "they",
            "this",
            "to",
            "what",
            "when",
            "where",
            "which",
            "who",
            "why",
            "with",
            "would",
            "you",
            "your",
            "think",
            "holding",
            "hold",
            "while",
            "because",
            "believe",
        }
        terms: list[str] = []
        for token in tokens:
            if token in stopwords:
                continue
            if len(token) <= 2:
                continue
            if token.endswith("s") and len(token) > 5:
                terms.append(token[:-1])
            terms.append(token)
        deduped: list[str] = []
        seen: set[str] = set()
        for term in terms:
            if term in seen:
                continue
            seen.add(term)
            deduped.append(term)
        return deduped[:8]

    async def _subject_neighbor_refs(
        self,
        *,
        subject_id: UUID,
        subject_type: str,
    ) -> set[tuple[str, UUID]]:
        rows = (
            await self.session.execute(
                select(
                    Edge.source_type,
                    Edge.source_id,
                    Edge.target_type,
                    Edge.target_id,
                ).where(
                    or_(
                        (Edge.source_type == subject_type)
                        & (Edge.source_id == subject_id),
                        (Edge.target_type == subject_type)
                        & (Edge.target_id == subject_id),
                    )
                )
            )
        ).all()
        refs: set[tuple[str, UUID]] = {(subject_type, subject_id)}
        for source_type, source_id, target_type, target_id in rows:
            refs.add((str(source_type), source_id))
            refs.add((str(target_type), target_id))
        return refs

    async def _matching_node_ids(self, model, text_filter, *, limit: int) -> list[UUID]:
        rows = (
            (
                await self.session.execute(
                    select(model.id)
                    .where(text_filter)
                    .order_by(desc(model.created_at))
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        return list(rows)

    async def _node_touches_subject_context(
        self,
        *,
        node_type: str,
        node_id: UUID,
        subject_id: UUID,
        subject_type: str,
        subject_neighbors: set[tuple[str, UUID]],
    ) -> bool:
        rows = (
            await self.session.execute(
                select(
                    Edge.source_type,
                    Edge.source_id,
                    Edge.target_type,
                    Edge.target_id,
                ).where(
                    or_(
                        (Edge.source_type == node_type) & (Edge.source_id == node_id),
                        (Edge.target_type == node_type) & (Edge.target_id == node_id),
                    )
                )
            )
        ).all()
        for source_type, source_id, target_type, target_id in rows:
            if (str(source_type), source_id) in subject_neighbors or (
                str(target_type),
                target_id,
            ) in subject_neighbors:
                return True
        return False

    async def hydrate_packet(self, packet: EvidencePacket) -> dict:
        subject_name = await self.operating.subject_name(
            packet.subject_id, packet.subject_type
        )
        analogy_query = " ".join(
            item
            for item in [
                packet.query_text,
                subject_name,
                packet.subject_type,
            ]
            if item
        )
        historical_analogies = await HistoricalEpisodeService(
            self.session
        ).find_analogies(analogy_query)
        portfolio_context = packet.portfolio_context_json or {}
        historical_analogy_lenses = HistoricalEpisodeService.application_lenses(
            historical_analogies,
            query_text=packet.query_text,
            subject_name=subject_name,
            portfolio_context=portfolio_context,
        )
        return {
            "query_text": packet.query_text,
            "subject_type": packet.subject_type,
            "subject_id": packet.subject_id,
            "subject_name": subject_name,
            "direct_evidence": await self._load_nodes(packet.direct_evidence_ids or []),
            "connected_evidence": await self._load_nodes(
                packet.connected_evidence_ids or []
            ),
            "historical_evidence": await self._load_nodes(
                packet.historical_evidence_ids or []
            ),
            "contradiction_evidence": await self._load_nodes(
                packet.contradiction_evidence_ids or []
            ),
            "lessons": await self._load_lessons(packet.lesson_ids or []),
            "historical_analogies": historical_analogies,
            "historical_analogy_context": HistoricalEpisodeService.as_context_text(
                historical_analogies
            ),
            "historical_analogy_lenses": historical_analogy_lenses,
            "portfolio_context": portfolio_context,
            "coverage": packet.coverage_map_snapshot_json or {},
            "gap_flags": packet.gap_flags or [],
        }

    async def _load_nodes(self, node_ids: list[UUID]) -> list[dict]:
        if not node_ids:
            return []
        facts = {
            item.id: item
            for item in (
                await self.session.execute(select(Fact).where(Fact.id.in_(node_ids)))
            )
            .scalars()
            .all()
        }
        claims = {
            item.id: item
            for item in (
                await self.session.execute(select(Claim).where(Claim.id.in_(node_ids)))
            )
            .scalars()
            .all()
        }
        events = {
            item.id: item
            for item in (
                await self.session.execute(select(Event).where(Event.id.in_(node_ids)))
            )
            .scalars()
            .all()
        }
        source_item_ids = {
            item.source_item_id
            for item in [*facts.values(), *claims.values()]
            if getattr(item, "source_item_id", None)
        }
        source_contexts: dict[UUID, dict] = {}
        if source_item_ids:
            source_rows = (
                await self.session.execute(
                    select(SourceItem, RawEvidence, Source)
                    .join(RawEvidence, SourceItem.raw_evidence_id == RawEvidence.id)
                    .join(Source, RawEvidence.source_id == Source.id)
                    .where(SourceItem.id.in_(source_item_ids))
                )
            ).all()
            source_ids = {source.id for _source_item, _evidence, source in source_rows}
            quality_segments: dict[UUID, SourceQualitySegment] = {}
            trust_profiles: dict[UUID, SourceTrustProfile] = {}
            value_profiles: dict[UUID, SourceValueProfile] = {}
            if source_ids:
                for segment in (
                    await self.session.execute(
                        select(SourceQualitySegment)
                        .where(SourceQualitySegment.source_id.in_(source_ids))
                        .order_by(
                            desc(SourceQualitySegment.last_evaluated),
                            desc(SourceQualitySegment.quality_score),
                        )
                    )
                ).scalars():
                    quality_segments.setdefault(segment.source_id, segment)
                trust_profiles = {
                    profile.source_id: profile
                    for profile in (
                        await self.session.execute(
                            select(SourceTrustProfile).where(
                                SourceTrustProfile.source_id.in_(source_ids)
                            )
                        )
                    ).scalars()
                }
                value_profiles = {
                    profile.source_id: profile
                    for profile in (
                        await self.session.execute(
                            select(SourceValueProfile).where(
                                SourceValueProfile.source_id.in_(source_ids)
                            )
                        )
                    ).scalars()
                }
            source_contexts = {
                source_item.id: self._source_context_from_evidence(
                    evidence,
                    source,
                    source_item_id=source_item.id,
                    quality_segment=quality_segments.get(source.id),
                    trust_profile=trust_profiles.get(source.id),
                    value_profile=value_profiles.get(source.id),
                )
                for source_item, evidence, source in source_rows
            }
        nodes: list[dict] = []
        for node_id in node_ids:
            if node_id in facts:
                fact = facts[node_id]
                nodes.append(
                    {
                        "id": fact.id,
                        "type": "fact",
                        "text": fact.statement,
                        "tier": fact.tier,
                        "importance": fact.importance,
                        "directness": fact.directness,
                        "is_verified": bool(fact.is_verified),
                        "promotion_eligible": bool(fact.promotion_eligible),
                        "horizon": getattr(fact, "target_horizon", "strategic"),
                        "horizon_reasoning": getattr(fact, "horizon_reasoning", None),
                        "created_at": fact.created_at.isoformat(),
                        "source": source_contexts.get(fact.source_item_id),
                    }
                )
            elif node_id in claims:
                claim = claims[node_id]
                nodes.append(
                    {
                        "id": claim.id,
                        "type": "claim",
                        "text": claim.statement,
                        "tier": claim.tier,
                        "importance": claim.importance,
                        "directness": claim.directness,
                        "is_verified": bool(claim.is_verified),
                        "promotion_eligible": bool(claim.promotion_eligible),
                        "horizon": getattr(claim, "target_horizon", "strategic"),
                        "horizon_reasoning": getattr(claim, "horizon_reasoning", None),
                        "contradiction_role": claim.contradiction_role,
                        "created_at": claim.created_at.isoformat(),
                        "valid_until": (
                            claim.valid_until.isoformat()
                            if getattr(claim, "valid_until", None)
                            else None
                        ),
                        "source": source_contexts.get(claim.source_item_id),
                    }
                )
            elif node_id in events:
                event = events[node_id]
                nodes.append(
                    {
                        "id": event.id,
                        "type": "event",
                        "text": event.description or event.title,
                        "tier": "event",
                        "importance": "medium",
                        "horizon": getattr(event, "target_horizon", "strategic"),
                        "created_at": event.created_at.isoformat(),
                    }
                )
        return nodes

    @staticmethod
    def _source_context_from_evidence(
        evidence: RawEvidence,
        source: Source,
        *,
        source_item_id: UUID | None = None,
        quality_segment: SourceQualitySegment | None = None,
        trust_profile: SourceTrustProfile | None = None,
        value_profile: SourceValueProfile | None = None,
    ) -> dict:
        metadata = evidence.metadata_json or {}
        feedback = metadata.get("user_feedback")
        feedback_context = None
        if isinstance(feedback, dict) and feedback.get("rating"):
            feedback_context = {
                "rating": feedback.get("rating"),
                "note": feedback.get("note"),
                "context": feedback.get("context"),
                "flagged_at": feedback.get("flagged_at"),
            }
        return {
            "name": source.name,
            "type": source.source_type,
            "is_trusted": bool(source.is_trusted),
            "evidence_title": evidence.title,
            "url": evidence.url,
            **build_source_provenance(
                source_id=getattr(source, "id", None),
                source_type=source.source_type,
                source_url=getattr(source, "url", None),
                source_item_id=source_item_id,
                raw_evidence_id=getattr(evidence, "id", None),
                evidence_url=evidence.url,
                content_hash=getattr(evidence, "content_hash", None),
                public_time=getattr(evidence, "public_time", None),
                event_time=getattr(evidence, "event_time", None),
                ingest_time=getattr(evidence, "ingest_time", None),
                metadata=metadata,
            ),
            "feedback": feedback_context,
            "quality": (
                None
                if quality_segment is None
                else {
                    "quality_score": float(quality_segment.quality_score or 0.0),
                    "originality_score": float(
                        quality_segment.originality_score or 0.0
                    ),
                    "timing_usefulness": float(
                        quality_segment.timing_usefulness or 0.0
                    ),
                    "evidence_count": int(quality_segment.evidence_count or 0),
                    "notes": quality_segment.notes,
                    "last_evaluated": (
                        quality_segment.last_evaluated.isoformat()
                        if quality_segment.last_evaluated
                        else None
                    ),
                }
            ),
            "trust_profile": (
                None
                if trust_profile is None
                else {
                    "factual_reliability": trust_profile.factual_reliability,
                    "noise_ratio": trust_profile.noise_ratio,
                    "trust_trajectory": trust_profile.trust_trajectory,
                    "correction_quality": trust_profile.correction_quality,
                }
            ),
            "value_profile": (
                None
                if value_profile is None
                else {
                    "idea_generation_value": value_profile.idea_generation_value,
                    "timing_value": value_profile.timing_value,
                    "portfolio_relevance_value": value_profile.portfolio_relevance_value,
                    "specificity": value_profile.specificity,
                    "originality": value_profile.originality,
                }
            ),
        }

    async def _load_lessons(self, lesson_ids: list[UUID]) -> list[dict]:
        if not lesson_ids:
            return []
        lessons = {
            item.id: item
            for item in (
                await self.session.execute(
                    select(Lesson).where(Lesson.id.in_(lesson_ids))
                )
            )
            .scalars()
            .all()
        }
        hydrated: list[dict] = []
        for lesson_id in lesson_ids:
            lesson = lessons.get(lesson_id)
            if lesson is None:
                continue
            hydrated.append(
                {
                    "id": lesson.id,
                    "title": lesson.title,
                    "summary": lesson.summary,
                    "lesson_type": lesson.lesson_type,
                    "applicable_sectors": lesson.applicable_sectors or [],
                    "applicable_regimes": lesson.applicable_regimes or [],
                    "maturity_status": lesson.maturity_status,
                    "confidence_score": lesson.confidence_score,
                    "supporting_observations": lesson.supporting_observations,
                    "contradicting_observations": lesson.contradicting_observations,
                    "neutral_observations": lesson.neutral_observations,
                    "stale_after": (
                        lesson.stale_after.isoformat() if lesson.stale_after else None
                    ),
                    "usage_count": lesson.usage_count,
                    "created_at": lesson.created_at.isoformat(),
                }
            )
        return hydrated

    async def _portfolio_context(
        self,
        subject_type: str,
        *,
        subject_id: UUID | None = None,
        query: str | None = None,
    ) -> dict:
        position_rows = (
            await self.session.execute(
                select(Position, Security, Entity)
                .join(Security, Position.security_id == Security.id)
                .join(Entity, Security.entity_id == Entity.id)
                .where(Position.list_type.in_(["holding", "watchlist", "considering"]))
            )
        ).all()
        positions = [row[0] for row in position_rows]
        if not positions:
            trusted_sources = (
                (
                    await self.session.execute(
                        select(Source)
                        .where(Source.is_trusted.is_(True))
                        .order_by(Source.updated_at.desc())
                        .limit(5)
                    )
                )
                .scalars()
                .all()
            )
            source_feedback = await self._recent_source_feedback()
            return {
                "tracked_positions": 0,
                "holdings": [],
                "watchlist": [],
                "considering": [],
                "remaining_buying_power": 0.0,
                "pct_capital_deployed": 0.0,
                "trusted_sources": [
                    {"name": source.name, "source_type": source.source_type}
                    for source in trusted_sources
                ],
                "source_feedback": source_feedback,
                "peer_exposures": [],
                "subject_peer_exposures": [],
                "active_watchers": [],
                "subject_watchers": [],
                "fundamental_metrics": [],
                "subject_fundamental_metrics": [],
                "market_setup_signals": [],
                "subject_market_setup_signals": [],
                "performance_attribution": None,
            }

        position_details = {
            position.id: {
                "position_id": str(position.id),
                "security_id": str(security.id),
                "entity_id": str(entity.id),
                "ticker": security.ticker,
                "name": entity.name,
                "list_type": position.list_type,
                "quantity": float(position.quantity or 0),
                "market_value": float(position.market_value or 0),
                "weight_pct": float(position.weight_pct or 0),
                "current_price": float(position.current_price or 0),
                "sector": entity.sector,
                "industry": entity.industry,
            }
            for position, security, entity in position_rows
        }
        holdings = [
            position for position in positions if position.list_type == "holding"
        ]
        watchlist = [
            position for position in positions if position.list_type == "watchlist"
        ]
        considering = [
            position for position in positions if position.list_type == "considering"
        ]
        recent_transactions = (
            (
                await self.session.execute(
                    select(Transaction).order_by(desc(Transaction.executed_at)).limit(8)
                )
            )
            .scalars()
            .all()
        )
        trusted_sources = (
            (
                await self.session.execute(
                    select(Source)
                    .where(Source.is_trusted.is_(True))
                    .order_by(Source.updated_at.desc())
                    .limit(5)
                )
            )
            .scalars()
            .all()
        )
        source_feedback = await self._recent_source_feedback()

        total_market_value = sum(
            float(position.market_value or 0) for position in holdings
        )
        latest_cash = (
            await self.session.execute(
                select(CashLedgerEntry)
                .order_by(
                    desc(CashLedgerEntry.executed_at), desc(CashLedgerEntry.created_at)
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        buying_power = float(latest_cash.balance_after) if latest_cash else 0.0
        total_equity = total_market_value + buying_power
        pct_deployed = (
            round((total_market_value / total_equity) * 100, 1)
            if total_equity > 0
            else 0.0
        )
        peer_service = PortfolioPeerContextService(self.session)
        peer_exposures = await peer_service.peer_exposures(limit=12)
        subject_peer_exposures = (
            await peer_service.peer_exposures(subject_id=subject_id, limit=8)
            if subject_type == "entity" and subject_id is not None
            else []
        )
        active_watchers = await self._active_watchers_for_positions(
            position_details=position_details,
            subject_id=subject_id if subject_type == "entity" else None,
        )
        market_setup_service = MarketSetupSignalService(self.session)
        fundamental_service = FundamentalMetricService(self.session)
        fundamental_metrics = await fundamental_service.relevant_metrics(
            subject_type="portfolio",
            subject_id=subject_id,
            position_details=position_details,
            limit=16,
        )
        subject_fundamental_metrics = (
            await fundamental_service.relevant_metrics(
                subject_type=subject_type,
                subject_id=subject_id,
                position_details=position_details,
                limit=10,
            )
            if subject_id is not None
            else []
        )
        market_setup_signals = await market_setup_service.relevant_signals(
            subject_type="portfolio",
            subject_id=subject_id,
            query=query,
            position_details=position_details,
            limit=12,
        )
        subject_market_setup_signals = (
            await market_setup_service.relevant_signals(
                subject_type=subject_type,
                subject_id=subject_id,
                query=query,
                position_details=position_details,
                limit=8,
            )
            if subject_id is not None
            else []
        )
        performance_attribution = await RiskService(
            self.session
        ).get_cached_performance_attribution(window_days=21)

        return {
            "tracked_positions": len(positions),
            "holdings_count": len(holdings),
            "watchlist_count": len(watchlist),
            "considering_count": len(considering),
            "total_market_value": round(total_market_value, 2),
            "remaining_buying_power": round(buying_power, 2),
            "pct_capital_deployed": pct_deployed,
            "top_holdings": [
                position_details[position.id]
                for position in sorted(
                    holdings,
                    key=lambda item: float(item.market_value or 0),
                    reverse=True,
                )[:5]
            ],
            "watchlist": [position_details[position.id] for position in watchlist[:6]],
            "considering": [
                position_details[position.id] for position in considering[:6]
            ],
            "recent_transactions": [
                {
                    "position_id": str(txn.position_id),
                    "ticker": position_details.get(txn.position_id, {}).get("ticker"),
                    "name": position_details.get(txn.position_id, {}).get("name"),
                    "action": txn.action,
                    "quantity": float(txn.quantity or 0),
                    "price": None if txn.price is None else float(txn.price),
                    "executed_at": txn.executed_at.isoformat(),
                }
                for txn in recent_transactions
            ],
            "trusted_sources": [
                {"name": source.name, "source_type": source.source_type}
                for source in trusted_sources
            ],
            "source_feedback": source_feedback,
            "peer_exposures": peer_exposures,
            "subject_peer_exposures": subject_peer_exposures,
            "active_watchers": active_watchers[:20],
            "subject_watchers": [
                watcher
                for watcher in active_watchers
                if subject_type == "entity"
                and subject_id is not None
                and (
                    watcher.get("entity_id") == str(subject_id)
                    or watcher.get("ticker")
                    in {
                        detail["ticker"]
                        for detail in position_details.values()
                        if detail.get("entity_id") == str(subject_id)
                    }
                )
            ][:8],
            "fundamental_metrics": fundamental_metrics,
            "subject_fundamental_metrics": subject_fundamental_metrics,
            "market_setup_signals": market_setup_signals,
            "subject_market_setup_signals": subject_market_setup_signals,
            "performance_attribution": (
                performance_attribution.model_dump(mode="json")
                if performance_attribution is not None
                else None
            ),
        }

    async def _active_watchers_for_positions(
        self,
        *,
        position_details: dict[UUID, dict],
        subject_id: UUID | None,
    ) -> list[dict]:
        tickers = {
            str(detail.get("ticker") or "").upper()
            for detail in position_details.values()
            if detail.get("ticker")
        }
        entity_ids = {
            UUID(str(detail.get("entity_id")))
            for detail in position_details.values()
            if detail.get("entity_id")
        }
        if subject_id is not None:
            entity_ids.add(subject_id)
        filters = []
        if tickers:
            filters.append(ActiveWatcher.ticker.in_(sorted(tickers)))
        if entity_ids:
            filters.append(ActiveWatcher.entity_id.in_(entity_ids))
        if not filters:
            return []
        rows = (
            (
                await self.session.execute(
                    select(ActiveWatcher)
                    .where(
                        ActiveWatcher.is_active.is_(True),
                        ActiveWatcher.status == "pending",
                        or_(*filters),
                    )
                    .order_by(desc(ActiveWatcher.created_at))
                    .limit(60)
                )
            )
            .scalars()
            .all()
        )
        output: list[dict] = []
        for watcher in rows:
            output.append(
                {
                    "ticker": watcher.ticker,
                    "entity_id": str(watcher.entity_id) if watcher.entity_id else None,
                    "condition_type": watcher.condition_type,
                    "condition_params": watcher.condition_params_json or {},
                    "objective": watcher.objective,
                    "adjustment_plan": watcher.adjustment_plan,
                    "deadline": (
                        watcher.deadline.isoformat() if watcher.deadline else None
                    ),
                    "created_at": watcher.created_at.isoformat(),
                }
            )
        return output

    async def _recent_source_feedback(self, limit: int = 8) -> dict:
        rows = (
            await self.session.execute(
                select(RawEvidence, Source)
                .join(Source, RawEvidence.source_id == Source.id)
                .where(RawEvidence.metadata_json.is_not(None))
                .order_by(desc(RawEvidence.updated_at))
                .limit(200)
            )
        ).all()
        items: list[dict] = []
        counts = {"useful": 0, "not_useful": 0}
        for evidence, source in rows:
            feedback = (evidence.metadata_json or {}).get("user_feedback")
            if not isinstance(feedback, dict):
                continue
            rating = str(feedback.get("rating") or "").strip()
            if rating not in counts:
                continue
            counts[rating] += 1
            if len(items) < limit:
                items.append(
                    {
                        "rating": rating,
                        "source_name": source.name,
                        "source_type": source.source_type,
                        "title": evidence.title,
                        "note": feedback.get("note"),
                        "context": feedback.get("context"),
                        "flagged_at": feedback.get("flagged_at"),
                    }
                )
        return {"counts": counts, "recent": items}

    async def _relevant_lesson_ids(
        self,
        *,
        query: str,
        subject_id: UUID,
        subject_type: str,
        max_lessons: int,
    ) -> list[UUID]:
        lessons = (
            (
                await self.session.execute(
                    select(Lesson).order_by(desc(Lesson.created_at)).limit(80)
                )
            )
            .scalars()
            .all()
        )
        if not lessons:
            return []
        signals = await self._lesson_signals(
            subject_type=subject_type, subject_id=subject_id
        )
        query_terms = self._meaningful_terms(query)
        context_terms = {
            *query_terms,
            *signals["subject_terms"],
            *signals["portfolio_terms"],
            *signals["sector_terms"],
        }
        ranked: list[tuple[float, Lesson]] = []
        for lesson in lessons:
            lesson_text_terms = self._meaningful_terms(
                f"{lesson.title} {lesson.summary}"
            )
            overlap = len(context_terms & lesson_text_terms)
            sector_overlap = len(
                set(lesson.applicable_sectors or []) & signals["sectors"]
            )
            regime_overlap = len(
                set(lesson.applicable_regimes or []) & signals["regimes"]
            )
            recency_bonus = max(0.0, 0.6 - (len(ranked) * 0.0))
            score = (
                overlap * 1.0
                + sector_overlap * 2.5
                + regime_overlap * 2.0
                + recency_bonus
            )
            if score <= 0:
                continue
            ranked.append((score, lesson))
        ranked.sort(key=lambda item: (item[0], item[1].created_at), reverse=True)
        chosen = [lesson for _, lesson in ranked[:max_lessons]]
        for lesson in chosen:
            lesson.usage_count = int(lesson.usage_count or 0) + 1
        return [lesson.id for lesson in chosen]

    async def _lesson_signals(
        self, *, subject_type: str, subject_id: UUID
    ) -> dict[str, set[str]]:
        subject_terms: set[str] = set()
        portfolio_terms: set[str] = set()
        sectors: set[str] = set()
        sector_terms: set[str] = set()
        regimes: set[str] = set()

        holding_rows = (
            await self.session.execute(
                select(Position, Security, Entity)
                .join(Security, Position.security_id == Security.id)
                .join(Entity, Security.entity_id == Entity.id)
                .where(Position.list_type == "holding", Position.quantity > 0)
            )
        ).all()
        for position, security, entity in holding_rows:
            portfolio_terms |= self._meaningful_terms(
                f"{security.ticker} {entity.name}"
            )
            if entity.sector:
                sectors.add(entity.sector)
                sector_terms |= self._meaningful_terms(entity.sector)
            if entity.industry:
                sector_terms |= self._meaningful_terms(entity.industry)

        if subject_type == "entity":
            entity = (
                await self.session.execute(
                    select(Entity).where(Entity.id == subject_id)
                )
            ).scalar_one_or_none()
            if entity is not None:
                subject_terms |= self._meaningful_terms(
                    " ".join(
                        filter(
                            None,
                            [
                                entity.name,
                                entity.description,
                                entity.industry,
                                entity.country,
                            ],
                        )
                    )
                )
                if entity.sector:
                    sectors.add(entity.sector)
                    sector_terms |= self._meaningful_terms(entity.sector)
                if entity.industry:
                    sector_terms |= self._meaningful_terms(entity.industry)
        elif subject_type == "theme":
            theme = (
                await self.session.execute(select(Theme).where(Theme.id == subject_id))
            ).scalar_one_or_none()
            if theme is not None:
                subject_terms |= self._meaningful_terms(
                    f"{theme.name} {theme.description or ''}"
                )
                tagged_ids = theme.tagged_security_ids or []
                if tagged_ids:
                    tagged_rows = (
                        await self.session.execute(
                            select(Security, Entity)
                            .join(Entity, Security.entity_id == Entity.id)
                            .where(Security.id.in_(tagged_ids))
                        )
                    ).all()
                    for security, entity in tagged_rows:
                        subject_terms |= self._meaningful_terms(
                            f"{security.ticker} {entity.name}"
                        )
                        if entity.sector:
                            sectors.add(entity.sector)
                            sector_terms |= self._meaningful_terms(entity.sector)
                        if entity.industry:
                            sector_terms |= self._meaningful_terms(entity.industry)
        elif subject_type == "portfolio":
            subject_terms |= portfolio_terms
        elif subject_type == "position":
            row = (
                await self.session.execute(
                    select(Position, Security, Entity)
                    .join(Security, Position.security_id == Security.id)
                    .join(Entity, Security.entity_id == Entity.id)
                    .where(Position.id == subject_id)
                )
            ).first()
            if row is not None:
                _, security, entity = row
                subject_terms |= self._meaningful_terms(
                    f"{security.ticker} {entity.name}"
                )
                if entity.sector:
                    sectors.add(entity.sector)
                    sector_terms |= self._meaningful_terms(entity.sector)
                if entity.industry:
                    sector_terms |= self._meaningful_terms(entity.industry)

        return {
            "subject_terms": subject_terms,
            "portfolio_terms": portfolio_terms,
            "sector_terms": sector_terms,
            "sectors": sectors,
            "regimes": regimes,
        }

    @staticmethod
    def _meaningful_terms(text: str | None) -> set[str]:
        if not text:
            return set()
        raw = re.findall(r"[A-Za-z0-9][A-Za-z0-9_/-]{2,}", text.lower())
        stop = {
            "the",
            "and",
            "for",
            "with",
            "that",
            "this",
            "from",
            "into",
            "over",
            "than",
            "have",
            "has",
            "had",
            "will",
            "would",
            "could",
            "should",
            "about",
            "after",
            "before",
            "through",
            "across",
            "because",
            "while",
            "where",
            "which",
            "their",
            "they",
            "them",
            "then",
            "into",
            "onto",
            "also",
            "just",
            "being",
            "been",
            "what",
            "when",
            "how",
            "why",
            "does",
            "did",
            "still",
            "your",
            "portfolio",
            "research",
            "question",
            "reasoning",
            "analysis",
            "market",
            "company",
        }
        return {term for term in raw if term not in stop}

    def _coverage_snapshot(
        self, coverage: CoverageMap | None, conclusion: ConclusionState | None
    ) -> dict:
        snapshot: dict = {}
        if coverage:
            snapshot.update(
                {
                    "coverage_score": coverage.overall_coverage_score,
                    "high_tier_evidence_count": coverage.high_tier_evidence_count,
                    "contradiction_count": coverage.contradiction_count,
                }
            )
        if conclusion:
            snapshot.update(
                {
                    "current_stance": conclusion.current_stance,
                    "confidence_band": conclusion.confidence_band,
                }
            )
        return snapshot

    def _gap_flags(self, coverage: CoverageMap | None) -> list[str]:
        if not coverage:
            return ["no_coverage_map"]
        flags: list[str] = []
        if coverage.high_tier_evidence_count == 0:
            flags.append("missing_high_tier_evidence")
        if coverage.contradiction_count == 0:
            flags.append("missing_contradiction_evidence")
        return flags
