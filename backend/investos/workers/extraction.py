import re
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from investos.core.dates import parse_explicit_calendar_datetime
from investos.core.llm import call_llm_json, compact_exception_message
from investos.core.prompting import bounded_document_excerpt, compact_text
from investos.core.storage import LocalStorage
from investos.models.catalog import SourceClaimRecord
from investos.models.entity import Entity, Security
from investos.models.evidence import RawEvidence, SourceItem
from investos.models.knowledge import Claim, Event, Fact
from investos.models.profile import Profile
from investos.models.source import Source
from investos.models.theme import Theme
from investos.services.artifact_hygiene import (
    is_topic_subject_name,
    is_unusable_subject,
    normalize_subject_name,
)
from investos.services.corroboration import source_authority
from investos.services.fundamentals import FundamentalMetricService
from investos.services.graph_edge_state import GraphEdgeStateService
from investos.services.knowledge_audit import KnowledgeAuditService
from investos.services.knowledge_time import (
    assess_knowledge_time,
    infer_expired_forecast_time,
)
from investos.services.market_setup import MarketSetupSignalService
from investos.services.operating_loop import OperatingLoopService
from investos.services.source_learning import SourceLearningService
from investos.workers.coverage import CoverageWorker

FUTURE_SIGNAL_RE = re.compile(
    r"\b(will|would|could|should|expect|forecast|project|may|likely)\b", re.I
)
EVENT_SIGNAL_RE = re.compile(
    r"\b(acquired|announced|reported|launched|filed|cut|raised|approved|guidance)\b",
    re.I,
)
CAPITALIZED_PHRASE_RE = re.compile(
    r"\b([A-Z][A-Za-z0-9&.-]+(?:\s+[A-Z][A-Za-z0-9&.-]+){0,3})\b"
)

_UNCLASSIFIED_SUBJECT = "Unclassified Research"


EXTRACTION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "primary_subject": {"type": "string"},
        "subject_type": {"type": "string", "enum": ["entity", "theme"]},
        "entity_type": {
            "type": ["string", "null"],
            "enum": [
                "company",
                "person",
                "organization",
                "index",
                "commodity",
                "currency",
                None,
            ],
        },
        "summary": {"type": "string"},
        "events": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                    "event_type": {"type": "string"},
                    "event_time_raw": {"type": ["string", "null"]},
                    "target_horizon": {
                        "type": "string",
                        "enum": ["tactical", "strategic", "visionary"],
                    },
                    "horizon_reasoning": {"type": "string"},
                },
                "required": [
                    "title",
                    "description",
                    "event_type",
                    "event_time_raw",
                    "target_horizon",
                    "horizon_reasoning",
                ],
            },
        },
        "facts": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "statement": {"type": "string"},
                    "fact_type": {"type": "string"},
                    "confidence": {"type": "number"},
                    "tier": {"type": "string"},
                    "importance": {"type": "string"},
                    "contradiction_role": {"type": "string"},
                    "event_time_raw": {"type": ["string", "null"]},
                    "target_horizon": {
                        "type": "string",
                        "enum": ["tactical", "strategic", "visionary"],
                    },
                    "horizon_reasoning": {"type": "string"},
                    "valid_until_raw": {"type": ["string", "null"]},
                },
                "required": [
                    "statement",
                    "fact_type",
                    "confidence",
                    "tier",
                    "importance",
                    "contradiction_role",
                    "event_time_raw",
                    "target_horizon",
                    "horizon_reasoning",
                    "valid_until_raw",
                ],
            },
        },
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "statement": {"type": "string"},
                    "claim_type": {"type": "string"},
                    "confidence": {"type": "number"},
                    "tier": {"type": "string"},
                    "importance": {"type": "string"},
                    "contradiction_role": {"type": "string"},
                    "sentiment": {"type": ["string", "null"]},
                    "event_time_raw": {"type": ["string", "null"]},
                    "target_horizon": {
                        "type": "string",
                        "enum": ["tactical", "strategic", "visionary"],
                    },
                    "horizon_reasoning": {"type": "string"},
                    "valid_until_raw": {"type": ["string", "null"]},
                },
                "required": [
                    "statement",
                    "claim_type",
                    "confidence",
                    "tier",
                    "importance",
                    "contradiction_role",
                    "sentiment",
                    "event_time_raw",
                    "target_horizon",
                    "horizon_reasoning",
                    "valid_until_raw",
                ],
            },
        },
        "fundamental_metrics": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "subject_name": {"type": ["string", "null"]},
                    "ticker": {"type": ["string", "null"]},
                    "relationship_to_primary_subject": {"type": "string"},
                    "metric_name": {"type": "string"},
                    "metric_family": {"type": "string"},
                    "value_text": {"type": "string"},
                    "numeric_value": {"type": ["number", "null"]},
                    "unit": {"type": ["string", "null"]},
                    "currency": {"type": ["string", "null"]},
                    "period_label": {"type": ["string", "null"]},
                    "as_of_raw": {"type": ["string", "null"]},
                    "direction": {"type": ["string", "null"]},
                    "confidence": {"type": "number"},
                    "investment_relevance": {"type": "string"},
                    "next_test": {"type": "string"},
                },
                "required": [
                    "subject_name",
                    "ticker",
                    "relationship_to_primary_subject",
                    "metric_name",
                    "metric_family",
                    "value_text",
                    "numeric_value",
                    "unit",
                    "currency",
                    "period_label",
                    "as_of_raw",
                    "direction",
                    "confidence",
                    "investment_relevance",
                    "next_test",
                ],
            },
        },
        "market_setup_signals": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "subject_name": {"type": ["string", "null"]},
                    "ticker": {"type": ["string", "null"]},
                    "relationship_to_primary_subject": {"type": "string"},
                    "signal_name": {"type": "string"},
                    "signal_family": {"type": "string"},
                    "setup_context": {"type": "string"},
                    "actual_context": {"type": ["string", "null"]},
                    "price_reaction": {"type": ["string", "null"]},
                    "value_text": {"type": ["string", "null"]},
                    "numeric_value": {"type": ["number", "null"]},
                    "unit": {"type": ["string", "null"]},
                    "currency": {"type": ["string", "null"]},
                    "period_label": {"type": ["string", "null"]},
                    "as_of_raw": {"type": ["string", "null"]},
                    "direction": {"type": ["string", "null"]},
                    "confidence": {"type": "number"},
                    "investment_relevance": {"type": "string"},
                    "next_test": {"type": "string"},
                },
                "required": [
                    "subject_name",
                    "ticker",
                    "relationship_to_primary_subject",
                    "signal_name",
                    "signal_family",
                    "setup_context",
                    "actual_context",
                    "price_reaction",
                    "value_text",
                    "numeric_value",
                    "unit",
                    "currency",
                    "period_label",
                    "as_of_raw",
                    "direction",
                    "confidence",
                    "investment_relevance",
                    "next_test",
                ],
            },
        },
    },
    "required": [
        "primary_subject",
        "subject_type",
        "entity_type",
        "summary",
        "events",
        "facts",
        "claims",
        "fundamental_metrics",
        "market_setup_signals",
    ],
}


INVESTMENT_OBJECT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        key: EXTRACTION_SCHEMA["properties"][key]
        for key in ("fundamental_metrics", "market_setup_signals")
    },
    "required": ["fundamental_metrics", "market_setup_signals"],
}

MAX_FUNDAMENTAL_METRICS = 12
MAX_MARKET_SETUP_SIGNALS = 8
EXTRACTION_SCHEMA["properties"]["fundamental_metrics"][
    "maxItems"
] = MAX_FUNDAMENTAL_METRICS
EXTRACTION_SCHEMA["properties"]["market_setup_signals"][
    "maxItems"
] = MAX_MARKET_SETUP_SIGNALS


class ExtractionWorker:
    def __init__(self, session: AsyncSession):
        self.session = session
        self.storage = LocalStorage()
        self.edge_state = GraphEdgeStateService(session)

    async def process_evidence(self, evidence_id: UUID) -> dict[str, object] | None:
        evidence = (
            await self.session.execute(
                select(RawEvidence).where(RawEvidence.id == evidence_id)
            )
        ).scalar_one_or_none()
        if not evidence or evidence.is_processed or not evidence.raw_content_ref:
            return None
        source_type = (
            await self.session.execute(
                select(Source.source_type).where(Source.id == evidence.source_id)
            )
        ).scalar_one_or_none()
        evidence_directness = source_authority(source_type, evidence.metadata_json)
        if evidence.source_item_type == "conversation_turn" or (
            evidence.metadata_json or {}
        ).get("skip_extraction"):
            evidence.is_processed = True
            await self.session.commit()
            return None
        existing_source_item = (
            await self.session.execute(
                select(SourceItem).where(SourceItem.raw_evidence_id == evidence_id)
            )
        ).scalar_one_or_none()
        if existing_source_item is not None:
            evidence.is_processed = True
            await self.session.commit()
            return None

        try:
            raw_bytes = await self.storage.get_object(evidence.raw_content_ref)
            text_content = raw_bytes.decode("utf-8", errors="ignore")[:12000]
        except FileNotFoundError:
            fallback_summary = (
                evidence.title
                or evidence.url
                or "Stored source content is no longer available locally."
            )
            source_item = await self._upsert_source_item(
                raw_evidence_id=evidence.id,
                source_id=evidence.source_id,
                extracted_text=None,
                summary=fallback_summary,
                processing_status="missing_raw_content",
            )
            evidence.is_processed = True
            metadata = dict(evidence.metadata_json or {})
            metadata["storage_missing"] = True
            evidence.metadata_json = metadata
            await self.session.commit()
            return {
                "subject_id": None,
                "subject_type": None,
                "summary": fallback_summary,
                "degraded": True,
                "missing_raw_content": True,
            }
        extraction_degraded = False
        try:
            extracted = await self._extract_structured_data(
                evidence.title or "Untitled evidence",
                text_content,
            )
        except Exception as exc:
            import logging

            logging.getLogger(__name__).warning(
                "Structured extraction fell back after LLM failure: %s",
                compact_exception_message(exc),
            )
            extracted = self._fallback_structured_data(
                evidence.title or "Untitled evidence",
                text_content,
            )
            extraction_degraded = True

        source_item = await self._upsert_source_item(
            raw_evidence_id=evidence.id,
            source_id=evidence.source_id,
            extracted_text=text_content[:4000],
            summary=extracted["summary"],
            processing_status=(
                "processed_with_fallback" if extraction_degraded else "processed"
            ),
        )

        subject_name = extracted["primary_subject"]
        subject_type = extracted["subject_type"]
        subject_id = await self._get_or_create_subject(
            subject_name,
            subject_type,
            extracted.get("entity_type"),
        )
        await self._ensure_profile(subject_type, subject_id, extracted["summary"])

        await self.edge_state.ensure_edge(
            source_type="raw_evidence",
            source_id=evidence.id,
            target_type="source_item",
            target_id=source_item.id,
            relationship_type="processed_into",
        )
        audit = KnowledgeAuditService(self.session)
        audit_metadata = {
            "raw_evidence_id": str(evidence.id),
            "source_item_id": str(source_item.id),
            "extraction_degraded": extraction_degraded,
        }
        reason = f"Extracted from source evidence: {evidence.title or evidence.url or 'untitled evidence'}"

        for payload in extracted["events"]:
            event_time = self._event_time_from_payload(payload, evidence)
            event = Event(
                title=payload["title"],
                description=payload["description"],
                event_type=payload["event_type"],
                event_time=event_time,
                public_time=evidence.public_time,
                ingest_time=evidence.ingest_time,
                eligible_action_time=event_time,
                target_horizon=payload.get("target_horizon", "strategic"),
                horizon_reasoning=payload.get("horizon_reasoning"),
            )
            self.session.add(event)
            await self.session.flush()
            await audit.record_change(
                node_type="event",
                node_id=event.id,
                change_type="created",
                reason=reason,
                actor="extraction_worker",
                source_type="source_item",
                source_id=source_item.id,
                subject_type=subject_type,
                subject_id=subject_id,
                metadata=audit_metadata,
            )
            await self.edge_state.ensure_edge(
                source_type="event",
                source_id=event.id,
                target_type="source_item",
                target_id=source_item.id,
                relationship_type="extracted_from",
            )
            await self.edge_state.ensure_edge(
                source_type="event",
                source_id=event.id,
                target_type=subject_type,
                target_id=subject_id,
                relationship_type="mentions",
            )

        for payload in extracted["facts"]:
            event_time = self._event_time_from_payload(payload, evidence)
            valid_until = self._valid_until_from_payload(payload, evidence)
            temporal = assess_knowledge_time(
                payload["statement"],
                event_time=event_time,
                public_time=evidence.public_time,
                ingest_time=evidence.ingest_time,
                valid_until=valid_until,
                item_type="fact",
            )
            fact = Fact(
                statement=payload["statement"],
                fact_type=payload["fact_type"],
                confidence=payload["confidence"],
                source_item_id=source_item.id,
                event_time=event_time,
                public_time=evidence.public_time,
                ingest_time=evidence.ingest_time,
                eligible_action_time=evidence.public_time or evidence.ingest_time,
                tier=payload["tier"],
                importance=payload["importance"],
                directness=evidence_directness,
                novelty=temporal.novelty,
                contradiction_role=payload["contradiction_role"],
                # Extraction records what a source said. Promotion is a later,
                # source-independent corroboration decision.
                promotion_eligible=False,
                target_horizon=payload.get("target_horizon", "strategic"),
                horizon_reasoning=payload.get("horizon_reasoning"),
                valid_until=valid_until,
            )
            self.session.add(fact)
            await self.session.flush()
            await audit.record_change(
                node_type="fact",
                node_id=fact.id,
                change_type="created",
                reason=reason,
                actor="extraction_worker",
                source_type="source_item",
                source_id=source_item.id,
                subject_type=subject_type,
                subject_id=subject_id,
                metadata=audit_metadata,
            )
            await self.edge_state.ensure_edge(
                source_type="fact",
                source_id=fact.id,
                target_type="source_item",
                target_id=source_item.id,
                relationship_type="extracted_from",
            )
            await self.edge_state.ensure_edge(
                source_type="fact",
                source_id=fact.id,
                target_type=subject_type,
                target_id=subject_id,
                relationship_type="supports",
            )

        for payload in extracted["claims"]:
            event_time = self._event_time_from_payload(payload, evidence)
            valid_until = self._valid_until_from_payload(payload, evidence)
            temporal = assess_knowledge_time(
                payload["statement"],
                event_time=event_time,
                public_time=evidence.public_time,
                ingest_time=evidence.ingest_time,
                valid_until=valid_until,
                item_type="claim",
            )
            claim = Claim(
                statement=payload["statement"],
                claim_type=payload["claim_type"],
                claimant=evidence.author,
                confidence=payload["confidence"],
                sentiment=payload.get("sentiment"),
                source_item_id=source_item.id,
                event_time=event_time,
                public_time=evidence.public_time,
                ingest_time=evidence.ingest_time,
                eligible_action_time=evidence.public_time or evidence.ingest_time,
                tier=payload["tier"],
                importance=payload["importance"],
                directness=evidence_directness,
                novelty=temporal.novelty,
                contradiction_role=payload["contradiction_role"],
                promotion_eligible=False,
                is_original=bool(evidence.author),
                target_horizon=payload.get("target_horizon", "strategic"),
                horizon_reasoning=payload.get("horizon_reasoning"),
                valid_until=valid_until,
            )
            self.session.add(claim)
            await self.session.flush()
            await audit.record_change(
                node_type="claim",
                node_id=claim.id,
                change_type="created",
                reason=reason,
                actor="extraction_worker",
                source_type="source_item",
                source_id=source_item.id,
                subject_type=subject_type,
                subject_id=subject_id,
                metadata=audit_metadata,
            )
            await self.edge_state.ensure_edge(
                source_type="claim",
                source_id=claim.id,
                target_type="source_item",
                target_id=source_item.id,
                relationship_type="extracted_from",
            )
            await self.edge_state.ensure_edge(
                source_type="claim",
                source_id=claim.id,
                target_type=subject_type,
                target_id=subject_id,
                relationship_type=(
                    "contradicts"
                    if claim.contradiction_role == "contradicts_consensus"
                    else "supports"
                ),
            )
            self.session.add(
                SourceClaimRecord(
                    source_id=source_item.source_id,
                    claim_id=claim.id,
                    claim_time=claim.public_time or claim.ingest_time,
                    assessment="pending",
                    ticker=subject_name if subject_type == "entity" else None,
                )
            )

        # Persist open-ended investment objects from the same structured pass.
        # These are source-dated evidence lanes, not thesis promotions; later
        # reasoning remains free to use, reject, or contradict them.
        for payload in extracted["fundamental_metrics"]:
            object_subject = await self.resolve_investment_object_subject(
                payload,
                default_subject_type=subject_type,
                default_subject_id=subject_id,
            )
            metric = await FundamentalMetricService(self.session).create_metric(
                metric_name=payload["metric_name"],
                metric_family=payload.get("metric_family"),
                subject_type=object_subject["subject_type"],
                subject_id=object_subject["subject_id"],
                entity_id=object_subject["entity_id"],
                security_id=object_subject["security_id"],
                ticker=object_subject["ticker"],
                raw_evidence_id=evidence.id,
                source_item_id=source_item.id,
                value_text=payload.get("value_text"),
                numeric_value=payload.get("numeric_value"),
                unit=payload.get("unit"),
                currency=payload.get("currency"),
                period_label=payload.get("period_label"),
                as_of=self._dated_value(payload.get("as_of_raw"), evidence),
                event_time=evidence.event_time,
                public_time=evidence.public_time,
                eligible_action_time=evidence.eligible_action_time,
                direction=payload.get("direction"),
                confidence=payload.get("confidence", 0.5),
                investment_relevance=payload.get("investment_relevance"),
                next_test=payload.get("next_test"),
                source_kind="structured_extraction",
                metadata={
                    "extraction_worker": True,
                    "object_subject_name": object_subject["subject_name"],
                    "relationship_to_primary_subject": object_subject["relationship"],
                    "primary_subject_type": subject_type,
                    "primary_subject_id": str(subject_id),
                },
            )
            await self.link_investment_object_context(
                object_type="fundamental_metric",
                object_id=metric.id,
                object_subject=object_subject,
                primary_subject_type=subject_type,
                primary_subject_id=subject_id,
                payload=payload,
            )

        for payload in extracted["market_setup_signals"]:
            object_subject = await self.resolve_investment_object_subject(
                payload,
                default_subject_type=subject_type,
                default_subject_id=subject_id,
            )
            signal = await MarketSetupSignalService(self.session).create_signal(
                signal_name=payload["signal_name"],
                signal_family=payload.get("signal_family"),
                subject_type=object_subject["subject_type"],
                subject_id=object_subject["subject_id"],
                entity_id=object_subject["entity_id"],
                security_id=object_subject["security_id"],
                ticker=object_subject["ticker"],
                raw_evidence_id=evidence.id,
                source_item_id=source_item.id,
                setup_context=payload.get("setup_context"),
                actual_context=payload.get("actual_context"),
                price_reaction=payload.get("price_reaction"),
                value_text=payload.get("value_text"),
                numeric_value=payload.get("numeric_value"),
                unit=payload.get("unit"),
                currency=payload.get("currency"),
                period_label=payload.get("period_label"),
                as_of=self._dated_value(payload.get("as_of_raw"), evidence),
                event_time=evidence.event_time,
                public_time=evidence.public_time,
                eligible_action_time=evidence.eligible_action_time,
                direction=payload.get("direction"),
                confidence=payload.get("confidence", 0.5),
                investment_relevance=payload.get("investment_relevance"),
                next_test=payload.get("next_test"),
                source_kind="structured_extraction",
                metadata={
                    "extraction_worker": True,
                    "object_subject_name": object_subject["subject_name"],
                    "relationship_to_primary_subject": object_subject["relationship"],
                    "primary_subject_type": subject_type,
                    "primary_subject_id": str(subject_id),
                },
            )
            await self.link_investment_object_context(
                object_type="market_setup_signal",
                object_id=signal.id,
                object_subject=object_subject,
                primary_subject_type=subject_type,
                primary_subject_id=subject_id,
                payload=payload,
            )

        evidence.is_processed = True
        metadata = dict(evidence.metadata_json or {})
        if extraction_degraded:
            metadata["extraction_degraded"] = True
            evidence.metadata_json = metadata
        await self.session.commit()
        await CoverageWorker(self.session).audit_subject_coverage(
            subject_id=subject_id,
            subject_type=subject_type,
            subject_name=subject_name,
        )
        if evidence.source_item_type not in {"conversation_turn", "manual_note"}:
            await SourceLearningService(self.session).learn_from_source(
                source_id=evidence.source_id,
                subject_name=subject_name,
                subject_type=subject_type,
            )
        loop_result = await OperatingLoopService(self.session).refresh_subject(
            subject_id=subject_id,
            subject_type=subject_type,
            subject_name=subject_name,
            trigger_reason=f"new evidence ingested: {evidence.title or evidence.source_item_type}",
            raw_evidence_id=evidence.id,
        )
        return loop_result

    async def _upsert_source_item(
        self,
        *,
        raw_evidence_id: UUID,
        source_id: UUID,
        extracted_text: str | None,
        summary: str,
        processing_status: str,
    ) -> SourceItem:
        stmt = (
            pg_insert(SourceItem)
            .values(
                raw_evidence_id=raw_evidence_id,
                source_id=source_id,
                extracted_text=extracted_text,
                summary=summary,
                processing_status=processing_status,
            )
            .on_conflict_do_update(
                index_elements=[SourceItem.raw_evidence_id],
                set_={
                    "source_id": source_id,
                    "extracted_text": extracted_text,
                    "summary": summary,
                    "processing_status": processing_status,
                },
            )
            .returning(SourceItem.id)
        )
        source_item_id = (await self.session.execute(stmt)).scalar_one()
        source_item = (
            await self.session.execute(
                select(SourceItem).where(SourceItem.id == source_item_id)
            )
        ).scalar_one()
        return source_item

    async def _extract_structured_data(self, title: str, text_content: str) -> dict:
        truncated = bounded_document_excerpt(
            text_content, head_chars=2600, tail_chars=1000
        )
        fallback_subject = self._detect_subject(title, text_content)
        fallback_type = self._subject_type(fallback_subject, title)
        system_prompt = self._structured_extraction_system_prompt()
        user_prompt = (
            f"Fallback subject guess: {fallback_subject} ({fallback_type})\n"
            f"Evidence title: {title}\n\n"
            f"Evidence text:\n{truncated}"
        )
        extracted = await call_llm_json(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            schema=EXTRACTION_SCHEMA,
        )
        extracted["primary_subject"] = compact_text(
            extracted.get("primary_subject") or fallback_subject, max_chars=120
        )
        extracted["subject_type"] = extracted.get("subject_type") or fallback_type
        extracted["entity_type"] = extracted.get(
            "entity_type"
        ) or self._fallback_entity_type(
            extracted["primary_subject"],
            title,
        )
        extracted["summary"] = compact_text(
            extracted.get("summary") or title, max_chars=900
        )
        extracted["events"] = extracted.get("events", [])[:5]
        extracted["facts"] = extracted.get("facts", [])[:8]
        extracted["claims"] = extracted.get("claims", [])[:6]
        extracted["fundamental_metrics"] = extracted.get("fundamental_metrics", [])[
            :MAX_FUNDAMENTAL_METRICS
        ]
        extracted["market_setup_signals"] = extracted.get("market_setup_signals", [])[
            :MAX_MARKET_SETUP_SIGNALS
        ]
        return extracted

    async def extract_investment_objects(self, title: str, text_content: str) -> dict:
        """Re-extract only source-dated investment objects from saved evidence.

        Historical reindexing deliberately does not recreate facts, claims, events,
        profiles, or conclusions. It reuses the live extraction definitions while
        keeping the write boundary in the backfill service.
        """
        truncated = bounded_document_excerpt(
            text_content, head_chars=2600, tail_chars=1000
        )
        extracted = await call_llm_json(
            system_prompt=(
                self._structured_extraction_system_prompt()
                + " For this historical reindexing pass, return only fundamental_metrics "
                "and market_setup_signals supported directly by the supplied evidence. "
                "Omit an object rather than filling a missing value from general knowledge. "
                f"Return no more than {MAX_FUNDAMENTAL_METRICS} fundamental metrics and "
                f"{MAX_MARKET_SETUP_SIGNALS} market-setup signals."
            ),
            user_prompt=f"Evidence title: {title}\n\nEvidence text:\n{truncated}",
            schema=INVESTMENT_OBJECT_SCHEMA,
        )
        return {
            "fundamental_metrics": extracted.get("fundamental_metrics", [])[
                :MAX_FUNDAMENTAL_METRICS
            ],
            "market_setup_signals": extracted.get("market_setup_signals", [])[
                :MAX_MARKET_SETUP_SIGNALS
            ],
        }

    @staticmethod
    def _structured_extraction_system_prompt() -> str:
        return (
            "Extract atomic market research objects from stored evidence for Prophet. "
            "Preserve the distinction between event, fact, and claim. "
            "Facts should reflect stronger evidence than claims. "
            "When subject_type is entity, also classify entity_type as company, person, organization, index, commodity, or currency. "
            "Use tiers from this set only: hard_fact, strong_derived, credible_interpretation, weak_signal. "
            "Use importance from: critical, high, medium, low. "
            "Use contradiction_role from: supports_consensus, contradicts_consensus, neutral, ambiguous. "
            "Crucially, identify the TIME HORIZON for every object:\n"
            "- 'tactical': Short-term noise, yesterday's news, Q3 guidance, current metrics. Decays quickly.\n"
            "- 'strategic': Multi-year targets, cycle views, major capital plans. Stable for 1-3 years.\n"
            "- 'visionary': Permanent thesis anchors, long-term projections (e.g. AGI 2040, Mars colony). These stay relevant forever regardless of age.\n"
            "If an object has a specific target date (e.g. 'Revenue doubling by 2028'), provide it in valid_until_raw. "
            "For every event, fact, and claim, set event_time_raw to the actual underlying event or assertion date only when "
            "the evidence states or directly implies it (e.g. an earnings report on June 24, 2026); otherwise use null. "
            "Do not use the article publication date or ingestion time as event_time_raw. For facts and claims, set "
            "valid_until_raw only when the evidence states a deadline, forecast target, or period after which the assertion "
            "can be outcome-tested; otherwise use null. Historical quotations and old metrics must retain their original "
            "period and must never be labeled as current merely because they were ingested today. "
            "For earnings, guidance, deals, announcements, or other market events, do not collapse the source into a generic event node. "
            "When present, extract separate facts or claims for the pre-event expectation/hurdle, actual result or deal terms, "
            "price reaction, analyst estimate or guidance revisions, investor positioning, market sentiment, institutional or ownership flows, "
            "peer read-through, and portfolio-relevant transmission mechanism. "
            "When evidence states concrete company metrics, preserve them as source-dated facts or claims with metric name, value, "
            "unit, fiscal period or as-of date, source context, and the stated or implied investment relevance. "
            "Metric families include valuation, profitability, growth, margins, balance-sheet debt/leverage, liquidity, "
            "interest coverage, dilution, capital intensity, estimate revisions, competitive positioning, peer comparisons, "
            "and sector-specific operating KPIs; this is an illustrative ontology, not a closed checklist, so keep any "
            "company- or industry-specific metric that helps explain demand, supply, margins, financing, valuation, or timing. "
            "When the source materially describes a business model, preserve source-backed facts and claims about who pays, the value "
            "delivered, revenue and cost drivers, customer or supplier dependencies, reinvestment needs, and where value is captured. "
            "Likewise preserve material externalities or second-order effects only when the source supports a concrete transmission route "
            "to demand, cost, regulation, reputation, stakeholder behavior, or valuation; do not invent or moralize them. "
            "Also return those concrete measurements in fundamental_metrics so they become first-class source-dated records. "
            "For every metric and market-setup signal, identify the actual measured subject in subject_name and ticker when available, "
            "and describe its open-ended relationship_to_primary_subject. Do not attach a peer, index, sector, or macro measurement "
            "to the primary company merely because it appeared in research about that company. "
            "When a source provides material investment objects for multiple named subjects, cover each named subject before adding "
            "secondary detail for any one subject. Rank objects by source materiality and portfolio relevance rather than source order. "
            "metric_name and metric_family are open-ended labels, not fixed enums. Only include a metric when the source provides "
            "a concrete value or clearly stated measurement; never invent a number. State why it matters and the next falsifiable check. "
            "Return pre-event expectations, investor hurdles, positioning, sentiment, implied moves, ownership/flow context, actual result, "
            "and price reaction in market_setup_signals when present. signal_name and signal_family are also open-ended. Preserve the "
            "difference between what the market expected and what happened; omit the signal when the source does not support it. "
            "Explain your reasoning for the horizon in horizon_reasoning. "
            "DO NOT extract obvious or purely internal system/portfolio telemetry. "
            "Facts and claims must focus purely on external market reality, fundamentals, events, qualitative research, and analysis. "
            "Keep arrays bounded and avoid duplicates."
        )

    @staticmethod
    def _event_time_from_payload(
        payload: dict, evidence: RawEvidence
    ) -> datetime | None:
        reference_time = (
            evidence.public_time or evidence.ingest_time or datetime.now(UTC)
        )
        return parse_explicit_calendar_datetime(
            payload.get("event_time_raw"),
            reference_time=reference_time,
        )

    @staticmethod
    def _valid_until_from_payload(
        payload: dict,
        evidence: RawEvidence,
    ) -> datetime | None:
        reference_time = (
            evidence.public_time or evidence.ingest_time or datetime.now(UTC)
        )
        explicit = parse_explicit_calendar_datetime(
            payload.get("valid_until_raw"),
            reference_time=reference_time,
        )
        if explicit is not None:
            return explicit
        return infer_expired_forecast_time(
            payload.get("statement"),
            reference_time=reference_time,
        )

    def _fallback_structured_data(self, title: str, text_content: str) -> dict:
        fallback_subject = self._detect_subject(title, text_content)
        fallback_type = self._subject_type(fallback_subject, title)
        excerpt = compact_text(text_content.strip(), max_chars=700) or compact_text(
            title, max_chars=300
        )
        summary = (
            excerpt
            if excerpt
            else f"Prophet stored this evidence for {fallback_subject}, but structured extraction could not complete."
        )
        return {
            "primary_subject": compact_text(fallback_subject, max_chars=120),
            "subject_type": fallback_type,
            "entity_type": (
                self._fallback_entity_type(fallback_subject, title)
                if fallback_type == "entity"
                else None
            ),
            "summary": summary,
            "events": [],
            "facts": [],
            "claims": [],
            "fundamental_metrics": [],
            "market_setup_signals": [],
        }

    @staticmethod
    def _dated_value(value: str | None, evidence: RawEvidence) -> datetime | None:
        if not value:
            return evidence.public_time or evidence.event_time
        reference_time = (
            evidence.public_time or evidence.ingest_time or datetime.now(UTC)
        )
        return parse_explicit_calendar_datetime(value, reference_time=reference_time)

    @staticmethod
    def dated_value(value: str | None, evidence: RawEvidence) -> datetime | None:
        return ExtractionWorker._dated_value(value, evidence)

    async def resolve_investment_object_subject(
        self,
        payload: dict,
        *,
        default_subject_type: str,
        default_subject_id: UUID,
    ) -> dict[str, object]:
        subject_name = compact_text(payload.get("subject_name"), max_chars=120) or None
        ticker = compact_text(payload.get("ticker"), max_chars=32)
        ticker = ticker.upper() if ticker else None
        relationship = (
            compact_text(payload.get("relationship_to_primary_subject"), max_chars=120)
            or "direct"
        )
        security = None
        entity = None
        if ticker:
            row = (
                await self.session.execute(
                    select(Security, Entity)
                    .join(Entity, Security.entity_id == Entity.id)
                    .where(Security.ticker.ilike(ticker), Security.is_active.is_(True))
                    .limit(1)
                )
            ).first()
            if row is not None:
                security, entity = row
        if entity is None and subject_name:
            entity = (
                await self.session.execute(
                    select(Entity).where(Entity.name.ilike(subject_name)).limit(1)
                )
            ).scalar_one_or_none()
            if entity is not None:
                security = (
                    await self.session.execute(
                        select(Security)
                        .where(
                            Security.entity_id == entity.id,
                            Security.is_active.is_(True),
                        )
                        .limit(1)
                    )
                ).scalar_one_or_none()
        if entity is not None:
            return {
                "subject_type": "entity",
                "subject_id": entity.id,
                "entity_id": entity.id,
                "security_id": security.id if security is not None else None,
                "ticker": ticker
                or (security.ticker.upper() if security is not None else None),
                "subject_name": entity.name,
                "relationship": relationship,
            }
        if subject_name or ticker:
            return {
                "subject_type": "portfolio",
                "subject_id": None,
                "entity_id": None,
                "security_id": None,
                "ticker": ticker,
                "subject_name": subject_name,
                "relationship": relationship,
            }
        return {
            "subject_type": default_subject_type,
            "subject_id": default_subject_id,
            "entity_id": (
                default_subject_id if default_subject_type == "entity" else None
            ),
            "security_id": None,
            "ticker": None,
            "subject_name": None,
            "relationship": relationship,
        }

    async def link_investment_object_context(
        self,
        *,
        object_type: str,
        object_id: UUID,
        object_subject: dict[str, object],
        primary_subject_type: str,
        primary_subject_id: UUID,
        payload: dict,
    ) -> None:
        if (
            object_subject.get("subject_type") == primary_subject_type
            and object_subject.get("subject_id") == primary_subject_id
        ):
            return
        await self.edge_state.ensure_edge(
            source_type=object_type,
            source_id=object_id,
            target_type=primary_subject_type,
            target_id=primary_subject_id,
            relationship_type="context_for",
            confidence=max(0.0, min(1.0, float(payload.get("confidence") or 0.0))),
            reasoning=payload.get("investment_relevance"),
            properties={
                "origin": "structured_extraction",
                "relationship_to_primary_subject": payload.get(
                    "relationship_to_primary_subject"
                ),
                "object_subject_name": object_subject.get("subject_name"),
                "object_ticker": object_subject.get("ticker"),
            },
        )

    def _detect_subject(self, title: str, text_content: str) -> str:
        title_stub = title.rsplit(".", 1)[0].replace("_", " ").strip()
        if title_stub and title_stub.lower() not in {
            "untitled evidence",
            "manual note",
        }:
            cleaned = normalize_subject_name(title_stub)
            if not is_unusable_subject(cleaned):
                return cleaned[:120]
        match = CAPITALIZED_PHRASE_RE.search(text_content)
        if match:
            cleaned = normalize_subject_name(match.group(1))
            if not is_unusable_subject(cleaned):
                return cleaned
        return _UNCLASSIFIED_SUBJECT

    def _subject_type(self, subject_name: str, title: str) -> str:
        lowered = f"{subject_name} {title}".lower()
        if subject_name == _UNCLASSIFIED_SUBJECT:
            return "theme"
        if is_topic_subject_name(subject_name):
            return "theme"
        if any(
            keyword in lowered
            for keyword in ["theme", "macro", "inflation", "rates", "ai"]
        ):
            return "theme"
        return "entity"

    def _fallback_entity_type(self, subject_name: str, title: str) -> str:
        lowered = f"{subject_name} {title}".lower()
        if any(
            keyword in lowered
            for keyword in [
                "sec",
                "bulletin",
                "staff",
                "department",
                "commission",
                "ministry",
                "agency",
            ]
        ):
            return "organization"
        if any(
            keyword in lowered
            for keyword in [
                "analyst",
                "ceo",
                "founder",
                "chair",
                "john ",
                "tim ",
                "cathie ",
                "tom lee",
                "elon ",
            ]
        ):
            return "person"
        if any(
            keyword in lowered
            for keyword in ["index", "s&p", "nasdaq", "dow jones", "benchmark"]
        ):
            return "index"
        if any(
            keyword in lowered
            for keyword in ["usd", "eur", "currency", "yen", "dollar"]
        ):
            return "currency"
        if any(
            keyword in lowered for keyword in ["gold", "silver", "oil", "commodity"]
        ):
            return "commodity"
        return "company"

    async def _get_or_create_subject(
        self, subject_name: str, subject_type: str, entity_type: str | None = None
    ) -> UUID:
        # Clean internal run-label prefixes before anything touches the entity table.
        cleaned = normalize_subject_name(subject_name)
        if is_unusable_subject(cleaned):
            # Funnel noise into one shared bucket instead of spawning a new junk row.
            cleaned = _UNCLASSIFIED_SUBJECT
            subject_type = "theme"
        subject_name = cleaned

        if subject_type == "theme":
            theme = (
                await self.session.execute(
                    select(Theme).where(Theme.name == subject_name).limit(1)
                )
            ).scalar_one_or_none()
            if theme:
                return theme.id
            theme = Theme(name=subject_name, status="active")
            self.session.add(theme)
            await self.session.flush()
            return theme.id

        entity = (
            await self.session.execute(
                select(Entity).where(Entity.name == subject_name).limit(1)
            )
        ).scalar_one_or_none()
        if entity:
            inferred_type = entity_type or self._fallback_entity_type(
                subject_name, subject_name
            )
            if entity.entity_type == "company" and inferred_type != "company":
                entity.entity_type = inferred_type
            return entity.id
        entity = Entity(
            name=subject_name,
            entity_type=entity_type
            or self._fallback_entity_type(subject_name, subject_name),
        )
        self.session.add(entity)
        await self.session.flush()
        return entity.id

    async def _ensure_profile(
        self, subject_type: str, subject_id: UUID, summary: str
    ) -> None:
        profile = (
            await self.session.execute(
                select(Profile)
                .where(
                    Profile.subject_type == subject_type,
                    Profile.subject_id == subject_id,
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if profile:
            profile.executive_summary = summary
        else:
            profile = Profile(
                subject_type=subject_type,
                subject_id=subject_id,
                executive_summary=summary,
            )
            self.session.add(profile)
        await self.session.flush()
