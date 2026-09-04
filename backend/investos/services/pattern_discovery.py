from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import delete, desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from investos.config import settings
from investos.core.llm import call_llm_json
from investos.models.catalog import HistoricalEpisode
from investos.models.coverage import CoverageMap, UnresolvedQuestion
from investos.models.entity import Entity, Security
from investos.models.evidence import RawEvidence, SourceItem
from investos.models.graph import Edge
from investos.models.market_setup import MarketSetupSignal
from investos.models.portfolio import Position
from investos.models.quant import RegimeState
from investos.models.source import Source
from investos.services.canonical_state import CanonicalStateService
from investos.services.corroboration import source_lineage_key
from investos.services.graph_edge_state import GraphEdgeStateService
from investos.services.knowledge_audit import KnowledgeAuditService
from investos.services.market_setup import MarketSetupSignalService
from investos.services.portfolio_peers import PortfolioPeerContextService

PATTERN_DISCOVERY_SCHEMA = {
    "type": "object",
    "properties": {
        "actionable": {"type": "boolean"},
        "reasoning_summary": {"type": "string"},
        "hypothesis": {
            "type": "object",
            "properties": {
                "label": {"type": "string"},
                "pattern_type": {"type": "string"},
                "observation": {"type": "string"},
                "proposed_mechanism": {"type": "string"},
                "affected_tickers": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "direction": {"type": "string"},
                "evidence_refs": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "falsifier": {"type": "string"},
                "next_test": {"type": "string"},
                "why_now": {"type": "string"},
                "historical_episode_id": {"type": "string"},
                "confidence": {"type": "number"},
            },
            "required": [
                "label",
                "pattern_type",
                "observation",
                "proposed_mechanism",
                "affected_tickers",
                "direction",
                "evidence_refs",
                "falsifier",
                "next_test",
                "why_now",
                "historical_episode_id",
                "confidence",
            ],
            "additionalProperties": False,
        },
    },
    "required": ["actionable", "reasoning_summary", "hypothesis"],
    "additionalProperties": False,
}


class PatternDiscoveryService:
    """Turn corroborated recurring observations into testable hypotheses.

    The model owns open-ended pattern generation. Deterministic code owns the
    evidence boundary, portfolio scope, deduplication, persistence, and the
    distinction between a provisional hypothesis and accepted knowledge.
    """

    SIGNAL_FAMILY = "pattern_hypothesis"

    def __init__(self, session: AsyncSession):
        self.session = session
        self.edge_state = GraphEdgeStateService(session)

    async def discover(self, *, apply: bool = True) -> dict[str, Any]:
        now = datetime.now(UTC)
        tracked = await self._tracked_universe()
        if len(tracked) < 2:
            return {
                "status": "idle",
                "detail": "insufficient_tracked_universe",
                "created": 0,
            }

        evidence = await self._recent_evidence_registry(
            tracked_tickers=set(tracked), now=now
        )
        independent_lineages = {
            item["lineage_key"] for item in evidence.values() if item["lineage_key"]
        }
        minimum_sources = max(2, settings.CORROBORATION_MIN_INDEPENDENT_SOURCES)
        if len(independent_lineages) < minimum_sources:
            return {
                "status": "idle",
                "detail": "insufficient_independent_pattern_evidence",
                "created": 0,
                "evidence_count": len(evidence),
                "independent_lineages": len(independent_lineages),
            }

        episodes = await self._historical_episodes()
        result = await call_llm_json(
            system_prompt=self._system_prompt(),
            user_prompt=json.dumps(
                {
                    "instruction": (
                        "Find at most one material, testable pattern that is not already obvious from a single item. "
                        "Return actionable=false when the packet does not support one."
                    ),
                    "tracked_universe": [
                        self._tracked_context(item) for item in tracked.values()
                    ],
                    "current_regime": await self._current_regime(),
                    "peer_exposures": await PortfolioPeerContextService(
                        self.session
                    ).peer_exposures(limit=12),
                    "historical_episodes": episodes,
                    "source_dated_signals": list(evidence.values()),
                },
                ensure_ascii=True,
                default=str,
            ),
            schema=PATTERN_DISCOVERY_SCHEMA,
            timeout_seconds=settings.PATTERN_DISCOVERY_LLM_TIMEOUT_SECONDS,
        )

        hypothesis, rejection = self._validate_hypothesis(
            result=result,
            evidence_registry=evidence,
            tracked_tickers=set(tracked),
            historical_episode_ids={item["id"] for item in episodes},
            minimum_confidence=settings.PATTERN_DISCOVERY_MIN_CONFIDENCE,
            minimum_independent_sources=minimum_sources,
        )
        if hypothesis is None:
            return {
                "status": "ok",
                "detail": rejection or "no_actionable_pattern",
                "created": 0,
                "evidence_count": len(evidence),
            }

        hypothesis["pattern_fingerprint"] = self._pattern_fingerprint(hypothesis)
        duplicate = await self._find_duplicate(hypothesis, now=now)
        if duplicate is not None:
            await KnowledgeAuditService(self.session).record_change(
                node_type="market_setup_signal",
                node_id=duplicate.id,
                change_type="duplicate_proposal_rejected",
                reason="A new model proposal repeated an existing provisional pattern.",
                actor="pattern_discovery",
                subject_type=duplicate.subject_type,
                subject_id=duplicate.subject_id,
                metadata={
                    "candidate_label": hypothesis["label"],
                    "candidate_pattern_type": hypothesis["pattern_type"],
                    "candidate_fingerprint": hypothesis["pattern_fingerprint"],
                    "candidate_affected_tickers": hypothesis["affected_tickers"],
                    "candidate_evidence_refs": hypothesis["evidence_refs"],
                },
            )
            await self.session.commit()
            return {
                "status": "ok",
                "detail": "duplicate_pattern_hypothesis",
                "created": 0,
                "pattern_fingerprint": hypothesis["pattern_fingerprint"],
                "existing_signal_id": str(duplicate.id),
            }
        if not apply:
            return {
                "status": "preview",
                "detail": "validated_pattern_preview",
                "created": 0,
                "hypothesis": hypothesis,
            }

        signal, question = await self._persist_hypothesis(
            hypothesis=hypothesis,
            evidence_registry=evidence,
            tracked=tracked,
            now=now,
        )
        return {
            "status": "ok",
            "detail": f"pattern_created={signal.signal_name}",
            "created": 1,
            "signal_id": str(signal.id),
            "question_id": None if question is None else str(question.id),
            "affected_tickers": hypothesis["affected_tickers"],
            "independent_lineages": len(hypothesis["lineage_keys"]),
        }

    @staticmethod
    def _system_prompt() -> str:
        return (
            "You are Prophet's pattern-discovery analyst. Generate hypotheses, not facts or predictions. "
            "Look across source-dated signals, tracked holdings, peer exposure, the current regime, and historical episodes. "
            "Name pattern_type in your own words; there is no fixed taxonomy. "
            "A useful pattern must connect multiple observations through a plausible investment mechanism that could affect "
            "future earnings, expectations, valuation, timing, or portfolio risk. Correlation alone is not causation. "
            "Use only supplied evidence_refs and affected tracked tickers. Cite at least two genuinely independent source "
            "lineages. State what would falsify the mechanism and the next source-backed test. Historical episodes are analogy "
            "seeds only and cannot count as current evidence. Return actionable=false when the evidence is thin, copied, stale, "
            "already explained, or lacks a concrete portfolio transmission route."
        )

    @classmethod
    def _validate_hypothesis(
        cls,
        *,
        result: dict[str, Any],
        evidence_registry: dict[str, dict[str, Any]],
        tracked_tickers: set[str],
        historical_episode_ids: set[str],
        minimum_confidence: float,
        minimum_independent_sources: int,
    ) -> tuple[dict[str, Any] | None, str | None]:
        if not result.get("actionable"):
            return None, "model_found_no_actionable_pattern"
        raw = result.get("hypothesis")
        if not isinstance(raw, dict):
            return None, "missing_pattern_hypothesis"

        confidence = cls._bounded_float(raw.get("confidence"))
        if confidence < minimum_confidence:
            return None, "pattern_confidence_below_threshold"

        evidence_refs = cls._unique_text(raw.get("evidence_refs"))
        if any(ref not in evidence_registry for ref in evidence_refs):
            return None, "pattern_referenced_unknown_evidence"
        lineages = {
            str(evidence_registry[ref].get("lineage_key") or "").strip()
            for ref in evidence_refs
        }
        lineages.discard("")
        if len(lineages) < max(2, minimum_independent_sources):
            return None, "pattern_lacks_independent_corroboration"

        affected_tickers = [
            ticker
            for ticker in cls._unique_text(raw.get("affected_tickers"), upper=True)
            if ticker in tracked_tickers
        ]
        if not affected_tickers:
            return None, "pattern_has_no_tracked_portfolio_route"

        required_text = {
            key: cls._clean_text(raw.get(key), limit=1200)
            for key in (
                "label",
                "pattern_type",
                "observation",
                "proposed_mechanism",
                "falsifier",
                "next_test",
                "why_now",
            )
        }
        if any(not value for value in required_text.values()):
            return None, "pattern_missing_testable_fields"

        historical_episode_id = cls._clean_text(
            raw.get("historical_episode_id"), limit=80
        )
        if historical_episode_id not in historical_episode_ids:
            historical_episode_id = ""

        return (
            {
                **required_text,
                "affected_tickers": affected_tickers,
                "direction": cls._clean_text(raw.get("direction"), limit=120)
                or "uncertain",
                "evidence_refs": evidence_refs,
                "lineage_keys": sorted(lineages),
                "historical_episode_id": historical_episode_id or None,
                "confidence": confidence,
            },
            None,
        )

    async def _tracked_universe(self) -> dict[str, dict[str, Any]]:
        rows = (
            await self.session.execute(
                select(Position, Security, Entity)
                .join(Security, Position.security_id == Security.id)
                .join(Entity, Security.entity_id == Entity.id)
                .where(Position.list_type.in_(["holding", "watchlist", "considering"]))
                .order_by(desc(Position.market_value), Position.added_at.desc())
            )
        ).all()
        tracked: dict[str, dict[str, Any]] = {}
        for position, security, entity in rows:
            ticker = str(security.ticker or "").strip().upper()
            if not ticker or ticker in tracked:
                continue
            tracked[ticker] = {
                "ticker": ticker,
                "entity_id": entity.id,
                "security_id": security.id,
                "entity_name": entity.name,
                "sector": entity.sector,
                "industry": entity.industry,
                "list_type": position.list_type,
                "weight_pct": float(position.weight_pct or 0.0),
                "market_value": float(position.market_value or 0.0),
            }
        return tracked

    async def _recent_evidence_registry(
        self, *, tracked_tickers: set[str], now: datetime
    ) -> dict[str, dict[str, Any]]:
        cutoff = now - timedelta(days=settings.PATTERN_DISCOVERY_LOOKBACK_DAYS)
        rows = list(
            (
                await self.session.execute(
                    select(MarketSetupSignal)
                    .where(
                        MarketSetupSignal.is_deprecated.is_(False),
                        MarketSetupSignal.signal_family != self.SIGNAL_FAMILY,
                        func.coalesce(
                            MarketSetupSignal.public_time,
                            MarketSetupSignal.as_of,
                            MarketSetupSignal.created_at,
                        )
                        >= cutoff,
                        func.coalesce(
                            MarketSetupSignal.public_time,
                            MarketSetupSignal.as_of,
                            MarketSetupSignal.created_at,
                        )
                        <= now,
                        or_(
                            MarketSetupSignal.ticker.in_(sorted(tracked_tickers)),
                            MarketSetupSignal.subject_type == "portfolio",
                            MarketSetupSignal.metadata_json["portfolio_relevant"].astext
                            == "true",
                        ),
                    )
                    .order_by(
                        desc(
                            func.coalesce(
                                MarketSetupSignal.public_time,
                                MarketSetupSignal.as_of,
                                MarketSetupSignal.created_at,
                            )
                        )
                    )
                    .limit(settings.PATTERN_DISCOVERY_MAX_SIGNALS)
                )
            )
            .scalars()
            .all()
        )
        source_item_ids = {row.source_item_id for row in rows if row.source_item_id}
        source_items = {
            item.id: item
            for item in (
                (
                    await self.session.execute(
                        select(SourceItem).where(SourceItem.id.in_(source_item_ids))
                    )
                )
                .scalars()
                .all()
            )
        }
        raw_ids = {row.raw_evidence_id for row in rows if row.raw_evidence_id}
        raw_ids.update(
            item.raw_evidence_id
            for item in source_items.values()
            if item.raw_evidence_id
        )
        raw_evidence = {
            item.id: item
            for item in (
                (
                    await self.session.execute(
                        select(RawEvidence).where(RawEvidence.id.in_(raw_ids))
                    )
                )
                .scalars()
                .all()
            )
        }
        source_ids = {item.source_id for item in raw_evidence.values()}
        sources = {
            item.id: item
            for item in (
                (
                    await self.session.execute(
                        select(Source).where(Source.id.in_(source_ids))
                    )
                )
                .scalars()
                .all()
            )
        }

        registry: dict[str, dict[str, Any]] = {}
        for signal in rows:
            source_item = source_items.get(signal.source_item_id)
            raw_id = signal.raw_evidence_id or (
                None if source_item is None else source_item.raw_evidence_id
            )
            evidence = raw_evidence.get(raw_id)
            source = None if evidence is None else sources.get(evidence.source_id)
            if evidence is None or source is None:
                continue
            ref = f"setup:{signal.id}"
            registry[ref] = {
                "ref": ref,
                "signal_id": str(signal.id),
                "ticker": signal.ticker,
                "signal_name": signal.signal_name,
                "signal_family": signal.signal_family,
                "setup_context": self._clean_text(signal.setup_context, limit=700),
                "actual_context": self._clean_text(signal.actual_context, limit=500),
                "price_reaction": self._clean_text(signal.price_reaction, limit=350),
                "investment_relevance": self._clean_text(
                    signal.investment_relevance, limit=500
                ),
                "direction": signal.direction,
                "confidence": float(signal.confidence or 0.0),
                "public_time": (
                    signal.public_time or signal.as_of or signal.created_at
                ).isoformat(),
                "raw_evidence_id": str(evidence.id),
                "source_item_id": (
                    None if source_item is None else str(source_item.id)
                ),
                "source_name": source.name,
                "source_type": source.source_type,
                "source_url": source.url,
                "lineage_key": source_lineage_key(
                    source_id=source.id,
                    source_url=source.url,
                    evidence_url=evidence.url,
                    metadata=evidence.metadata_json,
                ),
            }
        return registry

    async def _historical_episodes(self) -> list[dict[str, Any]]:
        rows = list(
            (
                await self.session.execute(
                    select(HistoricalEpisode)
                    .order_by(desc(HistoricalEpisode.created_at))
                    .limit(12)
                )
            )
            .scalars()
            .all()
        )
        return [
            {
                "id": str(row.id),
                "name": row.name,
                "episode_type": row.episode_type,
                "period": "-".join(
                    str(value.year)
                    for value in (row.start_time, row.end_time)
                    if value is not None
                ),
                "affected_sectors": row.affected_sectors or [],
                "affected_themes": row.affected_themes or [],
                "dominant_channel": row.dominant_channel,
                "lesson": row.notes,
                "use_policy": "Analogy seed only; current evidence must independently establish the mechanism.",
            }
            for row in rows
        ]

    async def _current_regime(self) -> dict[str, Any] | None:
        row = (
            await self.session.execute(
                select(RegimeState)
                .where(RegimeState.end_date.is_(None))
                .order_by(desc(RegimeState.computed_at))
                .limit(1)
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        return {
            "regime_type": row.regime_type,
            "confidence": float(row.confidence or 0.0),
            "signal_source": row.signal_source,
            "start_date": row.start_date.isoformat(),
            "computed_at": row.computed_at.isoformat(),
        }

    async def _persist_hypothesis(
        self,
        *,
        hypothesis: dict[str, Any],
        evidence_registry: dict[str, dict[str, Any]],
        tracked: dict[str, dict[str, Any]],
        now: datetime,
    ) -> tuple[MarketSetupSignal, UnresolvedQuestion | None]:
        primary = tracked[hypothesis["affected_tickers"][0]]
        cited = [evidence_registry[ref] for ref in hypothesis["evidence_refs"]]
        public_times = [
            datetime.fromisoformat(item["public_time"])
            for item in cited
            if item.get("public_time")
        ]
        first_raw_id = UUID(cited[0]["raw_evidence_id"])
        first_source_item_id = (
            UUID(cited[0]["source_item_id"]) if cited[0].get("source_item_id") else None
        )
        signal = await MarketSetupSignalService(self.session).create_signal(
            signal_name=hypothesis["label"],
            signal_family=self.SIGNAL_FAMILY,
            subject_type="entity",
            subject_id=primary["entity_id"],
            entity_id=primary["entity_id"],
            security_id=primary["security_id"],
            ticker=primary["ticker"],
            raw_evidence_id=first_raw_id,
            source_item_id=first_source_item_id,
            setup_context=(
                f"Observed pattern: {hypothesis['observation']} Proposed mechanism: "
                f"{hypothesis['proposed_mechanism']}"
            ),
            value_text="Provisional pattern hypothesis",
            as_of=max(public_times) if public_times else now,
            event_time=max(public_times) if public_times else now,
            public_time=max(public_times) if public_times else now,
            eligible_action_time=now,
            direction=hypothesis["direction"],
            confidence=hypothesis["confidence"],
            investment_relevance=hypothesis["why_now"],
            next_test=hypothesis["next_test"],
            source_kind="derived_corroborated_pattern",
            commit_transaction=False,
            metadata={
                "origin": "pattern_discovery",
                "hypothesis_status": "provisional",
                "portfolio_relevant": True,
                "pattern_type": hypothesis["pattern_type"],
                "pattern_fingerprint": hypothesis["pattern_fingerprint"],
                "affected_tickers": hypothesis["affected_tickers"],
                "evidence_refs": hypothesis["evidence_refs"],
                "source_lineages": hypothesis["lineage_keys"],
                "proposed_mechanism": hypothesis["proposed_mechanism"],
                "falsifier": hypothesis["falsifier"],
                "historical_episode_id": hypothesis["historical_episode_id"],
                "derivation_policy": (
                    "Model-proposed hypothesis; deterministic multi-lineage and portfolio-route validation applied."
                ),
            },
        )

        for ticker in hypothesis["affected_tickers"]:
            target = tracked[ticker]
            await self.edge_state.ensure_edge(
                source_type="market_setup_signal",
                source_id=signal.id,
                target_type="entity",
                target_id=target["entity_id"],
                relationship_type="pattern_affects",
                confidence=hypothesis["confidence"],
                reasoning=hypothesis["proposed_mechanism"],
                properties={
                    "origin": "pattern_discovery",
                    "hypothesis_status": "provisional",
                },
            )
        for item in cited:
            await self.edge_state.ensure_edge(
                source_type="market_setup_signal",
                source_id=signal.id,
                target_type="market_setup_signal",
                target_id=UUID(item["signal_id"]),
                relationship_type="derived_from_signal",
                confidence=hypothesis["confidence"],
                reasoning="Cited by the provisional pattern hypothesis.",
                properties={"origin": "pattern_discovery"},
            )
            await self.edge_state.ensure_edge(
                source_type="market_setup_signal",
                source_id=signal.id,
                target_type="raw_evidence",
                target_id=UUID(item["raw_evidence_id"]),
                relationship_type="sourced_from",
                confidence=hypothesis["confidence"],
                reasoning="Independent source evidence cited by the pattern hypothesis.",
                properties={
                    "origin": "pattern_discovery",
                    "lineage_key": item["lineage_key"],
                },
            )
        if hypothesis["historical_episode_id"]:
            await self.edge_state.ensure_edge(
                source_type="market_setup_signal",
                source_id=signal.id,
                target_type="historical_episode",
                target_id=UUID(hypothesis["historical_episode_id"]),
                relationship_type="rhymes_with",
                confidence=min(0.7, hypothesis["confidence"]),
                reasoning="Historical analogy is a hypothesis seed, not current evidence.",
                properties={"origin": "pattern_discovery", "evidence_role": "analogy"},
            )
        question = await self._ensure_research_question(
            signal=signal,
            hypothesis=hypothesis,
            primary=primary,
            first_raw_id=first_raw_id,
        )
        await self.session.commit()
        await self.session.refresh(signal)
        if question is not None:
            await self.session.refresh(question)
        return signal, question

    async def _ensure_research_question(
        self,
        *,
        signal: MarketSetupSignal,
        hypothesis: dict[str, Any],
        primary: dict[str, Any],
        first_raw_id: UUID,
    ) -> UnresolvedQuestion | None:
        coverage = await CanonicalStateService(self.session).ensure_coverage_map(
            subject_type="entity",
            subject_id=primary["entity_id"],
            create=lambda: CoverageMap(
                subject_type="entity",
                subject_id=primary["entity_id"],
                evidence_class_coverage_json={},
                overall_coverage_score=0.0,
            ),
        )

        question_text = self._clean_text(
            f"Test the provisional pattern '{hypothesis['label']}' across "
            f"{', '.join(hypothesis['affected_tickers'])}: {hypothesis['next_test']} "
            f"Falsify it if: {hypothesis['falsifier']}",
            limit=1600,
        )
        question_key = self._normalized_text(question_text)
        existing = list(
            (
                await self.session.execute(
                    select(UnresolvedQuestion).where(
                        UnresolvedQuestion.coverage_map_id == coverage.id,
                        UnresolvedQuestion.status.in_(["open", "investigating"]),
                    )
                )
            )
            .scalars()
            .all()
        )
        if any(
            self._normalized_text(row.question_text) == question_key for row in existing
        ):
            return None
        question = UnresolvedQuestion(
            coverage_map_id=coverage.id,
            question_text=question_text,
            urgency=5 if hypothesis["confidence"] >= 0.85 else 4,
            originating_evidence_id=first_raw_id,
            status="open",
        )
        self.session.add(question)
        await self.session.flush()
        await self.edge_state.ensure_edge(
            source_type="market_setup_signal",
            source_id=signal.id,
            target_type="unresolved_question",
            target_id=question.id,
            relationship_type="requires_test",
            confidence=hypothesis["confidence"],
            reasoning=hypothesis["falsifier"],
            properties={"origin": "pattern_discovery"},
        )
        return question

    async def _find_duplicate(
        self, hypothesis: dict[str, Any], *, now: datetime
    ) -> MarketSetupSignal | None:
        rows = list(
            (
                await self.session.execute(
                    select(MarketSetupSignal).where(
                        MarketSetupSignal.is_deprecated.is_(False),
                        MarketSetupSignal.signal_family == self.SIGNAL_FAMILY,
                        MarketSetupSignal.created_at
                        >= now - timedelta(days=settings.PATTERN_DISCOVERY_DEDUP_DAYS),
                    )
                )
            )
            .scalars()
            .all()
        )
        for row in rows:
            metadata = dict(row.metadata_json or {})
            existing = {
                "label": row.signal_name,
                "pattern_type": metadata.get("pattern_type"),
                "proposed_mechanism": metadata.get("proposed_mechanism"),
                "affected_tickers": metadata.get("affected_tickers") or [],
                "evidence_refs": metadata.get("evidence_refs") or [],
                "pattern_fingerprint": metadata.get("pattern_fingerprint"),
            }
            if self._is_duplicate_candidate(hypothesis, existing):
                return row
        return None

    @classmethod
    def _is_duplicate_candidate(
        cls,
        incoming: dict[str, Any],
        existing: dict[str, Any],
    ) -> bool:
        if existing.get("pattern_fingerprint") == incoming.get("pattern_fingerprint"):
            return True

        incoming_tickers = {
            str(value).strip().upper()
            for value in incoming.get("affected_tickers") or []
            if str(value).strip()
        }
        existing_tickers = {
            str(value).strip().upper()
            for value in existing.get("affected_tickers") or []
            if str(value).strip()
        }
        ticker_containment = cls._set_containment(incoming_tickers, existing_tickers)
        if ticker_containment < settings.PATTERN_DISCOVERY_DEDUP_TICKER_CONTAINMENT:
            return False

        evidence_containment = cls._set_containment(
            set(incoming.get("evidence_refs") or []),
            set(existing.get("evidence_refs") or []),
        )
        token_containment = cls._set_containment(
            cls._pattern_tokens(incoming), cls._pattern_tokens(existing)
        )
        return (
            evidence_containment
            >= settings.PATTERN_DISCOVERY_DEDUP_EVIDENCE_CONTAINMENT
            or token_containment >= settings.PATTERN_DISCOVERY_DEDUP_TOKEN_CONTAINMENT
        )

    @staticmethod
    def _set_containment(left: set[Any], right: set[Any]) -> float:
        if not left or not right:
            return 0.0
        return len(left & right) / min(len(left), len(right))

    async def retire_duplicate_signal(
        self,
        *,
        duplicate_id: UUID,
        canonical_id: UUID,
        reason: str,
    ) -> dict[str, Any]:
        duplicate = await self.session.get(MarketSetupSignal, duplicate_id)
        canonical = await self.session.get(MarketSetupSignal, canonical_id)
        if duplicate is None or canonical is None:
            raise ValueError("Both duplicate and canonical pattern signals must exist.")
        if (
            duplicate.signal_family != self.SIGNAL_FAMILY
            or canonical.signal_family != self.SIGNAL_FAMILY
        ):
            raise ValueError(
                "Only provisional pattern signals can be consolidated here."
            )

        edges = list(
            (
                await self.session.execute(
                    select(Edge).where(
                        or_(
                            (Edge.source_type == "market_setup_signal")
                            & (Edge.source_id == duplicate.id),
                            (Edge.target_type == "market_setup_signal")
                            & (Edge.target_id == duplicate.id),
                        )
                    )
                )
            )
            .scalars()
            .all()
        )
        question_ids = {
            edge.target_id
            for edge in edges
            if edge.source_type == "market_setup_signal"
            and edge.source_id == duplicate.id
            and edge.target_type == "unresolved_question"
        }
        if question_ids:
            questions = list(
                (
                    await self.session.execute(
                        select(UnresolvedQuestion).where(
                            UnresolvedQuestion.id.in_(question_ids)
                        )
                    )
                )
                .scalars()
                .all()
            )
            for question in questions:
                if question.status in {"open", "investigating"}:
                    question.status = "obsolete"

        await KnowledgeAuditService(self.session).record_change(
            node_type="market_setup_signal",
            node_id=duplicate.id,
            change_type="consolidated_duplicate",
            reason=reason,
            actor="pattern_discovery",
            source_type="market_setup_signal",
            source_id=canonical.id,
            subject_type=duplicate.subject_type,
            subject_id=duplicate.subject_id,
            metadata={
                "canonical_signal_id": str(canonical.id),
                "duplicate_snapshot": {
                    "label": duplicate.signal_name,
                    "pattern_type": (duplicate.metadata_json or {}).get("pattern_type"),
                    "affected_tickers": (duplicate.metadata_json or {}).get(
                        "affected_tickers"
                    ),
                    "evidence_refs": (duplicate.metadata_json or {}).get(
                        "evidence_refs"
                    ),
                    "proposed_mechanism": (duplicate.metadata_json or {}).get(
                        "proposed_mechanism"
                    ),
                    "falsifier": (duplicate.metadata_json or {}).get("falsifier"),
                },
                "removed_edge_count": len(edges),
                "obsoleted_question_ids": [str(value) for value in question_ids],
            },
        )
        canonical_metadata = dict(canonical.metadata_json or {})
        merged_ids = list(canonical_metadata.get("merged_duplicate_ids") or [])
        if str(duplicate.id) not in merged_ids:
            merged_ids.append(str(duplicate.id))
        canonical_metadata["merged_duplicate_ids"] = merged_ids
        canonical.metadata_json = canonical_metadata
        if edges:
            await self.session.execute(
                delete(Edge).where(Edge.id.in_([edge.id for edge in edges]))
            )
        await self.session.delete(duplicate)
        await self.session.commit()
        return {
            "canonical_signal_id": str(canonical.id),
            "retired_duplicate_id": str(duplicate.id),
            "removed_edges": len(edges),
            "obsoleted_questions": len(question_ids),
        }

    @classmethod
    def _pattern_fingerprint(cls, hypothesis: dict[str, Any]) -> str:
        payload = "|".join(
            [
                cls._normalized_text(hypothesis.get("pattern_type")),
                ",".join(sorted(hypothesis.get("affected_tickers") or [])),
                " ".join(sorted(cls._pattern_tokens(hypothesis))),
            ]
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]

    @classmethod
    def _pattern_tokens(cls, hypothesis: dict[str, Any]) -> set[str]:
        text = " ".join(
            str(hypothesis.get(key) or "")
            for key in ("label", "pattern_type", "proposed_mechanism")
        )
        return {
            token for token in re.findall(r"[a-z0-9]+", text.lower()) if len(token) > 2
        }

    @staticmethod
    def _tracked_context(item: dict[str, Any]) -> dict[str, Any]:
        return {
            key: item.get(key)
            for key in (
                "ticker",
                "entity_name",
                "sector",
                "industry",
                "list_type",
                "weight_pct",
                "market_value",
            )
        }

    @staticmethod
    def _unique_text(value: Any, *, upper: bool = False) -> list[str]:
        values = value if isinstance(value, list) else []
        result: list[str] = []
        seen: set[str] = set()
        for item in values:
            text = " ".join(str(item or "").split())
            text = text.upper() if upper else text
            if not text or text in seen:
                continue
            seen.add(text)
            result.append(text)
        return result

    @staticmethod
    def _clean_text(value: Any, *, limit: int) -> str:
        return " ".join(str(value or "").split())[:limit].strip()

    @staticmethod
    def _normalized_text(value: Any) -> str:
        return " ".join(re.findall(r"[a-z0-9]+", str(value or "").lower()))

    @staticmethod
    def _bounded_float(value: Any) -> float:
        try:
            return max(0.0, min(1.0, float(value or 0.0)))
        except (TypeError, ValueError):
            return 0.0
