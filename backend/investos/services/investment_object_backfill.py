from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import delete, desc, exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from investos.core.llm import compact_exception_message
from investos.models.evidence import RawEvidence, SourceItem
from investos.models.fundamental import FundamentalMetric
from investos.models.graph import Edge
from investos.models.market_setup import MarketSetupSignal
from investos.models.source import Source
from investos.services.fundamentals import FundamentalMetricService
from investos.services.market_setup import MarketSetupSignalService
from investos.workers.extraction import ExtractionWorker

BACKFILL_EXTRACTOR_VERSION = 2


class InvestmentObjectBackfillService:
    """Quality-gated reindexing for evidence stored before investment objects existed."""

    def __init__(self, session: AsyncSession):
        self.session = session
        self.extraction = ExtractionWorker(session)
        self.market_setup = MarketSetupSignalService(session)
        self.fundamentals = FundamentalMetricService(session)

    async def run(
        self,
        *,
        apply: bool = False,
        scan_limit: int = 300,
        max_model_calls: int = 10,
        min_confidence: float = 0.75,
        portfolio_only: bool = True,
        include_conversation_turns: bool = False,
        retry_completed: bool = False,
        evidence_id: UUID | None = None,
    ) -> dict[str, Any]:
        clean_scan_limit = max(1, min(int(scan_limit or 300), 2500))
        clean_model_limit = max(1, min(int(max_model_calls or 10), 100))
        clean_confidence = max(0.0, min(1.0, float(min_confidence or 0.0)))
        known_subjects = await self.market_setup._known_subject_catalog()
        statement = self._candidate_statement(
            evidence_id=evidence_id,
            retry_completed=retry_completed,
        )
        rows = (
            await self.session.execute(
                statement.order_by(
                    desc(RawEvidence.public_time),
                    desc(RawEvidence.event_time),
                    desc(RawEvidence.created_at),
                ).limit(clean_scan_limit)
            )
        ).all()

        result: dict[str, Any] = {
            "dry_run": not apply,
            "extractor_version": BACKFILL_EXTRACTOR_VERSION,
            "target_evidence_id": str(evidence_id) if evidence_id else None,
            "scanned": 0,
            "model_calls": 0,
            "candidate_evidence": 0,
            "metric_candidates": 0,
            "setup_candidates": 0,
            "metrics_created": 0,
            "setup_created": 0,
            "exact_duplicates_removed": 0,
            "skipped_already_structured": 0,
            "skipped_completed": 0,
            "skipped_unsafe_origin": 0,
            "skipped_unusable_text": 0,
            "skipped_undated": 0,
            "skipped_unresolved_subject": 0,
            "skipped_non_portfolio": 0,
            "skipped_quality_gate": 0,
            "skipped_existing": 0,
            "errors": 0,
            "examples": [],
        }

        for evidence, source_item, source in rows:
            result["scanned"] += 1
            if result["model_calls"] >= clean_model_limit:
                break
            if self.market_setup._should_skip_backfill(
                evidence,
                include_conversation_turns=include_conversation_turns,
            ):
                result["skipped_unsafe_origin"] += 1
                if apply:
                    self._mark_checkpoint(evidence, status="skipped_unsafe_origin")
                continue
            checkpoint = dict(
                (evidence.metadata_json or {}).get("investment_object_backfill") or {}
            )
            if (
                not retry_completed
                and int(checkpoint.get("extractor_version") or 0)
                >= BACKFILL_EXTRACTOR_VERSION
                and checkpoint.get("status") in {"completed", "no_qualified_objects"}
            ):
                result["skipped_completed"] += 1
                continue
            if await self._has_modern_structured_objects(evidence.id, source_item.id):
                result["skipped_already_structured"] += 1
                if apply:
                    self._mark_checkpoint(evidence, status="already_structured")
                continue

            text = self.market_setup._compose_evidence_text(evidence, source_item)
            if len(text) < 80 or source_item.processing_status not in {
                "processed",
                "processed_with_fallback",
            }:
                result["skipped_unusable_text"] += 1
                if apply:
                    self._mark_checkpoint(evidence, status="skipped_unusable_text")
                continue
            subject = self.market_setup._match_known_subject(
                dict(evidence.metadata_json or {}),
                text,
                known_subjects,
            )
            if subject is None:
                result["skipped_unresolved_subject"] += 1
                if apply:
                    self._mark_checkpoint(evidence, status="skipped_unresolved_subject")
                continue
            if portfolio_only and not subject.get("portfolio_relevant"):
                result["skipped_non_portfolio"] += 1
                if apply:
                    self._mark_checkpoint(evidence, status="skipped_non_portfolio")
                continue

            result["model_calls"] += 1
            try:
                extracted = await self.extraction.extract_investment_objects(
                    evidence.title or "Untitled evidence",
                    text,
                )
            except Exception as exc:
                result["errors"] += 1
                if len(result["examples"]) < 10:
                    result["examples"].append(
                        self._preview_base(evidence, source, subject)
                        | {"status": "error", "error": compact_exception_message(exc)}
                    )
                continue

            metrics = []
            signals = []
            for payload in extracted.get("fundamental_metrics", []):
                if not self._qualified_metric(payload, clean_confidence):
                    result["skipped_quality_gate"] += 1
                elif not self._has_source_date(payload, evidence):
                    result["skipped_undated"] += 1
                else:
                    metrics.append(payload)
            for payload in extracted.get("market_setup_signals", []):
                if not self._qualified_signal(payload, clean_confidence):
                    result["skipped_quality_gate"] += 1
                elif not self._has_source_date(payload, evidence):
                    result["skipped_undated"] += 1
                else:
                    signals.append(payload)
            result["metric_candidates"] += len(metrics)
            result["setup_candidates"] += len(signals)
            if metrics or signals:
                result["candidate_evidence"] += 1

            if len(result["examples"]) < 10:
                result["examples"].append(
                    self._preview_base(evidence, source, subject)
                    | {
                        "status": (
                            "candidate"
                            if metrics or signals
                            else "no_qualified_objects"
                        ),
                        "fundamental_metrics": [
                            self._metric_preview(payload) for payload in metrics
                        ],
                        "market_setup_signals": [
                            self._signal_preview(payload) for payload in signals
                        ],
                    }
                )

            if not apply:
                continue

            created_metrics = await self._persist_metrics(
                evidence=evidence,
                source_item=source_item,
                source=source,
                subject=subject,
                payloads=metrics,
                result=result,
            )
            created_signals = await self._persist_signals(
                evidence=evidence,
                source_item=source_item,
                source=source,
                subject=subject,
                payloads=signals,
                result=result,
            )
            duplicates_removed = await self._remove_exact_signal_duplicates(evidence.id)
            created_signals = max(0, created_signals - duplicates_removed)
            result["metrics_created"] += created_metrics
            result["setup_created"] += created_signals
            result["exact_duplicates_removed"] += duplicates_removed
            self._mark_checkpoint(
                evidence,
                status="completed" if metrics or signals else "no_qualified_objects",
                details={
                    "subject_type": "entity",
                    "subject_id": str(subject["entity_id"]),
                    "security_id": str(subject["security_id"]),
                    "ticker": subject.get("ticker"),
                    "min_confidence": clean_confidence,
                    "metrics_created": created_metrics,
                    "setup_created": created_signals,
                },
            )
            await self.session.commit()

        if apply:
            await self.session.commit()
        if apply and result["metrics_created"]:
            await self.fundamentals.refresh_freshness()
        return result

    @staticmethod
    def _mark_checkpoint(evidence, *, status: str, details: dict | None = None) -> None:
        metadata = dict(evidence.metadata_json or {})
        metadata["investment_object_backfill"] = {
            "extractor_version": BACKFILL_EXTRACTOR_VERSION,
            "completed_at": datetime.now(UTC).isoformat(),
            "status": status,
            **(details or {}),
        }
        evidence.metadata_json = metadata

    @staticmethod
    def _candidate_statement(*, evidence_id: UUID | None, retry_completed: bool):
        statement = (
            select(RawEvidence, SourceItem, Source)
            .join(SourceItem, SourceItem.raw_evidence_id == RawEvidence.id)
            .join(Source, RawEvidence.source_id == Source.id)
            .where(RawEvidence.is_processed.is_(True))
        )
        if evidence_id is not None:
            return statement.where(RawEvidence.id == evidence_id)
        if retry_completed:
            return statement

        checkpoint = RawEvidence.metadata_json["investment_object_backfill"]
        statement = statement.where(
            or_(
                RawEvidence.metadata_json.is_(None),
                checkpoint["extractor_version"].as_integer().is_(None),
                checkpoint["extractor_version"].as_integer()
                < BACKFILL_EXTRACTOR_VERSION,
            )
        )
        metric_exists = exists(
            select(FundamentalMetric.id).where(
                FundamentalMetric.source_kind == "structured_extraction",
                or_(
                    FundamentalMetric.raw_evidence_id == RawEvidence.id,
                    FundamentalMetric.source_item_id == SourceItem.id,
                ),
            )
        )
        signal_exists = exists(
            select(MarketSetupSignal.id).where(
                MarketSetupSignal.source_kind == "structured_extraction",
                or_(
                    MarketSetupSignal.raw_evidence_id == RawEvidence.id,
                    MarketSetupSignal.source_item_id == SourceItem.id,
                ),
            )
        )
        return statement.where(~metric_exists, ~signal_exists)

    async def _has_modern_structured_objects(self, evidence_id, source_item_id) -> bool:
        metric = (
            await self.session.execute(
                select(FundamentalMetric.id)
                .where(
                    or_(
                        FundamentalMetric.raw_evidence_id == evidence_id,
                        FundamentalMetric.source_item_id == source_item_id,
                    ),
                    FundamentalMetric.source_kind == "structured_extraction",
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if metric is not None:
            return True
        signal = (
            await self.session.execute(
                select(MarketSetupSignal.id)
                .where(
                    or_(
                        MarketSetupSignal.raw_evidence_id == evidence_id,
                        MarketSetupSignal.source_item_id == source_item_id,
                    ),
                    MarketSetupSignal.source_kind == "structured_extraction",
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        return signal is not None

    async def _persist_metrics(
        self, *, evidence, source_item, source, subject, payloads, result
    ) -> int:
        created = 0
        for payload in payloads:
            object_subject = await self._resolve_payload_subject(payload, subject)
            family = payload.get(
                "metric_family"
            ) or self.fundamentals._family_from_metric(payload["metric_name"])
            existing = (
                await self.session.execute(
                    select(FundamentalMetric.id)
                    .where(
                        FundamentalMetric.metric_name == payload["metric_name"],
                        FundamentalMetric.period_label == payload.get("period_label"),
                        FundamentalMetric.ticker == object_subject["ticker"],
                        or_(
                            FundamentalMetric.raw_evidence_id == evidence.id,
                            FundamentalMetric.source_item_id == source_item.id,
                        ),
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            if existing is not None:
                result["skipped_existing"] += 1
                continue
            metric = await self.fundamentals.create_metric(
                metric_name=payload["metric_name"],
                metric_family=family,
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
                as_of=self.extraction.dated_value(payload.get("as_of_raw"), evidence),
                event_time=evidence.event_time,
                public_time=evidence.public_time,
                eligible_action_time=evidence.eligible_action_time,
                direction=payload.get("direction"),
                confidence=payload.get("confidence", 0.5),
                investment_relevance=payload.get("investment_relevance"),
                next_test=payload.get("next_test"),
                source_kind="structured_backfill",
                metadata={
                    "extractor_version": BACKFILL_EXTRACTOR_VERSION,
                    "source_name": source.name,
                    "source_type": source.source_type,
                    "historical_reindex": True,
                    "object_subject_name": object_subject["subject_name"],
                    "relationship_to_primary_subject": object_subject["relationship"],
                    "primary_subject_id": str(subject["entity_id"]),
                },
            )
            await self.extraction.link_investment_object_context(
                object_type="fundamental_metric",
                object_id=metric.id,
                object_subject=object_subject,
                primary_subject_type="entity",
                primary_subject_id=subject["entity_id"],
                payload=payload,
            )
            created += 1
        return created

    async def _persist_signals(
        self, *, evidence, source_item, source, subject, payloads, result
    ) -> int:
        created = 0
        for payload in payloads:
            object_subject = await self._resolve_payload_subject(payload, subject)
            family = payload.get("signal_family") or "market_setup"
            existing = (
                await self.session.execute(
                    select(MarketSetupSignal.id)
                    .where(
                        MarketSetupSignal.ticker == object_subject["ticker"],
                        or_(
                            MarketSetupSignal.signal_name == payload["signal_name"],
                            MarketSetupSignal.setup_context
                            == payload.get("setup_context"),
                        ),
                        or_(
                            MarketSetupSignal.raw_evidence_id == evidence.id,
                            MarketSetupSignal.source_item_id == source_item.id,
                        ),
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            if existing is not None:
                result["skipped_existing"] += 1
                continue
            signal = await self.market_setup.create_signal(
                signal_name=payload["signal_name"],
                signal_family=family,
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
                as_of=self.extraction.dated_value(payload.get("as_of_raw"), evidence),
                event_time=evidence.event_time,
                public_time=evidence.public_time,
                eligible_action_time=evidence.eligible_action_time,
                direction=payload.get("direction"),
                confidence=payload.get("confidence", 0.5),
                investment_relevance=payload.get("investment_relevance"),
                next_test=payload.get("next_test"),
                source_kind="structured_backfill",
                metadata={
                    "extractor_version": BACKFILL_EXTRACTOR_VERSION,
                    "source_name": source.name,
                    "source_type": source.source_type,
                    "historical_reindex": True,
                    "object_subject_name": object_subject["subject_name"],
                    "relationship_to_primary_subject": object_subject["relationship"],
                    "primary_subject_id": str(subject["entity_id"]),
                },
            )
            await self.extraction.link_investment_object_context(
                object_type="market_setup_signal",
                object_id=signal.id,
                object_subject=object_subject,
                primary_subject_type="entity",
                primary_subject_id=subject["entity_id"],
                payload=payload,
            )
            created += 1
        return created

    async def _remove_exact_signal_duplicates(self, evidence_id: UUID) -> int:
        rows = (
            (
                await self.session.execute(
                    select(MarketSetupSignal)
                    .where(
                        MarketSetupSignal.raw_evidence_id == evidence_id,
                        MarketSetupSignal.source_kind == "structured_backfill",
                    )
                    .order_by(MarketSetupSignal.created_at, MarketSetupSignal.id)
                )
            )
            .scalars()
            .all()
        )
        seen: set[tuple[str, str]] = set()
        duplicates: list[MarketSetupSignal] = []
        for signal in rows:
            key = self._exact_signal_key(signal)
            if key in seen:
                duplicates.append(signal)
            else:
                seen.add(key)
        if not duplicates:
            return 0
        duplicate_ids = [signal.id for signal in duplicates]
        await self.session.execute(
            delete(Edge).where(
                Edge.source_type == "market_setup_signal",
                Edge.source_id.in_(duplicate_ids),
            )
        )
        for signal in duplicates:
            await self.session.delete(signal)
        await self.session.commit()
        return len(duplicates)

    @staticmethod
    def _exact_signal_key(signal: MarketSetupSignal) -> tuple[str, str]:
        normalize = lambda value: " ".join(str(value or "").casefold().split())
        return (
            normalize(signal.ticker),
            normalize(signal.setup_context),
        )

    async def _resolve_payload_subject(
        self,
        payload: dict[str, Any],
        primary_subject: dict[str, Any],
    ) -> dict[str, object]:
        if payload.get("subject_name") or payload.get("ticker"):
            return await self.extraction.resolve_investment_object_subject(
                payload,
                default_subject_type="entity",
                default_subject_id=primary_subject["entity_id"],
            )
        return {
            "subject_type": "entity",
            "subject_id": primary_subject["entity_id"],
            "entity_id": primary_subject["entity_id"],
            "security_id": primary_subject["security_id"],
            "ticker": primary_subject.get("ticker"),
            "subject_name": primary_subject.get("name"),
            "relationship": payload.get("relationship_to_primary_subject") or "direct",
        }

    @staticmethod
    def _qualified_metric(payload: dict[str, Any], min_confidence: float) -> bool:
        return bool(
            str(payload.get("metric_name") or "").strip()
            and (
                payload.get("numeric_value") is not None
                or str(payload.get("value_text") or "").strip()
            )
            and str(payload.get("investment_relevance") or "").strip()
            and str(payload.get("next_test") or "").strip()
            and float(payload.get("confidence") or 0.0) >= min_confidence
        )

    @staticmethod
    def _qualified_signal(payload: dict[str, Any], min_confidence: float) -> bool:
        return bool(
            str(payload.get("signal_name") or "").strip()
            and str(payload.get("setup_context") or "").strip()
            and str(payload.get("investment_relevance") or "").strip()
            and str(payload.get("next_test") or "").strip()
            and float(payload.get("confidence") or 0.0) >= min_confidence
        )

    @staticmethod
    def _has_source_date(payload: dict[str, Any], evidence: RawEvidence) -> bool:
        if evidence.public_time is not None or evidence.event_time is not None:
            return True
        return (
            ExtractionWorker.dated_value(payload.get("as_of_raw"), evidence) is not None
        )

    @staticmethod
    def _preview_base(evidence, source, subject) -> dict[str, Any]:
        return {
            "evidence_id": str(evidence.id),
            "title": InvestmentObjectBackfillService._clip(evidence.title, 180),
            "source_name": source.name,
            "source_type": source.source_type,
            "public_time": (
                evidence.public_time.isoformat() if evidence.public_time else None
            ),
            "event_time": (
                evidence.event_time.isoformat() if evidence.event_time else None
            ),
            "subject": {
                "entity_id": str(subject["entity_id"]),
                "security_id": str(subject["security_id"]),
                "ticker": subject.get("ticker"),
                "name": subject.get("name"),
                "portfolio_relevant": bool(subject.get("portfolio_relevant")),
            },
        }

    @staticmethod
    def _metric_preview(payload: dict[str, Any]) -> dict[str, Any]:
        return {
            key: (
                InvestmentObjectBackfillService._clip(payload.get(key), 220)
                if key in {"value_text", "investment_relevance", "next_test"}
                else payload.get(key)
            )
            for key in (
                "subject_name",
                "ticker",
                "relationship_to_primary_subject",
                "metric_name",
                "metric_family",
                "value_text",
                "numeric_value",
                "unit",
                "period_label",
                "as_of_raw",
                "confidence",
                "investment_relevance",
                "next_test",
            )
        }

    @staticmethod
    def _signal_preview(payload: dict[str, Any]) -> dict[str, Any]:
        return {
            key: (
                InvestmentObjectBackfillService._clip(payload.get(key), 220)
                if key
                in {
                    "setup_context",
                    "actual_context",
                    "price_reaction",
                    "investment_relevance",
                    "next_test",
                }
                else payload.get(key)
            )
            for key in (
                "subject_name",
                "ticker",
                "relationship_to_primary_subject",
                "signal_name",
                "signal_family",
                "setup_context",
                "actual_context",
                "price_reaction",
                "period_label",
                "as_of_raw",
                "confidence",
                "investment_relevance",
                "next_test",
            )
        }

    @staticmethod
    def _clip(value: Any, limit: int) -> str | None:
        text = " ".join(str(value).split()).strip() if value is not None else ""
        if not text:
            return None
        return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"
