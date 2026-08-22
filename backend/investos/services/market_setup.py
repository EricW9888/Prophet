from __future__ import annotations

import json
import math
import re
from dataclasses import asdict, dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import and_, desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from investos.core.llm import call_llm_json
from investos.models.entity import Entity, Security
from investos.models.evidence import RawEvidence, SourceItem
from investos.models.knowledge import Event
from investos.models.market_setup import MarketSetupSignal
from investos.models.portfolio import Position
from investos.models.source import Source
from investos.services.graph_edge_state import GraphEdgeStateService
from investos.services.knowledge_audit import KnowledgeAuditService
from investos.services.source import SourceService

_AMOUNT_RE = re.compile(r"[-+]?\$?\d[\d,]*(?:\.\d+)?")
_PERCENT_RE = re.compile(r"[-+]?\d[\d,]*(?:\.\d+)?\s?%")
_PERIOD_RE = re.compile(
    r"\b(?:Q[1-4]\s?(?:FY)?\s?\d{2,4}|FY\s?\d{2,4}|fiscal\s+\d{4}|calendar\s+\d{4})\b",
    re.I,
)
_SKIP_BACKFILL_ITEM_TYPES = {
    "conversation_turn",
    "source_feedback",
}
MARKET_SETUP_OUTCOMES = {
    "validated": 1.0,
    "partially_validated": 0.5,
    "invalidated": 0.0,
    "indeterminate": None,
}
MARKET_SETUP_ASSESSMENT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "assessment": {
            "type": "string",
            "enum": list(MARKET_SETUP_OUTCOMES),
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
_MARKET_SETUP_CLUES = (
    (
        "expectation_delta",
        re.compile(
            r"\b(expect(?:ed|s|ations?)?|consensus|whisper|hurdle|bar|beat|miss|upside|downside|estimate|revision)\b",
            re.I,
        ),
    ),
    (
        "earnings_or_guidance_setup",
        re.compile(
            r"\b(earnings|guidance|revenue|eps|margin|free cash flow|fcf|bookings|backlog|orders)\b",
            re.I,
        ),
    ),
    (
        "fundamental_metric_setup",
        re.compile(
            r"\b(pe|p/e|forward pe|ev/ebitda|roe|roic|debt|leverage|liquidity|interest coverage|gross margin|operating margin)\b",
            re.I,
        ),
    ),
    (
        "sentiment_or_positioning",
        re.compile(
            r"\b(sentiment|crowd(?:ed|ing)?|positioning|short interest|options?|implied move|flow|ownership|insider|institutional|whale)\b",
            re.I,
        ),
    ),
    (
        "competitive_read_through",
        re.compile(
            r"\b(competitor|competition|share gain|share loss|pricing|supply|demand|capacity|cycle|read[- ]through)\b",
            re.I,
        ),
    ),
    (
        "price_reaction",
        re.compile(
            r"\b(stock|shares?|price|market cap|rall(?:y|ied)|fell|dropped|rose|sold off|gap(?:ped)?|reaction)\b",
            re.I,
        ),
    ),
    (
        "event_countdown",
        re.compile(
            r"\b(report|release|conference|investor day|deadline|filing|hearing|vote|launch|trial|approval)\b",
            re.I,
        ),
    ),
)


@dataclass(frozen=True)
class MarketSetupSignalDraft:
    signal_name: str
    signal_family: str
    subject_type: str | None
    subject_id: UUID | None
    entity_id: UUID | None
    security_id: UUID | None
    ticker: str | None
    event_id: UUID | None
    raw_evidence_id: UUID | None
    source_item_id: UUID | None
    setup_context: str | None
    actual_context: str | None
    price_reaction: str | None
    value_text: str | None
    numeric_value: float | None
    unit: str | None
    currency: str | None
    period_label: str | None
    as_of: datetime | None
    event_time: datetime | None
    public_time: datetime | None
    eligible_action_time: datetime | None
    direction: str | None
    confidence: float
    investment_relevance: str | None
    next_test: str | None
    source_kind: str | None
    metadata: dict[str, Any]

    def to_context(self) -> dict[str, Any]:
        payload = asdict(self)
        for key in ("as_of", "event_time", "public_time", "eligible_action_time"):
            value = payload.get(key)
            if isinstance(value, datetime):
                payload[key] = value.isoformat()
        for key in (
            "subject_id",
            "entity_id",
            "security_id",
            "event_id",
            "raw_evidence_id",
            "source_item_id",
        ):
            value = payload.get(key)
            if isinstance(value, UUID):
                payload[key] = str(value)
        return payload


class MarketSetupSignalService:
    """Store investor setup, expectations, flows, sentiment, and metric context."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def create_signal(
        self,
        *,
        signal_name: str,
        signal_family: str | None = None,
        subject_type: str | None = None,
        subject_id: UUID | None = None,
        entity_id: UUID | None = None,
        security_id: UUID | None = None,
        ticker: str | None = None,
        event_id: UUID | None = None,
        raw_evidence_id: UUID | None = None,
        source_item_id: UUID | None = None,
        setup_context: str | None = None,
        actual_context: str | None = None,
        price_reaction: str | None = None,
        value_text: str | None = None,
        numeric_value: float | None = None,
        unit: str | None = None,
        currency: str | None = None,
        period_label: str | None = None,
        as_of: datetime | None = None,
        event_time: datetime | None = None,
        public_time: datetime | None = None,
        eligible_action_time: datetime | None = None,
        direction: str | None = None,
        confidence: float = 0.5,
        investment_relevance: str | None = None,
        next_test: str | None = None,
        source_kind: str | None = None,
        metadata: dict[str, Any] | None = None,
        commit_transaction: bool = True,
    ) -> MarketSetupSignal:
        clean_name = self._required_text(signal_name, field="signal_name")
        resolved = await self._resolve_subject(
            subject_type=subject_type,
            subject_id=subject_id,
            entity_id=entity_id,
            security_id=security_id,
            ticker=ticker,
        )
        metadata_payload = dict(metadata or {})
        metadata_payload.setdefault("ingested_by", "market_setup_signal_service")
        signal = MarketSetupSignal(
            subject_type=resolved["subject_type"],
            subject_id=resolved["subject_id"],
            entity_id=resolved["entity_id"],
            security_id=resolved["security_id"],
            ticker=resolved["ticker"],
            event_id=event_id,
            raw_evidence_id=raw_evidence_id,
            source_item_id=source_item_id,
            signal_name=clean_name,
            signal_family=self._text(signal_family) or "market_setup",
            setup_context=self._text(setup_context),
            actual_context=self._text(actual_context),
            price_reaction=self._text(price_reaction),
            value_text=self._text(value_text),
            numeric_value=self._finite(numeric_value),
            unit=self._text(unit),
            currency=self._upper(currency),
            period_label=self._text(period_label),
            as_of=self._date(as_of),
            event_time=self._date(event_time),
            public_time=self._date(public_time),
            eligible_action_time=self._date(eligible_action_time),
            direction=self._text(direction),
            confidence=max(0.0, min(1.0, float(confidence or 0.0))),
            investment_relevance=self._text(investment_relevance),
            next_test=self._text(next_test),
            source_kind=self._text(source_kind),
            metadata_json=metadata_payload,
        )
        self.session.add(signal)
        await self.session.flush()
        await self._attach_graph_edges(signal)
        if commit_transaction:
            await self.session.commit()
            await self.session.refresh(signal)
        return signal

    async def propose_outcome_assessment(
        self,
        *,
        signal_id: UUID,
        apply: bool = False,
        min_confidence: float = 0.75,
        grace_hours: int = 6,
    ) -> dict[str, Any] | None:
        signal = (
            await self.session.execute(
                select(MarketSetupSignal).where(MarketSetupSignal.id == signal_id)
            )
        ).scalar_one_or_none()
        if signal is None:
            return None
        now = datetime.now(UTC)
        if signal.outcome_status != "unscored" or not self._assessment_due(
            signal,
            now=now,
            grace_hours=grace_hours,
        ):
            return {
                "id": signal.id,
                "assessment": signal.outcome_status,
                "confidence": 0.0,
                "rationale": "Signal is not due for outcome assessment.",
                "limitations": "Wait for the explicit event/outcome horizon or observed result.",
                "assessment_evidence": [],
                "should_apply": False,
                "applied": False,
                "follow_up_evidence_count": 0,
                "recommended_research_query": None,
            }

        subjects = self._assessment_subjects(signal)
        after_time = (
            signal.event_time or signal.public_time or signal.as_of or signal.created_at
        )
        follow_up_evidence = await SourceService(self.session).follow_up_graph_evidence(
            subjects=subjects,
            after_time=after_time,
            limit=24,
        )
        if signal.raw_evidence_id and (signal.actual_context or signal.price_reaction):
            follow_up_evidence.insert(
                0,
                {
                    "id": str(signal.raw_evidence_id),
                    "node_type": "raw_evidence",
                    "text": self._outcome_observation_text(signal),
                    "date": (
                        signal.public_time or signal.event_time or signal.created_at
                    ).isoformat(),
                    "tier": None,
                    "importance": None,
                    "contradiction_role": None,
                    "target_horizon": None,
                },
            )
        follow_up_evidence = self._deduplicate_assessment_evidence(follow_up_evidence)
        if not follow_up_evidence:
            return {
                "id": signal.id,
                "assessment": "indeterminate",
                "confidence": 0.0,
                "rationale": "No later subject-linked evidence was found.",
                "limitations": "The setup remains unscored until a source-backed outcome is available.",
                "assessment_evidence": [],
                "should_apply": False,
                "applied": False,
                "follow_up_evidence_count": 0,
                "recommended_research_query": self._outcome_followup_query(signal),
            }

        prompt_payload = {
            "task": "Assess whether an earlier market setup or expectation was validated by later evidence.",
            "allowed_assessments": list(MARKET_SETUP_OUTCOMES),
            "signal": {
                "id": str(signal.id),
                "name": signal.signal_name,
                "family": signal.signal_family,
                "ticker": signal.ticker,
                "setup_context": signal.setup_context,
                "expected_direction": signal.direction,
                "event_time": (
                    signal.event_time.isoformat() if signal.event_time else None
                ),
                "public_time": (
                    signal.public_time.isoformat() if signal.public_time else None
                ),
                "actual_context": signal.actual_context,
                "price_reaction": signal.price_reaction,
                "investment_relevance": signal.investment_relevance,
                "next_test": signal.next_test,
            },
            "subjects": subjects,
            "later_evidence": follow_up_evidence,
            "instructions": [
                "Use only later_evidence IDs from this payload as assessment_evidence_ids.",
                "Judge the stated setup, expected channel, timing, and magnitude rather than whether the broad story sounds plausible.",
                "Return indeterminate when evidence is adjacent, contradictory without resolution, stale, or insufficiently attributable.",
                "Do not infer a validated setup from price movement alone when the proposed mechanism is unverified.",
            ],
        }
        try:
            raw = await call_llm_json(
                system_prompt=(
                    "You are Prophet's market-setup outcome assessor. Calibrate prior investor setups "
                    "conservatively using only attributable later evidence. Preserve uncertainty."
                ),
                user_prompt=json.dumps(prompt_payload, ensure_ascii=True, default=str),
                schema=MARKET_SETUP_ASSESSMENT_SCHEMA,
                timeout_seconds=12,
            )
        except Exception as exc:
            return {
                "id": signal.id,
                "assessment": "indeterminate",
                "confidence": 0.0,
                "rationale": "Automated assessment provider failed.",
                "limitations": str(exc),
                "assessment_evidence": [],
                "should_apply": False,
                "applied": False,
                "follow_up_evidence_count": len(follow_up_evidence),
                "recommended_research_query": self._outcome_followup_query(signal),
            }

        proposal = self._sanitize_assessment_proposal(
            raw,
            allowed_evidence_ids={UUID(item["id"]) for item in follow_up_evidence},
            min_confidence=min_confidence,
        )
        result = {
            "id": signal.id,
            **proposal,
            "applied": False,
            "follow_up_evidence_count": len(follow_up_evidence),
            "recommended_research_query": (
                None
                if proposal["should_apply"]
                else self._outcome_followup_query(signal)
            ),
        }
        if apply and proposal["should_apply"]:
            result["applied"] = await self._apply_outcome_assessment(
                signal=signal,
                proposal=proposal,
                evidence_lookup={UUID(item["id"]): item for item in follow_up_evidence},
            )
        return result

    async def assess_due_signals(
        self,
        *,
        limit: int = 5,
        scan_limit: int = 500,
        apply: bool = True,
        min_confidence: float = 0.75,
        grace_hours: int = 6,
        retry_hours: int = 24,
        research_missing_evidence: bool = False,
        research_limit: int = 1,
    ) -> dict[str, Any]:
        clean_limit = max(1, min(int(limit or 5), 20))
        clean_scan_limit = max(clean_limit, min(int(scan_limit or 500), 5000))
        clean_research_limit = max(0, min(int(research_limit or 0), 5))
        now = datetime.now(UTC)
        event_cutoff = now - timedelta(
            hours=max(0, min(int(grace_hours or 0), 24 * 30))
        )
        outcome_due_text = MarketSetupSignal.metadata_json["outcome_due_at"].astext
        retry_at_text = MarketSetupSignal.metadata_json["outcome_assessment_attempt"][
            "next_retry_at"
        ].astext
        due_clause = or_(
            MarketSetupSignal.actual_context.is_not(None),
            MarketSetupSignal.price_reaction.is_not(None),
            MarketSetupSignal.event_time <= event_cutoff,
            outcome_due_text.is_not(None),
        )
        retry_ready_clause = or_(
            retry_at_text.is_(None),
            retry_at_text <= now.isoformat(),
        )
        deferred_count = int(
            (
                await self.session.execute(
                    select(func.count())
                    .select_from(MarketSetupSignal)
                    .where(
                        MarketSetupSignal.outcome_status == "unscored",
                        due_clause,
                        retry_at_text.is_not(None),
                        retry_at_text > now.isoformat(),
                    )
                )
            ).scalar_one()
            or 0
        )
        rows = list(
            (
                await self.session.execute(
                    select(MarketSetupSignal)
                    .where(
                        MarketSetupSignal.outcome_status == "unscored",
                        due_clause,
                        retry_ready_clause,
                    )
                    .order_by(
                        MarketSetupSignal.updated_at.asc(),
                        MarketSetupSignal.event_time.asc().nulls_last(),
                        MarketSetupSignal.public_time.asc().nulls_last(),
                        MarketSetupSignal.created_at.asc(),
                    )
                    .limit(clean_scan_limit)
                )
            )
            .scalars()
            .all()
        )
        due_candidates = [
            signal
            for signal in rows
            if self._assessment_due(signal, now=now, grace_hours=grace_hours)
        ]
        eligible = [
            signal
            for signal in due_candidates
            if self._assessment_retry_ready(signal, now=now)
        ]
        due = eligible[:clean_limit]
        results = []
        research_attempted = 0
        research_started = 0
        for signal in due:
            result = await self.propose_outcome_assessment(
                signal_id=signal.id,
                apply=apply,
                min_confidence=min_confidence,
                grace_hours=grace_hours,
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
                        followup = await self._run_outcome_followup_research(
                            signal=signal,
                            query=str(result["recommended_research_query"]),
                        )
                    except Exception as exc:
                        followup = {
                            "started": False,
                            "reason": f"research_followup_failed: {exc}",
                            "evidence_id": None,
                            "processed": False,
                            "query": str(result["recommended_research_query"]),
                            "title": self._outcome_followup_title(signal),
                        }
                    result["research_followup"] = followup
                    if followup.get("started"):
                        research_started += 1
                if apply and not result.get("applied"):
                    await self._record_assessment_attempt(
                        signal=signal,
                        result=result,
                        retry_hours=retry_hours,
                    )
                results.append(result)
        return {
            "scanned": len(rows),
            "due": len(due),
            "eligible": len(eligible),
            "deferred": deferred_count,
            "proposed": len(results),
            "applied": sum(1 for result in results if result.get("applied")),
            "research_attempted": research_attempted,
            "research_started": research_started,
            "results": results,
        }

    async def _run_outcome_followup_research(
        self,
        *,
        signal: MarketSetupSignal,
        query: str,
    ) -> dict[str, Any]:
        from investos.services.research import ResearchService

        result = await ResearchService(self.session).run_ad_hoc_request(
            query=query,
            title=self._outcome_followup_title(signal),
            source_item_type="market_setup_outcome_followup",
            metadata_json={
                "trigger": "market_setup_assessment_followup",
                "market_setup_signal_id": str(signal.id),
                "signal_name": signal.signal_name,
                "signal_family": signal.signal_family,
                "ticker": signal.ticker,
                "subject_type": signal.subject_type,
                "subject_id": str(signal.subject_id) if signal.subject_id else None,
                "event_time": (
                    signal.event_time.isoformat() if signal.event_time else None
                ),
                "setup_context": self._bounded_text(signal.setup_context, 1000),
                "query": query[:500],
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

    async def _record_assessment_attempt(
        self,
        *,
        signal: MarketSetupSignal,
        result: dict[str, Any],
        retry_hours: int,
    ) -> None:
        now = datetime.now(UTC)
        metadata = dict(signal.metadata_json or {})
        previous = metadata.get("outcome_assessment_attempt")
        previous_count = 0
        if isinstance(previous, dict):
            try:
                previous_count = int(previous.get("attempt_count") or 0)
            except (TypeError, ValueError):
                previous_count = 0
        retry_delay = timedelta(hours=max(1, min(int(retry_hours or 24), 24 * 30)))
        metadata["outcome_assessment_attempt"] = {
            "attempt_count": previous_count + 1,
            "attempted_at": now.isoformat(),
            "next_retry_at": (now + retry_delay).isoformat(),
            "assessment": result.get("assessment"),
            "confidence": float(result.get("confidence") or 0.0),
            "rationale": self._bounded_text(result.get("rationale"), 1000),
            "limitations": self._bounded_text(result.get("limitations"), 700),
            "recommended_research_query": self._bounded_text(
                result.get("recommended_research_query"), 500
            ),
            "research_followup": (
                json.loads(json.dumps(result.get("research_followup"), default=str))
                if result.get("research_followup")
                else None
            ),
        }
        signal.metadata_json = metadata
        signal.mark_updated()
        await self.session.commit()

    async def _apply_outcome_assessment(
        self,
        *,
        signal: MarketSetupSignal,
        proposal: dict[str, Any],
        evidence_lookup: dict[UUID, dict[str, Any]],
    ) -> bool:
        evidence_ids: list[UUID] = proposal["assessment_evidence"]
        assessed_at = datetime.now(UTC)
        metadata = dict(signal.metadata_json or {})
        metadata["outcome_assessment"] = {
            "assessed_at": assessed_at.isoformat(),
            "confidence": proposal["confidence"],
            "rationale": proposal["rationale"],
            "limitations": proposal["limitations"],
            "assessment_evidence_ids": [str(item) for item in evidence_ids],
            "assessor": "market_setup_outcome_assessor",
        }
        signal.outcome_status = proposal["assessment"]
        signal.outcome_score = MARKET_SETUP_OUTCOMES[proposal["assessment"]]
        signal.outcome_notes = self._assessment_notes(
            signal=signal,
            proposal=proposal,
            evidence_lookup=evidence_lookup,
        )
        signal.metadata_json = metadata
        signal.mark_updated()
        await KnowledgeAuditService(self.session).record_change(
            node_type="market_setup_signal",
            node_id=signal.id,
            change_type="outcome_assessed",
            reason="Later subject-linked evidence was used to calibrate the stored market setup.",
            actor="market_setup_outcome_assessor",
            source_type="market_setup_signal",
            source_id=signal.id,
            subject_type=signal.subject_type,
            subject_id=signal.subject_id,
            metadata=metadata["outcome_assessment"],
        )
        await self.session.commit()
        return True

    async def create_from_evidence(
        self,
        evidence: RawEvidence,
        source: Source | None = None,
    ) -> MarketSetupSignal | None:
        draft = self.draft_from_evidence(evidence, source)
        if draft is None:
            return None
        return await self.create_signal(**asdict(draft))

    async def backfill_from_existing_evidence(
        self,
        *,
        limit: int = 500,
        apply: bool = False,
        min_confidence: float = 0.45,
        include_conversation_turns: bool = False,
    ) -> dict[str, Any]:
        """Seed setup signals from already-stored source evidence.

        This is deliberately a durable-indexing pass, not a thesis promoter. It
        only creates source-dated market setup rows that later reasoning can use
        or reject.
        """
        clean_limit = max(1, min(int(limit or 500), 2500))
        known_subjects = await self._known_subject_catalog()
        rows = (
            await self.session.execute(
                select(RawEvidence, SourceItem, Source)
                .join(Source, RawEvidence.source_id == Source.id)
                .outerjoin(SourceItem, SourceItem.raw_evidence_id == RawEvidence.id)
                .order_by(
                    desc(RawEvidence.public_time),
                    desc(RawEvidence.event_time),
                    desc(RawEvidence.created_at),
                )
                .limit(clean_limit)
            )
        ).all()

        result: dict[str, Any] = {
            "dry_run": not apply,
            "scanned": 0,
            "candidates": 0,
            "created": 0,
            "skipped_existing": 0,
            "skipped_no_signal": 0,
            "skipped_quality_gate": 0,
            "skipped_unsafe_origin": 0,
            "examples": [],
        }
        for evidence, source_item, source in rows:
            result["scanned"] += 1
            if self._should_skip_backfill(
                evidence, include_conversation_turns=include_conversation_turns
            ):
                result["skipped_unsafe_origin"] += 1
                continue
            draft = self.draft_from_evidence_or_text(
                evidence=evidence,
                source=source,
                source_item=source_item,
                known_subjects=known_subjects,
            )
            if draft is None:
                result["skipped_no_signal"] += 1
                continue
            if draft.confidence < min_confidence:
                result["skipped_quality_gate"] += 1
                continue
            result["candidates"] += 1
            if await self._draft_exists(draft):
                result["skipped_existing"] += 1
                continue
            if len(result["examples"]) < 10:
                result["examples"].append(self._candidate_preview(draft))
            if apply:
                await self.create_signal(**asdict(draft))
                result["created"] += 1
        return result

    async def relevant_signals(
        self,
        *,
        subject_type: str,
        subject_id: UUID | None,
        query: str | None = None,
        position_details: dict[UUID, dict] | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        clauses = []
        ticker_filter: set[str] = set()
        entity_filter: set[UUID] = set()
        security_filter: set[UUID] = set()

        if subject_type == "portfolio" and position_details:
            for detail in position_details.values():
                if detail.get("ticker"):
                    ticker_filter.add(str(detail["ticker"]).upper())
                if detail.get("entity_id"):
                    try:
                        entity_filter.add(UUID(str(detail["entity_id"])))
                    except (TypeError, ValueError):
                        pass
                if detail.get("security_id"):
                    try:
                        security_filter.add(UUID(str(detail["security_id"])))
                    except (TypeError, ValueError):
                        pass
            clauses.append(MarketSetupSignal.subject_type == "portfolio")
        elif subject_type == "entity" and subject_id is not None:
            clauses.extend(
                [
                    (MarketSetupSignal.subject_type == "entity")
                    & (MarketSetupSignal.subject_id == subject_id),
                    MarketSetupSignal.entity_id == subject_id,
                ]
            )
            securities = (
                (
                    await self.session.execute(
                        select(Security).where(Security.entity_id == subject_id)
                    )
                )
                .scalars()
                .all()
            )
            for security in securities:
                security_filter.add(security.id)
                ticker_filter.add(str(security.ticker).upper())
        elif subject_type == "position" and subject_id is not None:
            clauses.append(
                (MarketSetupSignal.subject_type == "position")
                & (MarketSetupSignal.subject_id == subject_id)
            )
            row = (
                await self.session.execute(
                    select(Position, Security)
                    .join(Security, Position.security_id == Security.id)
                    .where(Position.id == subject_id)
                    .limit(1)
                )
            ).first()
            if row is not None:
                position, security = row
                security_filter.add(security.id)
                entity_filter.add(security.entity_id)
                ticker_filter.add(str(security.ticker).upper())
        elif subject_id is not None:
            clauses.append(
                (MarketSetupSignal.subject_type == subject_type)
                & (MarketSetupSignal.subject_id == subject_id)
            )

        if ticker_filter:
            clauses.append(MarketSetupSignal.ticker.in_(sorted(ticker_filter)))
        if entity_filter:
            clauses.append(MarketSetupSignal.entity_id.in_(entity_filter))
        if security_filter:
            clauses.append(MarketSetupSignal.security_id.in_(security_filter))
        if not clauses:
            return []

        stmt = select(MarketSetupSignal).where(or_(*clauses))
        # Do not hard-filter by query terms here. Once a signal is tied to the
        # subject or portfolio, phrasing drift should not make it disappear from
        # the analyst packet; the reasoning pass can decide whether it matters.
        rows = (
            (
                await self.session.execute(
                    stmt.order_by(
                        desc(MarketSetupSignal.public_time),
                        desc(MarketSetupSignal.as_of),
                        desc(MarketSetupSignal.created_at),
                    ).limit(limit)
                )
            )
            .scalars()
            .all()
        )
        return [await self._context_from_signal(signal) for signal in rows]

    async def _known_subject_catalog(self) -> list[dict[str, Any]]:
        position_rows = (
            await self.session.execute(
                select(
                    Position.security_id, Position.list_type, Position.weight_pct
                ).where(Position.security_id.is_not(None))
            )
        ).all()
        position_by_security: dict[UUID, dict[str, Any]] = {}
        for security_id, list_type, weight_pct in position_rows:
            position_by_security[security_id] = {
                "list_type": list_type,
                "weight_pct": None if weight_pct is None else float(weight_pct),
            }

        rows = (
            await self.session.execute(
                select(Security, Entity)
                .join(Entity, Security.entity_id == Entity.id)
                .where(Security.is_active.is_(True))
                .order_by(Security.ticker)
            )
        ).all()
        subjects: list[dict[str, Any]] = []
        for security, entity in rows:
            ticker = self._upper(security.ticker)
            aliases = [alias for alias in (entity.aliases or []) if self._text(alias)]
            tokens = [
                token for token in [ticker, entity.name, *aliases] if self._text(token)
            ]
            position = position_by_security.get(security.id)
            subjects.append(
                {
                    "entity_id": entity.id,
                    "security_id": security.id,
                    "ticker": ticker,
                    "name": entity.name,
                    "aliases": aliases,
                    "tokens": tokens,
                    "portfolio_relevant": bool(position),
                    "position": position,
                }
            )
        return subjects

    async def _draft_exists(self, draft: MarketSetupSignalDraft) -> bool:
        source_clauses = []
        if draft.raw_evidence_id is not None:
            source_clauses.append(
                MarketSetupSignal.raw_evidence_id == draft.raw_evidence_id
            )
        if draft.source_item_id is not None:
            source_clauses.append(
                MarketSetupSignal.source_item_id == draft.source_item_id
            )
        if not source_clauses:
            return False
        existing = (
            await self.session.execute(
                select(MarketSetupSignal.id)
                .where(
                    and_(
                        MarketSetupSignal.signal_name == draft.signal_name,
                        MarketSetupSignal.signal_family == draft.signal_family,
                        or_(*source_clauses),
                    )
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        return existing is not None

    async def _context_from_signal(self, signal: MarketSetupSignal) -> dict[str, Any]:
        source_name = None
        source_type = None
        evidence_title = None
        url = None
        raw_id = signal.raw_evidence_id
        if raw_id is None and signal.source_item_id is not None:
            source_item = (
                await self.session.execute(
                    select(SourceItem).where(SourceItem.id == signal.source_item_id)
                )
            ).scalar_one_or_none()
            if source_item is not None:
                raw_id = source_item.raw_evidence_id
        if raw_id is not None:
            row = (
                await self.session.execute(
                    select(RawEvidence, Source)
                    .join(Source, RawEvidence.source_id == Source.id)
                    .where(RawEvidence.id == raw_id)
                    .limit(1)
                )
            ).first()
            if row is not None:
                raw, source = row
                source_name = source.name
                source_type = source.source_type
                evidence_title = raw.title
                url = raw.url
        return {
            "id": str(signal.id),
            "signal_name": signal.signal_name,
            "signal_family": signal.signal_family,
            "ticker": signal.ticker,
            "subject_type": signal.subject_type,
            "subject_id": str(signal.subject_id) if signal.subject_id else None,
            "setup_context": signal.setup_context,
            "actual_context": signal.actual_context,
            "price_reaction": signal.price_reaction,
            "value_text": signal.value_text,
            "numeric_value": (
                None if signal.numeric_value is None else float(signal.numeric_value)
            ),
            "unit": signal.unit,
            "period_label": signal.period_label,
            "as_of": signal.as_of.isoformat() if signal.as_of else None,
            "event_time": signal.event_time.isoformat() if signal.event_time else None,
            "public_time": (
                signal.public_time.isoformat() if signal.public_time else None
            ),
            "eligible_action_time": (
                signal.eligible_action_time.isoformat()
                if signal.eligible_action_time
                else None
            ),
            "direction": signal.direction,
            "confidence": float(signal.confidence or 0.0),
            "investment_relevance": signal.investment_relevance,
            "next_test": signal.next_test,
            "source_kind": signal.source_kind,
            "outcome_status": signal.outcome_status,
            "outcome_notes": signal.outcome_notes,
            "outcome_score": signal.outcome_score,
            "outcome_assessment": (signal.metadata_json or {}).get(
                "outcome_assessment"
            ),
            "outcome_assessment_attempt": (signal.metadata_json or {}).get(
                "outcome_assessment_attempt"
            ),
            "source_name": source_name,
            "source_type": source_type,
            "evidence_title": evidence_title,
            "url": url,
        }

    @classmethod
    def _assessment_due(
        cls,
        signal: MarketSetupSignal,
        *,
        now: datetime,
        grace_hours: int,
    ) -> bool:
        if signal.actual_context or signal.price_reaction:
            return True
        metadata = signal.metadata_json or {}
        explicit_due = cls._date(metadata.get("outcome_due_at"))
        if explicit_due is not None:
            return explicit_due <= cls._date(now)
        event_time = cls._date(signal.event_time)
        if event_time is None:
            return False
        grace = timedelta(hours=max(0, min(int(grace_hours or 0), 24 * 30)))
        return event_time + grace <= cls._date(now)

    @classmethod
    def _assessment_retry_ready(
        cls,
        signal: MarketSetupSignal,
        *,
        now: datetime,
    ) -> bool:
        metadata = signal.metadata_json or {}
        attempt = metadata.get("outcome_assessment_attempt")
        if not isinstance(attempt, dict):
            return True
        next_retry = cls._date(attempt.get("next_retry_at"))
        return next_retry is None or next_retry <= cls._date(now)

    @classmethod
    def _outcome_followup_title(cls, signal: MarketSetupSignal) -> str:
        subject = cls._outcome_subject_label(signal, limit=70)
        return f"Market setup outcome follow-up for {subject or 'stored signal'}"

    @classmethod
    def _outcome_followup_query(cls, signal: MarketSetupSignal) -> str:
        subject = cls._outcome_subject_label(signal, limit=90)
        setup = cls._bounded_text(
            signal.setup_context or signal.value_text or signal.signal_name,
            260,
        )
        reference_time = (
            signal.event_time or signal.public_time or signal.as_of or signal.created_at
        )
        date_text = (
            reference_time.date().isoformat()
            if isinstance(reference_time, datetime)
            else "the setup date"
        )
        return (
            f'Find later primary or high-quality evidence after {date_text} that directly tests whether "{setup}" '
            f"materialized for {subject or 'the subject'}. Verify the stated mechanism, timing, magnitude, "
            "reported outcome, and subsequent market or estimate reaction; separate direct outcome evidence "
            "from adjacent commentary."
        )

    @classmethod
    def _outcome_subject_label(
        cls,
        signal: MarketSetupSignal,
        *,
        limit: int,
    ) -> str | None:
        ticker = cls._upper(signal.ticker)
        if ticker and re.fullmatch(r"[A-Z0-9][A-Z0-9.\-]{0,14}", ticker):
            return ticker
        name = cls._text(signal.signal_name)
        return cls._bounded_text(name.replace("_", " ") if name else None, limit)

    @staticmethod
    def _bounded_text(value: Any, limit: int) -> str | None:
        text = MarketSetupSignalService._text(value)
        if text is None:
            return None
        return text[: max(1, int(limit))]

    @staticmethod
    def _assessment_subjects(signal: MarketSetupSignal) -> list[dict[str, Any]]:
        if signal.subject_type and signal.subject_id:
            return [
                {
                    "subject_type": signal.subject_type,
                    "subject_id": str(signal.subject_id),
                    "ticker": signal.ticker,
                    "relationship_type": "setup_subject",
                    "confidence": float(signal.confidence or 0.0),
                }
            ]
        if signal.entity_id:
            return [
                {
                    "subject_type": "entity",
                    "subject_id": str(signal.entity_id),
                    "ticker": signal.ticker,
                    "relationship_type": "setup_entity",
                    "confidence": float(signal.confidence or 0.0),
                }
            ]
        return []

    @staticmethod
    def _outcome_observation_text(signal: MarketSetupSignal) -> str:
        parts = []
        if signal.actual_context:
            parts.append(f"Actual: {signal.actual_context}")
        if signal.price_reaction:
            parts.append(f"Price reaction: {signal.price_reaction}")
        return " | ".join(parts)

    @staticmethod
    def _deduplicate_assessment_evidence(
        items: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        deduplicated = []
        seen: set[str] = set()
        for item in items:
            item_id = str(item.get("id") or "").strip()
            if not item_id or item_id in seen:
                continue
            try:
                UUID(item_id)
            except ValueError:
                continue
            seen.add(item_id)
            deduplicated.append(item)
        return deduplicated

    @staticmethod
    def _sanitize_assessment_proposal(
        raw: dict[str, Any],
        *,
        allowed_evidence_ids: set[UUID],
        min_confidence: float,
    ) -> dict[str, Any]:
        assessment = str(raw.get("assessment") or "indeterminate").strip().lower()
        if assessment not in MARKET_SETUP_OUTCOMES:
            assessment = "indeterminate"
        try:
            confidence = float(raw.get("confidence") or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        confidence = max(0.0, min(1.0, confidence))
        threshold = max(0.0, min(1.0, float(min_confidence or 0.0)))
        evidence_ids: list[UUID] = []
        for raw_id in raw.get("assessment_evidence_ids") or []:
            try:
                evidence_id = UUID(str(raw_id))
            except (TypeError, ValueError):
                continue
            if evidence_id in allowed_evidence_ids and evidence_id not in evidence_ids:
                evidence_ids.append(evidence_id)
        should_apply = confidence >= threshold and (
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
    def _assessment_notes(
        *,
        signal: MarketSetupSignal,
        proposal: dict[str, Any],
        evidence_lookup: dict[UUID, dict[str, Any]],
    ) -> str:
        evidence_bits = []
        for evidence_id in proposal["assessment_evidence"]:
            evidence = evidence_lookup.get(evidence_id)
            if evidence is None:
                continue
            text = " ".join(str(evidence.get("text") or "").split())
            evidence_bits.append(
                f"{evidence.get('node_type')}:{evidence_id} {text[:160]}"
            )
        parts = [
            "Auto-assessed by market setup outcome assessor.",
            f"signal={signal.signal_name}",
            f"assessment={proposal['assessment']}",
            f"confidence={proposal['confidence']:.2f}",
        ]
        if proposal["rationale"]:
            parts.append(f"rationale={proposal['rationale'][:500]}")
        if proposal["limitations"]:
            parts.append(f"limitations={proposal['limitations'][:300]}")
        if evidence_bits:
            parts.append("evidence=" + " || ".join(evidence_bits[:5]))
        return " | ".join(parts)

    async def _resolve_subject(
        self,
        *,
        subject_type: str | None,
        subject_id: UUID | None,
        entity_id: UUID | None,
        security_id: UUID | None,
        ticker: str | None,
    ) -> dict[str, Any]:
        clean_ticker = self._upper(ticker)
        resolved_security_id = security_id
        resolved_entity_id = entity_id
        if resolved_security_id is not None:
            security = (
                await self.session.execute(
                    select(Security).where(Security.id == resolved_security_id)
                )
            ).scalar_one_or_none()
            if security is not None:
                resolved_entity_id = resolved_entity_id or security.entity_id
                clean_ticker = clean_ticker or self._upper(security.ticker)
        elif clean_ticker:
            security = (
                await self.session.execute(
                    select(Security)
                    .where(Security.ticker.ilike(clean_ticker))
                    .order_by(desc(Security.is_active))
                    .limit(1)
                )
            ).scalar_one_or_none()
            if security is not None:
                resolved_security_id = security.id
                resolved_entity_id = resolved_entity_id or security.entity_id
        if subject_type == "entity" and subject_id is not None:
            resolved_entity_id = resolved_entity_id or subject_id
        elif subject_type == "security" and subject_id is not None:
            resolved_security_id = resolved_security_id or subject_id
        if subject_type is None and resolved_entity_id is not None:
            subject_type = "entity"
            subject_id = resolved_entity_id
        return {
            "subject_type": self._text(subject_type),
            "subject_id": subject_id,
            "entity_id": resolved_entity_id,
            "security_id": resolved_security_id,
            "ticker": clean_ticker,
        }

    async def _attach_graph_edges(self, signal: MarketSetupSignal) -> int:
        targets: list[tuple[str, UUID, str, float]] = []
        if signal.subject_type and signal.subject_id:
            targets.append(
                (
                    signal.subject_type,
                    signal.subject_id,
                    "captures_market_setup_for",
                    0.88,
                )
            )
        if signal.entity_id:
            targets.append(
                ("entity", signal.entity_id, "captures_market_setup_for", 0.9)
            )
        if signal.security_id:
            targets.append(
                ("security", signal.security_id, "captures_market_setup_for", 0.86)
            )
        if signal.event_id:
            targets.append(("event", signal.event_id, "sets_up_event", 0.84))
        if signal.raw_evidence_id:
            targets.append(
                ("raw_evidence", signal.raw_evidence_id, "sourced_from", 0.78)
            )
        if signal.source_item_id:
            targets.append(("source_item", signal.source_item_id, "sourced_from", 0.78))
        if signal.subject_type == "portfolio" or (signal.metadata_json or {}).get(
            "portfolio_relevant"
        ):
            targets.append(
                (
                    "portfolio",
                    UUID("00000000-0000-0000-0000-000000000000"),
                    "informs_portfolio_setup",
                    0.74,
                )
            )

        seen: set[tuple[str, UUID, str]] = set()
        edge_state = GraphEdgeStateService(self.session)
        created_count = 0
        for target_type, target_id, relationship_type, confidence in targets:
            key = (target_type, target_id, relationship_type)
            if key in seen:
                continue
            seen.add(key)
            _, created = await edge_state.ensure_edge(
                source_type="market_setup_signal",
                source_id=signal.id,
                target_type=target_type,
                target_id=target_id,
                relationship_type=relationship_type,
                confidence=confidence,
                reasoning=signal.investment_relevance,
                properties={
                    "origin": "market_setup_signal_service",
                    "signal_family": signal.signal_family,
                },
            )
            created_count += int(created)
        return created_count

    @classmethod
    def draft_from_evidence_or_text(
        cls,
        *,
        evidence: Any,
        source: Any | None = None,
        source_item: Any | None = None,
        known_subjects: list[dict[str, Any]] | None = None,
    ) -> MarketSetupSignalDraft | None:
        metadata = dict(getattr(evidence, "metadata_json", None) or {})
        source_item_id = getattr(source_item, "id", None)
        explicit = cls.draft_from_evidence(evidence, source)
        if explicit is not None:
            matched = cls._match_known_subject(
                metadata,
                cls._compose_evidence_text(evidence, source_item),
                known_subjects or [],
            )
            metadata_payload = dict(explicit.metadata)
            metadata_payload.setdefault("derived_from_existing_evidence", True)
            metadata_payload.setdefault("backfill_extractor", "structured_metadata")
            enriched = replace(
                explicit,
                source_item_id=explicit.source_item_id or source_item_id,
                subject_type=explicit.subject_type or ("entity" if matched else None),
                subject_id=explicit.subject_id or (matched or {}).get("entity_id"),
                entity_id=explicit.entity_id or (matched or {}).get("entity_id"),
                security_id=explicit.security_id or (matched or {}).get("security_id"),
                ticker=explicit.ticker or (matched or {}).get("ticker"),
                metadata=metadata_payload,
            )
            return enriched

        text = cls._compose_evidence_text(evidence, source_item)
        if not text:
            return None
        families = cls._signal_families_from_text(text)
        if not families:
            return None
        matched = cls._match_known_subject(metadata, text, known_subjects or [])
        if matched is None and not cls._explicit_portfolio_relevance(metadata, text):
            return None

        signal_family = families[0]
        subject_label = cls._subject_label(matched) if matched else "Portfolio"
        setup_context = cls._best_sentence(text, families, matched)
        if not setup_context:
            return None
        actual_context = cls._sentence_matching(
            text,
            re.compile(
                r"\b(actual|reported|result|beat|miss|guidance|raised|cut|fell|rose|reaction)\b",
                re.I,
            ),
            exclude=setup_context,
        )
        price_reaction = cls._sentence_matching(
            text,
            re.compile(
                r"\b(shares?|stock|price|rall(?:y|ied)|fell|dropped|rose|sold off|reaction|gap(?:ped)?)\b",
                re.I,
            ),
        )
        value_text = cls._sentence_matching(
            text,
            re.compile(
                r"(%|\$|\b\d+(?:\.\d+)?x\b|\b\d+(?:\.\d+)?\s?(?:bps|million|billion|bn|m)\b)",
                re.I,
            ),
        )
        period_match = _PERIOD_RE.search(text)
        direction = cls._direction_from_text(text)
        public_time = cls._date(
            getattr(evidence, "public_time", None)
            or getattr(evidence, "created_at", None)
        )
        signal_name = cls._signal_name_from_text(
            title=cls._text(getattr(evidence, "title", None)),
            subject_label=subject_label,
            signal_family=signal_family,
            setup_context=setup_context,
        )
        metadata_payload = {
            "derived_from_existing_evidence": True,
            "backfill_extractor": "text_market_setup_seed",
            "backfill_extractor_version": 1,
            "matched_signal_families": families,
            "source_item_processing_status": getattr(
                source_item, "processing_status", None
            ),
            "source_id": (
                str(getattr(source, "id", "")) if getattr(source, "id", None) else None
            ),
            "source_name": getattr(source, "name", None),
            "source_type": getattr(source, "source_type", None),
            "source_title": getattr(evidence, "title", None),
            "portfolio_relevant": bool(
                (matched or {}).get("portfolio_relevant")
                or cls._explicit_portfolio_relevance(metadata, text)
            ),
        }
        if matched:
            metadata_payload["matched_subject"] = {
                "ticker": matched.get("ticker"),
                "name": matched.get("name"),
                "portfolio_relevant": matched.get("portfolio_relevant"),
            }
        return MarketSetupSignalDraft(
            signal_name=signal_name,
            signal_family=signal_family,
            subject_type="entity" if matched else "portfolio",
            subject_id=(matched or {}).get("entity_id"),
            entity_id=(matched or {}).get("entity_id"),
            security_id=(matched or {}).get("security_id"),
            ticker=(matched or {}).get("ticker"),
            event_id=cls._uuid(cls._first(metadata, "event_id")),
            raw_evidence_id=getattr(evidence, "id", None),
            source_item_id=source_item_id,
            setup_context=cls._clip(setup_context, 700),
            actual_context=cls._clip(actual_context, 700),
            price_reaction=cls._clip(price_reaction, 500),
            value_text=cls._clip(value_text, 500),
            numeric_value=cls._amount(value_text),
            unit="%" if value_text and _PERCENT_RE.search(value_text) else None,
            currency="USD" if value_text and "$" in value_text else None,
            period_label=period_match.group(0) if period_match else None,
            as_of=public_time,
            event_time=cls._date(getattr(evidence, "event_time", None)) or public_time,
            public_time=public_time,
            eligible_action_time=cls._date(
                getattr(evidence, "eligible_action_time", None)
            )
            or public_time,
            direction=direction,
            confidence=cls._text_backfill_confidence(
                families=families,
                matched=matched,
                source=getattr(source, "is_trusted", False),
                setup_context=setup_context,
            ),
            investment_relevance=cls._investment_relevance(
                signal_family, subject_label, setup_context
            ),
            next_test=cls._next_test(signal_family, subject_label),
            source_kind=cls._text(getattr(source, "source_type", None)),
            metadata=metadata_payload,
        )

    @classmethod
    def draft_from_evidence(
        cls,
        evidence: Any,
        source: Any | None = None,
    ) -> MarketSetupSignalDraft | None:
        metadata = dict(getattr(evidence, "metadata_json", None) or {})
        signal_name = cls._text(
            cls._first(
                metadata, "signal_name", "metric_name", "setup_signal", "event_signal"
            )
        )
        setup_context = cls._text(
            cls._first(
                metadata,
                "setup_context",
                "market_setup",
                "expectation_context",
                "hurdle",
            )
        )
        actual_context = cls._text(
            cls._first(
                metadata,
                "actual_context",
                "actual_result",
                "event_result",
                "reported_result",
            )
        )
        investment_relevance = cls._text(
            cls._first(
                metadata,
                "investment_relevance",
                "portfolio_relevance",
                "why_relevant",
                "read_through",
            )
        )
        if not any([signal_name, setup_context, actual_context, investment_relevance]):
            return None

        numeric_raw = cls._first(
            metadata, "numeric_value", "value", "metric_value", "reported_value"
        )
        ticker = cls._upper(
            cls._first(metadata, "ticker", "symbol", "issuer_ticker", "security_ticker")
        )
        event_time = cls._date(
            cls._first(metadata, "event_time", "event_date", "report_date")
            or getattr(evidence, "event_time", None)
        )
        public_time = cls._date(
            cls._first(metadata, "public_time", "published_at", "reported_at")
            or getattr(evidence, "public_time", None)
            or getattr(evidence, "created_at", None)
        )
        signal_family = (
            cls._text(
                cls._first(metadata, "signal_family", "metric_family", "setup_family")
            )
            or "market_setup"
        )
        title = cls._text(getattr(evidence, "title", None))
        name = signal_name or title or signal_family
        return MarketSetupSignalDraft(
            signal_name=name,
            signal_family=signal_family,
            subject_type=cls._text(cls._first(metadata, "subject_type")),
            subject_id=cls._uuid(cls._first(metadata, "subject_id")),
            entity_id=cls._uuid(cls._first(metadata, "entity_id")),
            security_id=cls._uuid(cls._first(metadata, "security_id")),
            ticker=ticker,
            event_id=cls._uuid(cls._first(metadata, "event_id")),
            raw_evidence_id=getattr(evidence, "id", None),
            source_item_id=cls._uuid(cls._first(metadata, "source_item_id")),
            setup_context=setup_context,
            actual_context=actual_context,
            price_reaction=cls._text(
                cls._first(metadata, "price_reaction", "market_reaction")
            ),
            value_text=cls._text(
                cls._first(
                    metadata, "value_text", "metric_text", "reported_text", "value"
                )
            ),
            numeric_value=cls._amount(numeric_raw),
            unit=cls._text(cls._first(metadata, "unit", "metric_unit")),
            currency=cls._upper(cls._first(metadata, "currency", "currency_code")),
            period_label=cls._text(
                cls._first(metadata, "period_label", "period", "fiscal_period")
            ),
            as_of=cls._date(cls._first(metadata, "as_of", "as_of_date", "metric_date")),
            event_time=event_time,
            public_time=public_time,
            eligible_action_time=cls._date(cls._first(metadata, "eligible_action_time"))
            or public_time,
            direction=cls._text(cls._first(metadata, "direction", "bias", "sentiment")),
            confidence=cls._confidence(
                cls._first(metadata, "confidence", "confidence_score")
            ),
            investment_relevance=investment_relevance,
            next_test=cls._text(
                cls._first(
                    metadata, "next_test", "monitoring_test", "what_to_check_next"
                )
            ),
            source_kind=cls._text(cls._first(metadata, "source_kind"))
            or getattr(source, "source_type", None),
            metadata=metadata,
        )

    @classmethod
    def _should_skip_backfill(
        cls, evidence: Any, *, include_conversation_turns: bool
    ) -> bool:
        metadata = dict(getattr(evidence, "metadata_json", None) or {})
        if cls.draft_from_evidence(evidence) is not None:
            return False
        item_type = str(getattr(evidence, "source_item_type", "") or "").strip().lower()
        if item_type in _SKIP_BACKFILL_ITEM_TYPES:
            return not include_conversation_turns
        origin = str(metadata.get("origin") or "").strip().lower()
        if origin in {"agent_chat", "agent_reflection"}:
            return not include_conversation_turns
        return False

    @classmethod
    def _candidate_preview(cls, draft: MarketSetupSignalDraft) -> dict[str, Any]:
        payload = draft.to_context()
        for key in (
            "setup_context",
            "actual_context",
            "investment_relevance",
            "next_test",
            "value_text",
        ):
            payload[key] = cls._clip(payload.get(key), 260)
        metadata = payload.get("metadata")
        if isinstance(metadata, dict):
            payload["metadata"] = {
                key: metadata.get(key)
                for key in (
                    "backfill_extractor",
                    "matched_signal_families",
                    "source_name",
                    "source_type",
                    "portfolio_relevant",
                    "matched_subject",
                )
                if key in metadata
            }
        return payload

    @classmethod
    def _compose_evidence_text(
        cls, evidence: Any, source_item: Any | None = None
    ) -> str:
        metadata = dict(getattr(evidence, "metadata_json", None) or {})
        parts = [
            cls._text(getattr(evidence, "title", None)),
            (
                cls._text(getattr(source_item, "summary", None))
                if source_item is not None
                else None
            ),
            (
                cls._text(getattr(source_item, "extracted_text", None))
                if source_item is not None
                else None
            ),
        ]
        for key in (
            "summary",
            "setup_context",
            "market_setup",
            "expectation_context",
            "actual_context",
            "actual_result",
            "portfolio_relevance",
            "investment_relevance",
            "why_relevant",
            "read_through",
        ):
            value = metadata.get(key)
            if isinstance(value, str):
                parts.append(value)
        return cls._clip(" ".join(part for part in parts if part), 6000) or ""

    @classmethod
    def _signal_families_from_text(cls, text: str) -> list[str]:
        families: list[str] = []
        for family, pattern in _MARKET_SETUP_CLUES:
            if pattern.search(text):
                families.append(family)
        if "fundamental_metric_setup" in families:
            families.remove("fundamental_metric_setup")
            families.insert(0, "fundamental_metric_setup")
        elif (
            "earnings_or_guidance_setup" in families and "expectation_delta" in families
        ):
            families.remove("expectation_delta")
            families.insert(0, "expectation_delta")
        return families

    @classmethod
    def _match_known_subject(
        cls,
        metadata: dict[str, Any],
        text: str,
        known_subjects: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        explicit_ticker = cls._upper(
            cls._first(metadata, "ticker", "symbol", "issuer_ticker", "security_ticker")
        )
        explicit_entity = cls._uuid(cls._first(metadata, "entity_id"))
        explicit_security = cls._uuid(cls._first(metadata, "security_id"))
        for subject in known_subjects:
            if explicit_security and subject.get("security_id") == explicit_security:
                return subject
            if explicit_entity and subject.get("entity_id") == explicit_entity:
                return subject
            if explicit_ticker and subject.get("ticker") == explicit_ticker:
                return subject

        text_upper = f" {text.upper()} "
        text_lower = text.lower()
        best: tuple[int, dict[str, Any]] | None = None
        for subject in known_subjects:
            score = 0
            ticker = subject.get("ticker")
            ticker_text = str(ticker).upper() if ticker else None
            if ticker_text and cls._ticker_mentioned_explicitly(text, ticker_text):
                score += 6
            elif ticker_text and re.search(
                rf"(?<![A-Z0-9]){re.escape(ticker_text)}(?![A-Z0-9])", text_upper
            ):
                score += 2 if cls._ambiguous_symbol(ticker_text) else 5
            name = cls._text(subject.get("name"))
            if (
                name
                and len(name) >= 4
                and not (
                    ticker_text
                    and name.upper() == ticker_text
                    and cls._ambiguous_symbol(ticker_text)
                )
                and cls._contains_phrase(text_lower, name)
            ):
                score += 4
            for alias in subject.get("aliases") or []:
                clean_alias = cls._text(alias)
                if (
                    clean_alias
                    and len(clean_alias) >= 4
                    and not (
                        ticker_text
                        and clean_alias.upper() == ticker_text
                        and cls._ambiguous_symbol(ticker_text)
                    )
                    and cls._contains_phrase(text_lower, clean_alias)
                ):
                    score += 3
            if subject.get("portfolio_relevant"):
                score += 1
            if score >= 4 and (best is None or score > best[0]):
                best = (score, subject)
        return best[1] if best else None

    @staticmethod
    def _ambiguous_symbol(ticker: str) -> bool:
        return bool(re.fullmatch(r"[A-Z]{1,4}", ticker or ""))

    @staticmethod
    def _contains_phrase(text_lower: str, phrase: str) -> bool:
        clean = re.escape(phrase.strip().lower())
        return bool(re.search(rf"(?<![a-z0-9]){clean}(?![a-z0-9])", text_lower))

    @staticmethod
    def _ticker_mentioned_explicitly(text: str, ticker: str) -> bool:
        escaped = re.escape(ticker.upper())
        patterns = [
            rf"\${escaped}(?![A-Z0-9])",
            rf"\({escaped}\)",
            rf"\b(?:NASDAQ|NYSE|AMEX|OTC|TICKER|SYMBOL)\s*[: ]\s*{escaped}\b",
            rf"\b{escaped}\s+(?:stock|shares|equity)\b",
        ]
        upper = text.upper()
        return any(re.search(pattern, upper) for pattern in patterns)

    @classmethod
    def _explicit_portfolio_relevance(cls, metadata: dict[str, Any], text: str) -> bool:
        if any(
            cls._text(cls._first(metadata, key))
            for key in (
                "portfolio_relevance",
                "investment_relevance",
                "why_relevant",
                "read_through",
            )
        ):
            return True
        return bool(
            re.search(
                r"\b(portfolio|holding|position|weight|allocation|exposure)\b",
                text,
                re.I,
            )
        )

    @classmethod
    def _best_sentence(
        cls, text: str, families: list[str], matched: dict[str, Any] | None
    ) -> str | None:
        sentences = cls._sentences(text)
        if not sentences:
            return cls._clip(text, 500)
        subject_tokens = []
        if matched:
            subject_tokens.extend(
                token.lower()
                for token in [
                    matched.get("ticker"),
                    matched.get("name"),
                    *(matched.get("aliases") or []),
                ]
                if cls._text(token)
            )
        best: tuple[int, str] | None = None
        for sentence in sentences:
            score = 0
            lower = sentence.lower()
            for family, pattern in _MARKET_SETUP_CLUES:
                if family in families and pattern.search(sentence):
                    score += 3
            if any(token and token.lower() in lower for token in subject_tokens):
                score += 2
            if _AMOUNT_RE.search(sentence) or _PERCENT_RE.search(sentence):
                score += 1
            if score > 0 and (best is None or score > best[0]):
                best = (score, sentence)
        return best[1] if best else sentences[0]

    @classmethod
    def _sentence_matching(
        cls,
        text: str,
        pattern: re.Pattern[str],
        *,
        exclude: str | None = None,
    ) -> str | None:
        for sentence in cls._sentences(text):
            if exclude and sentence == exclude:
                continue
            if pattern.search(sentence):
                return sentence
        return None

    @classmethod
    def _sentences(cls, text: str) -> list[str]:
        compact = cls._clip(text, 6000) or ""
        parts = re.split(r"(?<=[.!?])\s+|\n+", compact)
        return [part.strip(" -:\t") for part in parts if cls._text(part)]

    @classmethod
    def _signal_name_from_text(
        cls,
        *,
        title: str | None,
        subject_label: str,
        signal_family: str,
        setup_context: str,
    ) -> str:
        if title and 8 <= len(title) <= 140:
            return title
        family_label = signal_family.replace("_", " ")
        context = setup_context.rstrip(".")
        if len(context) <= 90:
            return (
                cls._clip(f"{subject_label}: {context}", 140)
                or f"{subject_label} {family_label}"
            )
        return f"{subject_label} {family_label}"

    @classmethod
    def _subject_label(cls, matched: dict[str, Any] | None) -> str:
        if not matched:
            return "Portfolio"
        ticker = matched.get("ticker")
        name = cls._text(matched.get("name"))
        if ticker and name:
            return f"{ticker} / {name}"
        return str(ticker or name or "Subject")

    @classmethod
    def _direction_from_text(cls, text: str) -> str | None:
        bullish = re.search(
            r"\b(bullish|positive|upside|raised|beat|accelerat(?:e|ed|ion)|strong|tailwind|improv(?:e|ed|ing))\b",
            text,
            re.I,
        )
        bearish = re.search(
            r"\b(bearish|negative|downside|cut|miss|decelerat(?:e|ed|ion)|weak|headwind|risk|fell|drop(?:ped)?)\b",
            text,
            re.I,
        )
        if bullish and bearish:
            return "mixed"
        if bullish:
            return "positive"
        if bearish:
            return "negative"
        return None

    @classmethod
    def _text_backfill_confidence(
        cls,
        *,
        families: list[str],
        matched: dict[str, Any] | None,
        source: bool,
        setup_context: str,
    ) -> float:
        confidence = 0.42
        if matched is not None:
            confidence += 0.08
        if len(families) >= 2:
            confidence += 0.05
        if _AMOUNT_RE.search(setup_context) or _PERCENT_RE.search(setup_context):
            confidence += 0.04
        if source:
            confidence += 0.04
        return max(0.0, min(0.72, confidence))

    @classmethod
    def _investment_relevance(
        cls, signal_family: str, subject_label: str, setup_context: str
    ) -> str:
        mechanisms = {
            "expectation_delta": "the market reaction depends on the gap between the investor bar and the reported outcome",
            "earnings_or_guidance_setup": "earnings and guidance can reset near-term estimates, multiples, and read-throughs",
            "fundamental_metric_setup": "valuation, profitability, and balance-sheet metrics change the return hurdle and downside risk",
            "sentiment_or_positioning": "crowding, flows, and ownership can amplify or fade a fundamental move",
            "competitive_read_through": "competitive demand, supply, and pricing read-throughs can change forward earnings power",
            "price_reaction": "the source records how the market was already discounting the information",
            "event_countdown": "a dated catalyst creates a before/after test for the thesis",
        }
        mechanism = mechanisms.get(
            signal_family,
            "the source describes market setup that should be compared with later outcomes",
        )
        return (
            cls._clip(
                f"For {subject_label}, this matters because {mechanism}. Source setup: {setup_context}",
                700,
            )
            or mechanism
        )

    @classmethod
    def _next_test(cls, signal_family: str, subject_label: str) -> str:
        tests = {
            "expectation_delta": "Compare consensus, whisper expectations, guidance, estimate revisions, and post-event price reaction.",
            "earnings_or_guidance_setup": "Check whether the next report changes revenue, margin, EPS, FCF, or guidance enough to move normalized earnings power.",
            "fundamental_metric_setup": "Update valuation, growth, margins, leverage, liquidity, and returns on capital against peers and history.",
            "sentiment_or_positioning": "Track whether flows, ownership, options pricing, or short interest confirm or contradict the setup.",
            "competitive_read_through": "Map the demand, supply, pricing, and share-gain channel to direct competitors and portfolio holdings.",
            "price_reaction": "Separate one-day reaction from follow-through, estimate changes, and sector-factor movement.",
            "event_countdown": "Set a watcher for the catalyst and score the before/after outcome.",
        }
        return f"{subject_label}: {tests.get(signal_family, 'Compare the source-dated setup with later direct evidence and outcome data.')}"

    @staticmethod
    def _clip(value: Any, limit: int) -> str | None:
        if value is None:
            return None
        text = " ".join(str(value).split())
        if not text:
            return None
        if len(text) <= limit:
            return text
        return text[: max(0, limit - 3)].rstrip() + "..."

    @staticmethod
    def _first(metadata: dict[str, Any], *keys: str) -> Any:
        for key in keys:
            value = metadata.get(key)
            if value not in (None, ""):
                return value
        return None

    @staticmethod
    def _required_text(value: str | None, *, field: str) -> str:
        clean = " ".join((value or "").split())
        if not clean:
            raise ValueError(f"{field}_required")
        return clean

    @staticmethod
    def _text(value: Any) -> str | None:
        if value is None:
            return None
        text = " ".join(str(value).split())
        return text or None

    @classmethod
    def _upper(cls, value: Any) -> str | None:
        text = cls._text(value)
        return text.upper() if text else None

    @staticmethod
    def _uuid(value: Any) -> UUID | None:
        if isinstance(value, UUID):
            return value
        if value in (None, ""):
            return None
        try:
            return UUID(str(value))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _finite(value: Any) -> float | None:
        if value is None or value == "":
            return None
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        return parsed if math.isfinite(parsed) else None

    @classmethod
    def _amount(cls, value: Any) -> float | None:
        if isinstance(value, int | float):
            return cls._finite(value)
        if value is None or value == "":
            return None
        matches = _AMOUNT_RE.findall(str(value).replace(",", ""))
        if not matches:
            return None
        return cls._finite(matches[0].replace("$", ""))

    @staticmethod
    def _date(value: Any) -> datetime | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=UTC)
        if isinstance(value, date):
            return datetime.combine(value, time.min, tzinfo=UTC)
        text = str(value).strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            try:
                parsed = datetime.fromisoformat(text[:10])
            except ValueError:
                return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)

    @classmethod
    def _confidence(cls, value: Any) -> float:
        if isinstance(value, str) and value.strip().endswith("%"):
            parsed_percent = cls._amount(value)
            if parsed_percent is not None:
                return max(0.0, min(1.0, parsed_percent / 100.0))
        parsed = cls._finite(value)
        if parsed is None:
            return 0.5
        if parsed > 1.0:
            parsed = parsed / 100.0
        return max(0.0, min(1.0, parsed))

    @classmethod
    def _query_terms(cls, query: str) -> list[str]:
        tokens = re.findall(r"[A-Za-z0-9][A-Za-z0-9+./-]*", (query or "").lower())
        stopwords = {
            "a",
            "an",
            "and",
            "are",
            "as",
            "at",
            "be",
            "by",
            "for",
            "from",
            "how",
            "i",
            "in",
            "is",
            "it",
            "of",
            "on",
            "or",
            "that",
            "the",
            "this",
            "to",
            "what",
            "when",
            "where",
            "which",
            "with",
            "would",
            "you",
            "your",
        }
        terms: list[str] = []
        seen: set[str] = set()
        for token in tokens:
            if len(token) <= 2 or token in stopwords or token in seen:
                continue
            seen.add(token)
            terms.append(token)
        return terms[:8]
