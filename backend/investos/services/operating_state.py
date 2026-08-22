from __future__ import annotations

import re
from uuid import UUID

from sqlalchemy import desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from investos.config import settings
from investos.models.catalog import (
    SourceProfile,
    SourceTrustProfile,
    SourceValueProfile,
)
from investos.models.conclusion import ConclusionState
from investos.models.coverage import UnresolvedQuestion
from investos.models.entity import Entity, Security
from investos.models.evidence import RawEvidence
from investos.models.graph import Edge
from investos.models.knowledge import Claim, Event, Fact
from investos.models.lesson import Lesson
from investos.models.portfolio import CashLedgerEntry, Position
from investos.models.profile import Profile
from investos.models.shadow import ExperimentResult, ShadowExperiment
from investos.models.source import Source
from investos.models.theme import Theme
from investos.services.canonical_state import CanonicalStateService
from investos.services.portfolio_peers import PortfolioPeerContextService
from investos.services.review import ReviewService
from investos.services.risk import RiskService
from investos.services.runtime_settings import RuntimeSettingsStore
from investos.services.shadow import ShadowService


class OperatingStateService:
    def __init__(self, session: AsyncSession):
        self.session = session
        self.canonical = CanonicalStateService(session)

    async def trusted_sources_payload(
        self, *, exclude_names: set[str] | None = None
    ) -> dict[str, object]:
        excludes = exclude_names or set()
        sources = (
            (
                await self.session.execute(
                    select(Source)
                    .where(Source.is_trusted.is_(True))
                    .order_by(desc(Source.updated_at), Source.name.asc())
                )
            )
            .scalars()
            .all()
        )
        filtered = [source for source in sources if source.name not in excludes]
        recent_rows = (
            (
                await self.session.execute(
                    select(RawEvidence)
                    .where(
                        RawEvidence.source_id.in_(
                            [source.id for source in filtered] or [UUID(int=0)]
                        )
                    )
                    .order_by(desc(RawEvidence.created_at))
                )
            )
            .scalars()
            .all()
        )
        recent_by_source: dict[UUID, list[dict[str, object]]] = {}
        for evidence in recent_rows:
            bucket = recent_by_source.setdefault(evidence.source_id, [])
            if len(bucket) >= 3:
                continue
            bucket.append(
                {
                    "title": evidence.title,
                    "url": evidence.url,
                    "created_at": evidence.created_at.isoformat(),
                    "source_item_type": evidence.source_item_type,
                    "is_processed": evidence.is_processed,
                }
            )
        return {
            "count": len(filtered),
            "sources": [
                {
                    "name": source.name,
                    "source_type": source.source_type,
                    "url": source.url,
                    "description": source.description,
                    "updated_at": source.updated_at.isoformat(),
                    "trust_profile": await self._source_trust_summary(source.id),
                    "recent_items": recent_by_source.get(source.id, []),
                }
                for source in filtered[:12]
            ],
        }

    async def _source_trust_summary(self, source_id: UUID) -> dict[str, object] | None:
        profile = (
            await self.session.execute(
                select(SourceProfile).where(SourceProfile.source_id == source_id)
            )
        ).scalar_one_or_none()
        trust = (
            await self.session.execute(
                select(SourceTrustProfile).where(
                    SourceTrustProfile.source_id == source_id
                )
            )
        ).scalar_one_or_none()
        value = (
            await self.session.execute(
                select(SourceValueProfile).where(
                    SourceValueProfile.source_id == source_id
                )
            )
        ).scalar_one_or_none()
        if profile is None and trust is None and value is None:
            return None
        return {
            "specialization_domains": (
                None if profile is None else (profile.specialization_domains or [])
            ),
            "known_weaknesses": (
                None if profile is None else (profile.known_weaknesses or [])
            ),
            "trust_trajectory": (None if trust is None else trust.trust_trajectory)
            or (None if profile is None else profile.trust_trajectory),
            "factual_reliability": None if trust is None else trust.factual_reliability,
            "noise_ratio": None if trust is None else trust.noise_ratio,
            "idea_generation_value": (
                None if value is None else value.idea_generation_value
            ),
            "timing_value": None if value is None else value.timing_value,
            "portfolio_relevance_value": (
                None if value is None else value.portfolio_relevance_value
            ),
            "specificity": None if value is None else value.specificity,
            "originality": None if value is None else value.originality,
        }

    async def benchmark_payload(self) -> dict[str, object]:
        runtime = RuntimeSettingsStore.load()
        return {
            "default_benchmark_ticker": runtime.portfolio.default_benchmark_ticker,
        }

    async def portfolio_state_payload(self) -> dict[str, object]:
        position_rows = (
            await self.session.execute(
                select(Position, Security, Entity)
                .join(Security, Position.security_id == Security.id)
                .join(Entity, Security.entity_id == Entity.id)
                .where(Position.list_type.in_(["holding", "watchlist", "considering"]))
                .order_by(desc(Position.market_value), Position.added_at.desc())
            )
        ).all()
        positions = [position for position, _, _ in position_rows]
        holdings = [
            position for position in positions if position.list_type == "holding"
        ]
        watchlist = [
            position for position in positions if position.list_type == "watchlist"
        ]
        considering = [
            position for position in positions if position.list_type == "considering"
        ]
        total_market_value = sum(
            float(position.market_value or 0.0) for position in holdings
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
        peer_exposures = (
            await PortfolioPeerContextService(self.session).peer_exposures(limit=12)
            if len(positions) > 1
            else []
        )
        performance_attribution = await RiskService(
            self.session
        ).get_cached_performance_attribution(window_days=21)

        return {
            "holdings_count": len(holdings),
            "watchlist_count": len(watchlist),
            "considering_count": len(considering),
            "total_market_value": round(total_market_value, 2),
            "remaining_buying_power": round(buying_power, 2),
            "pct_capital_deployed": pct_deployed,
            "top_holdings": [
                {
                    "ticker": security.ticker,
                    "name": entity.name,
                    "quantity": float(position.quantity or 0.0),
                    "market_value": float(position.market_value or 0.0),
                    "current_price": float(position.current_price or 0.0),
                }
                for position, security, entity in position_rows[:5]
                if position.list_type == "holding"
            ],
            "peer_exposures": peer_exposures,
            "performance_attribution": (
                performance_attribution.model_dump(mode="json")
                if performance_attribution is not None
                else None
            ),
        }

    async def entity_overview_payload(
        self,
        *,
        subject_id: UUID,
        subject_name: str,
    ) -> dict[str, object]:
        security_rows = (
            await self.session.execute(
                select(Security, Entity)
                .join(Entity, Security.entity_id == Entity.id)
                .where(Security.entity_id == subject_id)
                .order_by(Security.ticker.asc())
            )
        ).all()
        position_rows = (
            await self.session.execute(
                select(Position, Security)
                .join(Security, Position.security_id == Security.id)
                .where(Security.entity_id == subject_id)
                .order_by(desc(Position.market_value), Position.added_at.desc())
            )
        ).all()
        profile = (
            await self.session.execute(
                select(Profile).where(
                    Profile.subject_type == "entity", Profile.subject_id == subject_id
                )
            )
        ).scalar_one_or_none()
        conclusion = await self.canonical.get_conclusion_state(
            subject_type="entity",
            subject_id=subject_id,
        )
        return {
            "subject_name": subject_name,
            "securities": [
                {
                    "ticker": security.ticker,
                    "exchange": security.exchange,
                    "asset_class": security.asset_class,
                    "instrument_type": security.instrument_type,
                    "sector": entity.sector,
                    "industry": entity.industry,
                    "country": entity.country,
                    "description": entity.description,
                }
                for security, entity in security_rows[:4]
            ],
            "positions": [
                {
                    "ticker": security.ticker,
                    "list_type": position.list_type,
                    "direction": position.direction,
                    "quantity": float(position.quantity or 0.0),
                    "avg_cost_basis": float(position.avg_cost_basis or 0.0),
                    "current_price": float(position.current_price or 0.0),
                    "market_value": float(position.market_value or 0.0),
                    "unrealized_pnl": float(position.unrealized_pnl or 0.0),
                    "conviction": position.conviction,
                }
                for position, security in position_rows[:6]
            ],
            "profile": {
                "executive_summary": profile.executive_summary if profile else None,
                "bull_case": profile.bull_case if profile else None,
                "bear_case": profile.bear_case if profile else None,
                "active_contradictions": (
                    profile.active_contradictions if profile else []
                ),
            },
            "accepted_state": {
                "current_stance": conclusion.current_stance if conclusion else None,
                "confidence_band": conclusion.confidence_band if conclusion else None,
                "current_thesis_summary": (
                    conclusion.current_thesis_summary if conclusion else None
                ),
                "what_would_falsify": (
                    conclusion.what_would_falsify if conclusion else []
                ),
                "what_would_strengthen": (
                    conclusion.what_would_strengthen if conclusion else []
                ),
            },
        }

    async def knowledge_status_payload(
        self,
        *,
        subject_id: UUID,
        subject_type: str,
        subject_name: str,
        query: str,
        limit: int = 10,
    ) -> dict[str, object]:
        direct_edges = (
            (
                await self.session.execute(
                    select(Edge)
                    .where(
                        Edge.target_type == subject_type,
                        Edge.target_id == subject_id,
                        Edge.source_type.in_(["fact", "claim", "event"]),
                    )
                    .order_by(desc(Edge.created_at))
                )
            )
            .scalars()
            .all()
        )
        direct_ids = {
            "fact": [
                edge.source_id for edge in direct_edges if edge.source_type == "fact"
            ],
            "claim": [
                edge.source_id for edge in direct_edges if edge.source_type == "claim"
            ],
            "event": [
                edge.source_id for edge in direct_edges if edge.source_type == "event"
            ],
        }
        direct_facts = await self._knowledge_nodes_by_ids(Fact, direct_ids["fact"])
        direct_claims = await self._knowledge_nodes_by_ids(Claim, direct_ids["claim"])
        direct_events = await self._knowledge_nodes_by_ids(Event, direct_ids["event"])
        direct_nodes = [
            *[self._knowledge_node_payload("fact", item) for item in direct_facts],
            *[self._knowledge_node_payload("claim", item) for item in direct_claims],
            *[self._knowledge_node_payload("event", item) for item in direct_events],
        ]
        active_direct_nodes = [
            item for item in direct_nodes if not item["is_deprecated"]
        ]
        deprecated_direct_nodes = [
            item for item in direct_nodes if item["is_deprecated"]
        ]
        terms = self._knowledge_query_terms(query, subject_name=subject_name)
        matching_nodes = await self._matching_knowledge_nodes(terms=terms, limit=limit)
        matching_active_nodes = [
            item for item in matching_nodes if not item["is_deprecated"]
        ]
        matching_deprecated_nodes = [
            item for item in matching_nodes if item["is_deprecated"]
        ]
        direct_term_matches = (
            self._filter_nodes_by_terms(active_direct_nodes, terms)
            if terms
            else active_direct_nodes
        )
        matched_terms = self._matched_terms(
            [*direct_term_matches, *matching_active_nodes], terms
        )
        latest_direct_nodes = sorted(
            active_direct_nodes,
            key=lambda item: str(
                item.get("updated_at") or item.get("created_at") or ""
            ),
            reverse=True,
        )[:limit]
        return {
            "subject_name": subject_name,
            "subject_type": subject_type,
            "query_terms": terms,
            "matched_terms": matched_terms,
            "missing_terms": [term for term in terms if term not in matched_terms],
            "direct_active_count": len(active_direct_nodes),
            "direct_deprecated_count": len(deprecated_direct_nodes),
            "direct_counts": self._knowledge_counts(direct_nodes),
            "direct_term_matches": direct_term_matches[:limit],
            "latest_direct_nodes": latest_direct_nodes,
            "matching_active_nodes": matching_active_nodes[:limit],
            "matching_deprecated_nodes": matching_deprecated_nodes[:limit],
            "searched_at": self._now_iso(),
        }

    async def _knowledge_nodes_by_ids(self, model, ids: list[UUID]) -> list[object]:
        if not ids:
            return []
        return (
            (
                await self.session.execute(
                    select(model)
                    .where(model.id.in_(ids))
                    .order_by(desc(model.updated_at))
                )
            )
            .scalars()
            .all()
        )

    async def _matching_knowledge_nodes(
        self, *, terms: list[str], limit: int
    ) -> list[dict[str, object]]:
        if not terms:
            return []
        fact_filter = or_(*[Fact.statement.ilike(f"%{term}%") for term in terms])
        claim_filter = or_(*[Claim.statement.ilike(f"%{term}%") for term in terms])
        event_filter = or_(
            *[Event.title.ilike(f"%{term}%") for term in terms],
            *[Event.description.ilike(f"%{term}%") for term in terms],
        )
        facts = (
            (
                await self.session.execute(
                    select(Fact)
                    .where(fact_filter)
                    .order_by(desc(Fact.updated_at))
                    .limit(limit)
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
                    .order_by(desc(Claim.updated_at))
                    .limit(limit)
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
                    .order_by(desc(Event.updated_at))
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        nodes = [
            *[self._knowledge_node_payload("fact", item) for item in facts],
            *[self._knowledge_node_payload("claim", item) for item in claims],
            *[self._knowledge_node_payload("event", item) for item in events],
        ]
        return sorted(
            nodes,
            key=lambda item: str(
                item.get("updated_at") or item.get("created_at") or ""
            ),
            reverse=True,
        )[: limit * 3]

    @staticmethod
    def _knowledge_node_payload(node_type: str, node) -> dict[str, object]:
        text = getattr(node, "statement", None) or getattr(node, "title", None) or ""
        description = getattr(node, "description", None)
        if node_type == "event" and description:
            text = f"{text}: {description}"
        return {
            "id": str(node.id),
            "type": node_type,
            "text": text,
            "confidence": (
                float(getattr(node, "confidence", 0.0) or 0.0)
                if node_type in {"fact", "claim"}
                else None
            ),
            "tier": getattr(node, "tier", None),
            "importance": getattr(node, "importance", None),
            "directness": getattr(node, "directness", None),
            "novelty": getattr(node, "novelty", None),
            "contradiction_role": getattr(node, "contradiction_role", None),
            "created_at": (
                node.created_at.isoformat()
                if getattr(node, "created_at", None)
                else None
            ),
            "updated_at": (
                node.updated_at.isoformat()
                if getattr(node, "updated_at", None)
                else None
            ),
            "is_deprecated": bool(getattr(node, "is_deprecated", False)),
            "deprecated_reason": getattr(node, "deprecated_reason", None),
        }

    @staticmethod
    def _knowledge_query_terms(query: str, *, subject_name: str = "") -> list[str]:
        stopwords = {
            "a",
            "about",
            "actually",
            "and",
            "are",
            "as",
            "at",
            "be",
            "been",
            "did",
            "do",
            "does",
            "for",
            "from",
            "get",
            "got",
            "graph",
            "have",
            "i",
            "if",
            "in",
            "info",
            "information",
            "into",
            "is",
            "it",
            "its",
            "knowledge",
            "me",
            "my",
            "new",
            "newfound",
            "node",
            "nodes",
            "of",
            "on",
            "or",
            "research",
            "save",
            "saved",
            "store",
            "stored",
            "that",
            "the",
            "this",
            "to",
            "was",
            "were",
            "what",
            "whether",
            "with",
            "you",
            "your",
        }
        raw = f"{query or ''} {subject_name or ''}"
        terms: list[str] = []
        for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9+./-]*", raw.lower()):
            if token in stopwords or len(token) <= 2:
                continue
            terms.append(token)
        seen: set[str] = set()
        deduped: list[str] = []
        for term in terms:
            if term in seen:
                continue
            seen.add(term)
            deduped.append(term)
        return deduped[:8]

    @staticmethod
    def _filter_nodes_by_terms(
        nodes: list[dict[str, object]], terms: list[str]
    ) -> list[dict[str, object]]:
        if not terms:
            return nodes
        matches = []
        for node in nodes:
            text = str(node.get("text") or "").lower()
            if any(term in text for term in terms):
                matches.append(node)
        return matches

    @staticmethod
    def _matched_terms(nodes: list[dict[str, object]], terms: list[str]) -> list[str]:
        matched: list[str] = []
        for term in terms:
            for node in nodes:
                if term in str(node.get("text") or "").lower():
                    matched.append(term)
                    break
        return matched

    @staticmethod
    def _knowledge_counts(nodes: list[dict[str, object]]) -> dict[str, dict[str, int]]:
        counts = {
            "fact": {"active": 0, "deprecated": 0},
            "claim": {"active": 0, "deprecated": 0},
            "event": {"active": 0, "deprecated": 0},
        }
        for node in nodes:
            node_type = str(node.get("type") or "")
            if node_type not in counts:
                continue
            bucket = "deprecated" if node.get("is_deprecated") else "active"
            counts[node_type][bucket] += 1
        return counts

    @staticmethod
    def _now_iso() -> str:
        from investos.models.base import utcnow

        return utcnow().isoformat()

    async def research_status_payload(
        self, *, session_id: UUID | None = None
    ) -> dict[str, object]:
        unresolved = (
            (
                await self.session.execute(
                    select(RawEvidence.id).where(RawEvidence.is_processed.is_(False))
                )
            )
            .scalars()
            .all()
        )
        open_items = (
            (
                await self.session.execute(
                    select(UnresolvedQuestion)
                    .where(UnresolvedQuestion.status == "open")
                    .order_by(
                        UnresolvedQuestion.urgency.desc(),
                        UnresolvedQuestion.created_at.asc(),
                    )
                    .limit(3)
                )
            )
            .scalars()
            .all()
        )
        open_question_count = int(
            (
                await self.session.execute(
                    select(func.count(UnresolvedQuestion.id)).where(
                        UnresolvedQuestion.status == "open"
                    )
                )
            ).scalar_one()
            or 0
        )
        latest_research = (
            (
                await self.session.execute(
                    select(RawEvidence)
                    .where(RawEvidence.source_item_type == "web_research")
                    .order_by(desc(RawEvidence.created_at))
                    .limit(12)
                )
            )
            .scalars()
            .all()
        )
        latest_item_payload: dict[str, object] | None = None
        for evidence in latest_research:
            metadata = evidence.metadata_json or {}
            if session_id is not None and metadata.get("session_id") not in {
                None,
                str(session_id),
            }:
                continue
            subject_name = None
            raw_subject_id = metadata.get("subject_id")
            raw_subject_type = metadata.get("subject_type")
            if raw_subject_id and raw_subject_type:
                try:
                    subject_name = await self.subject_name(
                        UUID(str(raw_subject_id)), str(raw_subject_type)
                    )
                except Exception as exc:
                    import logging

                    logging.getLogger(__name__).exception("Masked failure caught")
                    subject_name = None
            latest_item_payload = {
                "id": str(evidence.id),
                "title": evidence.title,
                "created_at": evidence.created_at.isoformat(),
                "is_processed": evidence.is_processed,
                "url": evidence.url,
                "subject_name": subject_name,
                "requested_via": metadata.get("requested_via"),
            }
            break
        return {
            "automation_enabled": settings.AUTOMATION_ENABLED,
            "research_provider_configured": bool(
                RuntimeSettingsStore.load().research.api_key
            ),
            "pending_evidence_count": len(unresolved),
            "open_question_count": open_question_count,
            "open_questions": [
                {
                    "question_text": item.question_text,
                    "urgency": item.urgency,
                }
                for item in open_items
            ],
            "latest_item": latest_item_payload,
        }

    async def shadow_status_payload(self) -> dict[str, object]:
        experiments = (
            (
                await self.session.execute(
                    select(ShadowExperiment)
                    .order_by(desc(ShadowExperiment.created_at))
                    .limit(8)
                )
            )
            .scalars()
            .all()
        )
        result_rows = (
            (await self.session.execute(select(ExperimentResult))).scalars().all()
        )
        result_by_experiment_id = {
            result.experiment_id: result for result in result_rows
        }
        shadow_lessons = (
            (
                await self.session.execute(
                    select(Lesson)
                    .where(Lesson.originating_experiment_result_id.is_not(None))
                    .order_by(desc(Lesson.created_at))
                    .limit(3)
                )
            )
            .scalars()
            .all()
        )
        normalized_statuses = [
            ShadowService.normalize_run_status(experiment.run_status)
            for experiment in experiments
        ]
        queued_count = sum(1 for status in normalized_statuses if status == "queued")
        running_count = sum(1 for status in normalized_statuses if status == "running")
        completed_count = sum(
            1 for status in normalized_statuses if status == "completed"
        )
        failed_count = sum(1 for status in normalized_statuses if status == "failed")
        recent_experiments: list[dict[str, object]] = []
        for experiment in experiments:
            context = (experiment.initial_portfolio_state_json or {}).get(
                "experiment_context"
            ) or {}
            result = result_by_experiment_id.get(experiment.id)
            recent_experiments.append(
                {
                    "name": experiment.name,
                    "run_status": ShadowService.normalize_run_status(
                        experiment.run_status
                    ),
                    "trigger_type": context.get("trigger_type"),
                    "trigger_reason": context.get("trigger_reason"),
                    "initiated_by": context.get("initiated_by"),
                    "horizon_label": context.get("horizon_label"),
                    "created_at": experiment.created_at.isoformat(),
                    "completed_at": (
                        experiment.completed_at.isoformat()
                        if experiment.completed_at
                        else None
                    ),
                    "alpha": float(result.alpha) if result is not None else None,
                    "shadow_return": (
                        float(result.shadow_return) if result is not None else None
                    ),
                    "actual_return": (
                        float(result.actual_return) if result is not None else None
                    ),
                }
            )
        return {
            "count": len(experiments),
            "queued_count": queued_count,
            "running_count": running_count,
            "completed_count": completed_count,
            "failed_count": failed_count,
            "recent_experiments": recent_experiments,
            "recent_shadow_lessons": [
                {
                    "title": lesson.title,
                    "summary": lesson.summary,
                    "maturity_status": lesson.maturity_status,
                    "confidence_score": lesson.confidence_score,
                    "supporting_observations": lesson.supporting_observations,
                    "contradicting_observations": lesson.contradicting_observations,
                    "neutral_observations": lesson.neutral_observations,
                    "created_at": lesson.created_at.isoformat(),
                }
                for lesson in shadow_lessons
            ],
        }

    async def lessons_payload(self) -> dict[str, object]:
        lessons = (
            (
                await self.session.execute(
                    select(Lesson).order_by(desc(Lesson.created_at)).limit(8)
                )
            )
            .scalars()
            .all()
        )
        shadow_lesson_count = sum(
            1 for lesson in lessons if lesson.lesson_type == "shadow_policy_outcome"
        )
        return {
            "count": len(lessons),
            "shadow_lesson_count": shadow_lesson_count,
            "recent_lessons": [
                {
                    "title": lesson.title,
                    "summary": lesson.summary,
                    "lesson_type": lesson.lesson_type,
                    "maturity_status": lesson.maturity_status,
                    "confidence_score": lesson.confidence_score,
                    "supporting_observations": lesson.supporting_observations,
                    "contradicting_observations": lesson.contradicting_observations,
                    "neutral_observations": lesson.neutral_observations,
                    "created_at": lesson.created_at.isoformat(),
                    "from_shadow": lesson.lesson_type == "shadow_policy_outcome",
                }
                for lesson in lessons
            ],
        }

    async def review_queue_payload(self) -> dict[str, object]:
        queue = await ReviewService(self.session).list_queue()
        return {
            "count": len(queue),
            "top_items": [
                {
                    "item_type": item.item_type,
                    "item_label": item.item_label,
                    "priority_score": float(item.priority_score),
                    "trigger_reason": item.trigger_reason,
                    "status": item.status,
                }
                for item in queue[:6]
            ],
        }

    async def portfolio_monitor_payload(self) -> dict[str, object]:
        holding_rows = (
            await self.session.execute(
                select(Position, Security, Entity)
                .join(Security, Position.security_id == Security.id)
                .join(Entity, Security.entity_id == Entity.id)
                .where(Position.list_type == "holding", Position.quantity > 0)
                .order_by(desc(Position.market_value))
            )
        ).all()
        holding_entity_ids = {entity.id for _, _, entity in holding_rows}
        holding_position_ids = {position.id for position, _, _ in holding_rows}
        queue = await ReviewService(self.session).list_queue()
        relevant_queue = [
            item
            for item in queue
            if (
                (item.item_type == "position" and item.item_id in holding_position_ids)
                or (item.item_type == "entity" and item.item_id in holding_entity_ids)
                or item.item_type == "shadow_experiment"
            )
        ]
        research_rows = (
            (
                await self.session.execute(
                    select(RawEvidence)
                    .where(RawEvidence.source_item_type == "web_research")
                    .order_by(desc(RawEvidence.created_at))
                    .limit(25)
                )
            )
            .scalars()
            .all()
        )
        recent_research: list[dict[str, object]] = []
        for evidence in research_rows:
            metadata = evidence.metadata_json or {}
            raw_subject_id = metadata.get("subject_id")
            raw_subject_type = metadata.get("subject_type")
            include = False
            subject_name = "Portfolio"
            if raw_subject_id and raw_subject_type:
                try:
                    parsed_subject_id = UUID(str(raw_subject_id))
                    parsed_subject_type = str(raw_subject_type)
                    subject_name = await self.subject_name(
                        parsed_subject_id, parsed_subject_type
                    )
                    include = (
                        parsed_subject_type == "entity"
                        and parsed_subject_id in holding_entity_ids
                    ) or (
                        parsed_subject_type == "position"
                        and parsed_subject_id in holding_position_ids
                    )
                except Exception as exc:
                    import logging

                    logging.getLogger(__name__).exception("Masked failure caught")
                    include = False
            elif metadata.get("trigger") in {"research_loop", "chat_research_start"}:
                include = True
            if not include:
                continue
            recent_research.append(
                {
                    "id": str(evidence.id),
                    "title": evidence.title,
                    "subject_name": subject_name,
                    "created_at": evidence.created_at.isoformat(),
                    "is_processed": evidence.is_processed,
                }
            )
            if len(recent_research) >= 5:
                break
        return {
            "monitored_holding_count": len(holding_rows),
            "priority_review_count": len(relevant_queue),
            "priority_review_items": [
                {
                    "item_label": item.item_label,
                    "item_type": item.item_type,
                    "priority_score": float(item.priority_score),
                    "trigger_reason": item.trigger_reason,
                }
                for item in relevant_queue[:5]
            ],
            "recent_research_items": recent_research,
        }

    async def subject_name(self, subject_id: UUID, subject_type: str) -> str:
        if subject_type == "portfolio":
            return "Portfolio"
        if subject_type == "theme":
            theme = (
                await self.session.execute(select(Theme).where(Theme.id == subject_id))
            ).scalar_one_or_none()
            return theme.name if theme is not None else str(subject_id)
        if subject_type == "position":
            position = (
                await self.session.execute(
                    select(Position).where(Position.id == subject_id)
                )
            ).scalar_one_or_none()
            if position is None:
                return str(subject_id)
            security = (
                await self.session.execute(
                    select(Security).where(Security.id == position.security_id)
                )
            ).scalar_one_or_none()
            if security is None:
                return str(subject_id)
            entity = (
                await self.session.execute(
                    select(Entity).where(Entity.id == security.entity_id)
                )
            ).scalar_one_or_none()
            return (
                security.ticker
                if entity is None
                else f"{security.ticker} · {entity.name}"
            )
        entity = (
            await self.session.execute(select(Entity).where(Entity.id == subject_id))
        ).scalar_one_or_none()
        if entity is not None:
            security = (
                (
                    await self.session.execute(
                        select(Security)
                        .where(Security.entity_id == entity.id)
                        .order_by(Security.ticker.asc())
                    )
                )
                .scalars()
                .first()
            )
            if security is None:
                return entity.name
            if not entity.name or entity.name.lower() == security.ticker.lower():
                return security.ticker
            return f"{security.ticker} · {entity.name}"
        return str(subject_id)

    async def discoveries_payload(self) -> dict[str, object]:
        """Returns all pending autonomous discoveries for bulk review."""
        # Find autonomous profiles
        profile_rows = (
            await self.session.execute(
                select(Profile, Entity)
                .join(Entity, Profile.subject_id == Entity.id)
                .where(
                    Profile.is_autonomous.is_(True), Profile.review_status == "pending"
                )
                .order_by(Profile.created_at.desc())
            )
        ).all()

        # Find autonomous positions
        position_rows = (
            await self.session.execute(
                select(Position, Security, Entity)
                .join(Security, Position.security_id == Security.id)
                .join(Entity, Security.entity_id == Entity.id)
                .where(
                    Position.is_autonomous.is_(True),
                    Position.review_status == "pending",
                )
                .order_by(Position.added_at.desc())
            )
        ).all()

        discoveries = []
        for profile, entity in profile_rows:
            discoveries.append(
                {
                    "id": str(profile.id),
                    "subject_type": "entity",
                    "subject_id": str(profile.subject_id),
                    "name": entity.name,
                    "reason": profile.review_reason,
                    "strategist_reasoning": profile.strategist_reasoning,
                    "source_rationale": profile.source_rationale,
                    "created_at": profile.created_at.isoformat(),
                    "kind": "profile",
                }
            )

        for position, security, entity in position_rows:
            # Avoid duplicate entries if both exist
            if any(d["subject_id"] == str(entity.id) for d in discoveries):
                continue

            discoveries.append(
                {
                    "id": str(position.id),
                    "subject_type": "entity",
                    "subject_id": str(entity.id),
                    "name": f"{security.ticker} · {entity.name}",
                    "reason": position.review_status,
                    "created_at": position.added_at.isoformat(),
                    "kind": "position",
                }
            )

        return {"count": len(discoveries), "items": discoveries}

    async def approve_discovery(self, subject_id: UUID, subject_type: str) -> bool:
        if subject_type != "entity":
            raise ValueError("Only entity discoveries can be approved.")
        profile = (
            await self.session.execute(
                select(Profile).where(
                    Profile.subject_id == subject_id,
                    Profile.subject_type == "entity",
                )
            )
        ).scalar_one_or_none()
        position = (
            await self.session.execute(
                select(Position)
                .join(Security, Position.security_id == Security.id)
                .where(Security.entity_id == subject_id)
            )
        ).scalar_one_or_none()
        if profile:
            profile.is_autonomous = False
            profile.review_status = "approved"
        if position:
            position.is_autonomous = False
            position.review_status = "approved"
        changed = bool(profile or position)
        if changed:
            await self.session.commit()
        return changed

    async def dismiss_discovery(
        self, subject_id: UUID, subject_type: str, reason: str | None = None
    ) -> bool:
        """Dismisses a discovery and captures the reason for learning."""
        if subject_type != "entity":
            raise ValueError("Only entity discoveries can be dismissed.")
        profile = (
            await self.session.execute(
                select(Profile).where(
                    Profile.subject_id == subject_id,
                    Profile.subject_type == "entity",
                )
            )
        ).scalar_one_or_none()
        position = (
            await self.session.execute(
                select(Position)
                .join(Security, Position.security_id == Security.id)
                .where(Security.entity_id == subject_id)
            )
        ).scalar_one_or_none()
        if profile:
            profile.review_status = "dismissed"
            if reason and len(reason.strip()) > 10:
                self.session.add(
                    Lesson(
                        title=f"Constraint: Rejection of {subject_id}",
                        summary=(
                            "User dismissed an autonomous discovery. "
                            f"Feedback: {reason.strip()}"
                        ),
                        lesson_type="user_preference",
                        metadata_json={
                            "subject_id": str(subject_id),
                            "subject_type": subject_type,
                            "feedback_origin": "discovery_dismissal",
                        },
                    )
                )
        if position:
            position.review_status = "dismissed"
            if position.list_type in {"considering", "watchlist"}:
                await self.session.delete(position)
        changed = bool(profile or position)
        if changed:
            await self.session.commit()
        return changed
