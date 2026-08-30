from __future__ import annotations

import json
import os
from collections import Counter, defaultdict
from datetime import UTC, datetime

from sqlalchemy import and_, delete, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from investos.config import settings
from investos.core.dates import parse_explicit_calendar_datetime
from investos.core.storage import LocalStorage
from investos.models.conclusion import ConclusionRevision, ConclusionState
from investos.models.coverage import (
    CoverageMap,
    MissingEvidenceClass,
    Resolution,
    UnresolvedQuestion,
)
from investos.models.entity import Entity, Security
from investos.models.evidence import RawEvidence, SourceItem
from investos.models.fundamental import FundamentalMetric
from investos.models.graph import Edge
from investos.models.knowledge import Claim, Event, Fact
from investos.models.market_setup import MarketSetupSignal
from investos.models.portfolio import Position
from investos.models.profile import Profile
from investos.models.source import Source
from investos.models.theme import Theme
from investos.schemas.integrity import (
    IntegrityAuditCounts,
    IntegrityAuditResponse,
    IntegrityDuplicateEdge,
    IntegrityDuplicateSubject,
)
from investos.services.artifact_hygiene import (
    is_artifact_question_text,
    is_unusable_subject,
)
from investos.services.corroboration import near_duplicate_signature
from investos.services.graph_registry import durable_graph_model_map
from investos.services.knowledge_audit import KnowledgeAuditService
from investos.services.knowledge_time import (
    assess_knowledge_time,
    infer_expired_forecast_time,
)

# Substrings that mark a "thesis" that is actually an echoed LLM prompt or a
# deterministic fallback — never a real conclusion. Matching summaries are
# reset to an honest no-view state by repair_state(). Kept in one place so the
# reasoning fallback and the janitor agree on what counts as corrupt.
CORRUPT_THESIS_MARKERS = (
    "currently analyzing",
    "based on available local context",
    "autonomous reflection cycle",
    "operating loop refresh",
    "auto research:",
    "analyze this graph node",
    "deterministic fallback",
)
LINEAGE_SIGNATURE_VERSION = 1
VIRTUAL_GRAPH_NODE_TYPES = frozenset({"portfolio"})


class IntegrityService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.storage = LocalStorage()

    @staticmethod
    def is_corrupt_thesis(summary: str | None) -> bool:
        if not summary:
            return False
        text = summary.casefold()
        return any(marker in text for marker in CORRUPT_THESIS_MARKERS)

    @staticmethod
    def is_artifact_text(text: str | None) -> bool:
        return is_artifact_question_text(text)

    async def audit_state(self) -> IntegrityAuditResponse:
        unknown_edge_node_types = await self._unknown_edge_node_types()
        coverage_dupes = (
            await self.session.execute(
                select(
                    CoverageMap.subject_type,
                    CoverageMap.subject_id,
                    func.count(CoverageMap.id),
                )
                .group_by(CoverageMap.subject_type, CoverageMap.subject_id)
                .having(func.count(CoverageMap.id) > 1)
            )
        ).all()
        conclusion_dupes = (
            await self.session.execute(
                select(
                    ConclusionState.subject_type,
                    ConclusionState.subject_id,
                    func.count(ConclusionState.id),
                )
                .group_by(ConclusionState.subject_type, ConclusionState.subject_id)
                .having(func.count(ConclusionState.id) > 1)
            )
        ).all()
        edge_dupes = (
            await self.session.execute(
                select(
                    Edge.source_type,
                    Edge.source_id,
                    Edge.target_type,
                    Edge.target_id,
                    Edge.relationship_type,
                    func.count(Edge.id),
                )
                .group_by(
                    Edge.source_type,
                    Edge.source_id,
                    Edge.target_type,
                    Edge.target_id,
                    Edge.relationship_type,
                )
                .having(func.count(Edge.id) > 1)
            )
        ).all()
        source_dupes = await self._duplicate_source_groups()

        missing_storage_objects = await self._missing_storage_object_count()
        counts = IntegrityAuditCounts(
            profiles=await self._count(Profile),
            coverage_maps=await self._count(CoverageMap),
            conclusion_states=await self._count(ConclusionState),
            unresolved_questions_open=await self._count(
                UnresolvedQuestion,
                UnresolvedQuestion.status == "open",
            ),
            sources=await self._count(Source),
            raw_evidence=await self._count(RawEvidence),
            source_items=await self._count(SourceItem),
            facts=await self._count(Fact),
            claims=await self._count(Claim),
            events=await self._count(Event),
            edges=await self._count(Edge),
            duplicate_source_groups=len(source_dupes),
            duplicate_edge_groups=len(edge_dupes),
            orphan_edges=await self._orphan_edge_count(),
            unknown_edge_node_types=len(unknown_edge_node_types),
            missing_storage_objects=missing_storage_objects,
        )

        source_rows = [
            IntegrityDuplicateSubject(
                subject_type="source",
                subject_id=f"{name or '[unnamed]'}|{url or ''}",
                count=int(count),
            )
            for name, url, count in source_dupes
        ]
        coverage_rows = [
            IntegrityDuplicateSubject(
                subject_type=str(subject_type),
                subject_id=str(subject_id),
                count=int(count),
            )
            for subject_type, subject_id, count in coverage_dupes
        ]
        conclusion_rows = [
            IntegrityDuplicateSubject(
                subject_type=str(subject_type),
                subject_id=str(subject_id),
                count=int(count),
            )
            for subject_type, subject_id, count in conclusion_dupes
        ]
        edge_rows = [
            IntegrityDuplicateEdge(
                source_type=str(source_type),
                source_id=str(source_id),
                target_type=str(target_type),
                target_id=str(target_id),
                relationship_type=str(relationship_type),
                count=int(count),
            )
            for source_type, source_id, target_type, target_id, relationship_type, count in edge_dupes
        ]

        return IntegrityAuditResponse(
            ok=not source_rows
            and not coverage_rows
            and not conclusion_rows
            and not edge_rows
            and counts.orphan_edges == 0
            and counts.unknown_edge_node_types == 0
            and counts.missing_storage_objects == 0,
            counts=counts,
            duplicate_sources=source_rows,
            duplicate_coverage_subjects=coverage_rows,
            duplicate_conclusion_subjects=conclusion_rows,
            duplicate_edges=edge_rows,
            unknown_edge_node_types=unknown_edge_node_types,
        )

    @staticmethod
    def lineage_signature_metadata(metadata: object, text: object) -> dict | None:
        """Return an idempotent provenance checkpoint for old evidence text."""
        payload = dict(metadata) if isinstance(metadata, dict) else {}
        try:
            version = int(payload.get("lineage_signature_version") or 0)
        except (TypeError, ValueError):
            version = 0
        if version >= LINEAGE_SIGNATURE_VERSION and payload.get(
            "lineage_signature_status"
        ) in {"ready", "insufficient_text"}:
            return None
        signature, token_count = near_duplicate_signature(text)
        payload.update(
            {
                "near_duplicate_signature": signature,
                "signature_token_count": token_count,
                "lineage_signature_status": (
                    "ready" if signature else "insufficient_text"
                ),
                "lineage_signature_version": LINEAGE_SIGNATURE_VERSION,
            }
        )
        return payload

    async def backfill_lineage_signatures(
        self,
        *,
        limit: int = 100,
        dry_run: bool = False,
    ) -> dict:
        """Enrich a bounded batch of legacy evidence without invoking a model."""
        clean_limit = max(1, min(int(limit or 100), 2500))
        checkpoint = RawEvidence.metadata_json["lineage_signature_status"]
        rows = (
            await self.session.execute(
                select(RawEvidence, SourceItem)
                .join(SourceItem, SourceItem.raw_evidence_id == RawEvidence.id)
                .where(
                    or_(
                        RawEvidence.metadata_json.is_(None),
                        checkpoint.as_string().is_(None),
                    )
                )
                .order_by(RawEvidence.created_at, RawEvidence.id)
                .limit(clean_limit)
            )
        ).all()
        result = {
            "dry_run": dry_run,
            "signature_version": LINEAGE_SIGNATURE_VERSION,
            "scanned": len(rows),
            "enriched": 0,
            "ready": 0,
            "insufficient_text": 0,
        }
        audit = KnowledgeAuditService(self.session)
        for evidence, source_item in rows:
            metadata = self.lineage_signature_metadata(
                evidence.metadata_json,
                source_item.extracted_text or source_item.summary or "",
            )
            if metadata is None:
                continue
            result["enriched"] += 1
            result[str(metadata["lineage_signature_status"])] += 1
            if dry_run:
                continue
            evidence.metadata_json = metadata
            await audit.record_change(
                node_type="raw_evidence",
                node_id=evidence.id,
                change_type="provenance_enriched",
                reason=(
                    "Deterministic lineage backfill added a versioned near-duplicate "
                    "signature checkpoint for independent-source corroboration."
                ),
                actor="integrity_maintenance",
                source_type="source",
                source_id=evidence.source_id,
                metadata={
                    "lineage_signature_version": LINEAGE_SIGNATURE_VERSION,
                    "lineage_signature_status": metadata["lineage_signature_status"],
                    "signature_token_count": metadata["signature_token_count"],
                    "source_item_id": str(source_item.id),
                },
            )
        if not dry_run and result["enriched"]:
            await self.session.commit()
        return result

    async def _count(self, model, *filters) -> int:
        stmt = select(func.count()).select_from(model)
        for item in filters:
            stmt = stmt.where(item)
        return int((await self.session.execute(stmt)).scalar_one() or 0)

    async def _duplicate_source_groups(self) -> list[tuple[str, str, int]]:
        rows = (await self.session.execute(select(Source.name, Source.url))).all()
        counts = Counter(
            (
                (name or "").strip().casefold(),
                (url or "").strip().rstrip("/").casefold(),
            )
            for name, url in rows
        )
        return [
            (name, url, count) for (name, url), count in counts.items() if count > 1
        ]

    async def _orphan_edge_count(self) -> int:
        return len(await self._orphan_edge_ids())

    async def _orphan_edge_ids(self) -> list:
        """Edge ids whose source or target node no longer exists.

        Batched: collect every referenced (type, id), resolve which ids of each
        type actually exist with one query per type, then test edges in memory.
        Avoids ~2 queries per edge across tens of thousands of edges.
        """
        edges = (
            await self.session.execute(
                select(
                    Edge.id,
                    Edge.source_type,
                    Edge.source_id,
                    Edge.target_type,
                    Edge.target_id,
                )
            )
        ).all()
        needed: dict[str, set] = defaultdict(set)
        for _id, stype, sid, ttype, tid in edges:
            needed[stype].add(sid)
            needed[ttype].add(tid)

        existing: dict[str, set | None] = {}
        for node_type, ids in needed.items():
            model = self._model_for(node_type)
            if model is None:
                # Unknown types are reported separately, never presumed absent.
                # This prevents a new durable graph type from being erased
                # before the integrity registry learns how to validate it.
                existing[node_type] = None
                continue
            rows = (
                (
                    await self.session.execute(
                        select(model.id).where(model.id.in_(list(ids)))
                    )
                )
                .scalars()
                .all()
            )
            existing[node_type] = set(rows)

        def present(node_type: str, node_id) -> bool:
            ex = existing.get(node_type)
            if ex is None:
                return True
            return node_id in ex

        return [
            _id
            for _id, stype, sid, ttype, tid in edges
            if not present(stype, sid) or not present(ttype, tid)
        ]

    async def _unknown_edge_node_types(self) -> list[str]:
        rows = (
            await self.session.execute(select(Edge.source_type, Edge.target_type))
        ).all()
        node_types = {
            str(node_type)
            for source_type, target_type in rows
            for node_type in (source_type, target_type)
            if node_type
        }
        return sorted(
            node_type
            for node_type in node_types
            if node_type not in VIRTUAL_GRAPH_NODE_TYPES
            and self._model_for(node_type) is None
        )

    async def _missing_storage_object_count(self) -> int:
        evidence_rows = (
            await self.session.execute(
                select(RawEvidence.id, RawEvidence.raw_content_ref).where(
                    RawEvidence.raw_content_ref.is_not(None)
                )
            )
        ).all()
        missing = 0
        for _, raw_content_ref in evidence_rows:
            if not await self.storage.object_exists(raw_content_ref):
                missing += 1
        return missing

    @staticmethod
    def _model_for(node_type: str):
        """Map a registered durable graph node type to its ORM model."""
        return durable_graph_model_map().get(node_type)

    async def _node_exists(self, node_type: str, node_id) -> bool:
        model = self._model_for(node_type)
        if model is None:
            return node_type in VIRTUAL_GRAPH_NODE_TYPES
        stmt = select(model.id).where(model.id == node_id).limit(1)
        return (await self.session.execute(stmt)).scalar_one_or_none() is not None

    async def restore_investment_object_edges(
        self,
        *,
        limit: int = 1000,
        dry_run: bool = False,
    ) -> dict:
        """Restore missing graph edges from canonical metric and signal rows."""
        clean_limit = max(1, min(int(limit or 1000), 5000))
        missing_metric_edge = ~exists(
            select(Edge.id).where(
                Edge.source_type == "fundamental_metric",
                Edge.source_id == FundamentalMetric.id,
            )
        )
        metrics = (
            (
                await self.session.execute(
                    select(FundamentalMetric)
                    .where(missing_metric_edge)
                    .order_by(FundamentalMetric.created_at, FundamentalMetric.id)
                    .limit(clean_limit)
                )
            )
            .scalars()
            .all()
        )
        remaining = max(0, clean_limit - len(metrics))
        signals: list[MarketSetupSignal] = []
        if remaining:
            missing_signal_edge = ~exists(
                select(Edge.id).where(
                    Edge.source_type == "market_setup_signal",
                    Edge.source_id == MarketSetupSignal.id,
                )
            )
            signals = (
                (
                    await self.session.execute(
                        select(MarketSetupSignal)
                        .where(missing_signal_edge)
                        .order_by(
                            MarketSetupSignal.created_at,
                            MarketSetupSignal.id,
                        )
                        .limit(remaining)
                    )
                )
                .scalars()
                .all()
            )

        result = {
            "dry_run": dry_run,
            "metrics_scanned": len(metrics),
            "signals_scanned": len(signals),
            "objects_repaired": 0,
            "edges_created": 0,
        }
        if dry_run:
            result["objects_repaired"] = len(metrics) + len(signals)
            return result

        from investos.services.fundamentals import FundamentalMetricService
        from investos.services.market_setup import MarketSetupSignalService

        metric_service = FundamentalMetricService(self.session)
        signal_service = MarketSetupSignalService(self.session)
        for metric in metrics:
            created = await metric_service._attach_graph_edges(metric)
            result["objects_repaired"] += int(created > 0)
            result["edges_created"] += created
        for signal in signals:
            created = await signal_service._attach_graph_edges(signal)
            result["objects_repaired"] += int(created > 0)
            result["edges_created"] += created
        if result["edges_created"]:
            await self.session.commit()
        return result

    # ----------------------------------------------------------------- repair

    async def repair_state(self, *, dry_run: bool = False) -> dict:
        """Heal the corruption classes the audit detects, and write an audit.

        Safe, non-deletes-of-real-data by construction:
          * orphan edges point to nodes that no longer exist (dead links)
          * duplicate edges/canonical rows keep the surviving copy per group
          * corrupt theses are *reset* to an honest no-view, never deleted
        Portfolio/security/thesis rows are never touched.
        """
        orphan_edge_ids = await self._orphan_edge_ids()
        duplicate_edge_ids = await self._duplicate_edge_ids()
        corrupt_conclusion_ids = await self._corrupt_conclusion_ids()
        artifact_question_ids = await self._artifact_unresolved_question_ids()
        dup_coverage_ids = await self._duplicate_canonical_losers(
            CoverageMap, CoverageMap.last_computed_at
        )
        dup_conclusion_ids = await self._duplicate_canonical_losers(
            ConclusionState, ConclusionState.last_updated_at
        )
        stale_holding_ids = (
            (
                await self.session.execute(
                    select(Position.id).where(
                        Position.quantity == 0, Position.list_type == "holding"
                    )
                )
            )
            .scalars()
            .all()
        )
        # Negative share counts can only come from a replay bug (oversell against
        # missing lots). Never silently rewrite money state — surface for review.
        negative_qty_count = await self._count(Position, Position.quantity < 0)
        temporal_repairs = await self._repair_legacy_knowledge_time(
            limit=500,
            dry_run=dry_run,
        )

        actions = {
            "orphan_edges_removed": len(orphan_edge_ids),
            "duplicate_edges_removed": len(duplicate_edge_ids),
            "corrupt_theses_reset": len(corrupt_conclusion_ids),
            "artifact_questions_obsoleted": len(artifact_question_ids),
            "duplicate_coverage_maps_removed": len(dup_coverage_ids),
            "duplicate_conclusions_removed": len(dup_conclusion_ids),
            "stale_zero_holdings_closed": len(stale_holding_ids),
            "legacy_knowledge_times_repaired": temporal_repairs["repaired"],
        }

        if not dry_run:
            await self._record_edge_repairs(
                orphan_edge_ids,
                change_type="deleted_orphan",
                reason="Integrity repair removed an edge whose source or target node no longer exists.",
            )
            duplicate_only_edge_ids = [
                edge_id
                for edge_id in duplicate_edge_ids
                if edge_id not in set(orphan_edge_ids)
            ]
            await self._record_edge_repairs(
                duplicate_only_edge_ids,
                change_type="deleted_duplicate",
                reason="Integrity repair removed a duplicate explicit graph edge.",
            )
            await self._record_repair_rows(
                ConclusionState,
                corrupt_conclusion_ids,
                node_type="conclusion",
                change_type="reset_corrupt",
                reason="Integrity repair reset a corrupt fallback/autonomous thesis to an honest no-view state.",
                metadata_fields=(
                    "subject_type",
                    "subject_id",
                    "current_stance",
                    "current_thesis_summary",
                ),
            )
            await self._record_repair_rows(
                UnresolvedQuestion,
                artifact_question_ids,
                node_type="unresolved_question",
                change_type="obsoleted_artifact",
                reason="Integrity repair obsoleted an internal artifact question that should not drive research.",
                metadata_fields=(
                    "coverage_map_id",
                    "question_text",
                    "urgency",
                    "status",
                ),
            )
            await self._record_repair_rows(
                CoverageMap,
                dup_coverage_ids,
                node_type="coverage_map",
                change_type="deleted_duplicate",
                reason="Integrity repair removed a duplicate canonical coverage map.",
                metadata_fields=(
                    "subject_type",
                    "subject_id",
                    "overall_coverage_score",
                ),
            )
            await self._record_repair_rows(
                ConclusionState,
                dup_conclusion_ids,
                node_type="conclusion",
                change_type="deleted_duplicate",
                reason="Integrity repair removed a duplicate canonical conclusion state.",
                metadata_fields=(
                    "subject_type",
                    "subject_id",
                    "current_stance",
                    "current_thesis_summary",
                ),
            )
            await self._record_repair_rows(
                Position,
                stale_holding_ids,
                node_type="position",
                change_type="closed_stale_zero_holding",
                reason="Integrity repair moved a zero-quantity holding to the closed list.",
                metadata_fields=(
                    "security_id",
                    "quantity",
                    "market_value",
                    "list_type",
                ),
            )
            stale_edge_ids = list({*orphan_edge_ids, *duplicate_edge_ids})
            if stale_edge_ids:
                await self.session.execute(
                    delete(Edge).where(Edge.id.in_(stale_edge_ids))
                )
            if corrupt_conclusion_ids:
                await self.session.execute(
                    ConclusionState.__table__.update()
                    .where(ConclusionState.id.in_(corrupt_conclusion_ids))
                    .values(
                        current_stance="no_view",
                        current_thesis_summary="",
                        key_supporting_evidence_ids=None,
                        key_contradicting_evidence_ids=None,
                    )
                )
            if artifact_question_ids:
                await self.session.execute(
                    UnresolvedQuestion.__table__.update()
                    .where(UnresolvedQuestion.id.in_(artifact_question_ids))
                    .values(status="obsolete")
                )
            if dup_coverage_ids:
                await self._cascade_delete_coverage(dup_coverage_ids)
            if dup_conclusion_ids:
                await self._cascade_delete_conclusions(dup_conclusion_ids)
            if stale_holding_ids:
                await self.session.execute(
                    Position.__table__.update()
                    .where(Position.id.in_(stale_holding_ids))
                    .values(list_type="closed")
                )
            await self.session.commit()

        summary = {
            "ran_at": datetime.now(UTC).isoformat(),
            "dry_run": dry_run,
            "actions": actions,
            "total_repaired": sum(actions.values()),
            "flags": {"negative_quantity_positions": negative_qty_count},
            "samples": {
                "orphan_edge_ids": [str(i) for i in orphan_edge_ids[:25]],
                "corrupt_conclusion_ids": [str(i) for i in corrupt_conclusion_ids[:25]],
                "artifact_question_ids": [str(i) for i in artifact_question_ids[:25]],
                "knowledge_time_node_ids": temporal_repairs["node_ids"][:25],
            },
        }
        self._write_repair_audit(summary)
        return summary

    async def _repair_legacy_knowledge_time(
        self,
        *,
        limit: int,
        dry_run: bool,
    ) -> dict:
        """Remove the former ingest-time-as-event-time fallback from facts and claims."""

        clean_limit = max(1, min(int(limit or 1), 2500))
        rows: list[tuple[str, Fact | Claim]] = []
        per_type_limit = max(1, clean_limit // 2)
        for node_type, model in (("fact", Fact), ("claim", Claim)):
            remaining = clean_limit - len(rows)
            if remaining <= 0:
                break
            candidates = (
                (
                    await self.session.execute(
                        select(model)
                        .where(
                            model.event_time.is_not(None),
                            or_(
                                model.event_time == model.ingest_time,
                                and_(
                                    model.public_time.is_not(None),
                                    model.event_time == model.public_time,
                                ),
                            ),
                        )
                        .order_by(model.created_at.desc(), model.id)
                        .limit(min(remaining, per_type_limit))
                    )
                )
                .scalars()
                .all()
            )
            rows.extend((node_type, row) for row in candidates)

        result = {
            "scanned": len(rows),
            "repaired": len(rows),
            "node_ids": [str(row.id) for _, row in rows],
        }
        if dry_run or not rows:
            return result

        for node_type, row in rows:
            text = row.statement
            reference_time = row.public_time or row.ingest_time or datetime.now(UTC)
            explicit_event_time = parse_explicit_calendar_datetime(
                text,
                reference_time=reference_time,
            )
            valid_until = row.valid_until
            if node_type == "claim" and valid_until is None:
                valid_until = infer_expired_forecast_time(
                    text,
                    reference_time=reference_time,
                )
            temporal = assess_knowledge_time(
                text,
                event_time=explicit_event_time,
                public_time=row.public_time,
                ingest_time=row.ingest_time,
                valid_until=valid_until,
                item_type=node_type,
            )
            row.event_time = explicit_event_time
            row.valid_until = valid_until
            row.novelty = temporal.novelty
        return result

    async def _duplicate_edge_ids(self) -> list:
        rows = (
            await self.session.execute(
                select(
                    Edge.id,
                    Edge.source_type,
                    Edge.source_id,
                    Edge.target_type,
                    Edge.target_id,
                    Edge.relationship_type,
                )
            )
        ).all()
        groups: dict[tuple, list] = defaultdict(list)
        for eid, st, sid, tt, tid, rel in rows:
            groups[(st, str(sid), tt, str(tid), rel)].append(eid)
        losers: list = []
        for ids in groups.values():
            if len(ids) > 1:
                keep, *rest = sorted(ids, key=str)
                losers.extend(rest)
        return losers

    async def _corrupt_conclusion_ids(self) -> list:
        rows = (
            await self.session.execute(
                select(ConclusionState.id, ConclusionState.current_thesis_summary)
            )
        ).all()
        return [cid for cid, summary in rows if self.is_corrupt_thesis(summary)]

    async def _artifact_unresolved_question_ids(self) -> list:
        rows = (
            await self.session.execute(
                select(
                    UnresolvedQuestion.id,
                    UnresolvedQuestion.question_text,
                    CoverageMap.subject_type,
                    CoverageMap.subject_id,
                )
                .join(CoverageMap, UnresolvedQuestion.coverage_map_id == CoverageMap.id)
                .where(UnresolvedQuestion.status == "open")
            )
        ).all()
        ids: list = []
        label_cache: dict[tuple[str, str], str] = {}
        for qid, question_text, subject_type, subject_id in rows:
            if self.is_artifact_text(question_text):
                ids.append(qid)
                continue
            key = (str(subject_type), str(subject_id))
            if key not in label_cache:
                label = ""
                if subject_type == "entity":
                    entity = await self.session.get(Entity, subject_id)
                    label = "" if entity is None else entity.name
                elif subject_type == "theme":
                    theme = await self.session.get(Theme, subject_id)
                    label = "" if theme is None else theme.name
                label_cache[key] = label
            label = label_cache[key]
            if self.is_artifact_text(label) or is_unusable_subject(label):
                ids.append(qid)
        return ids

    async def _duplicate_canonical_losers(self, model, recency_col) -> list:
        """Per (subject_type, subject_id), keep the most recent row; return the rest."""
        rows = (
            await self.session.execute(
                select(model.id, model.subject_type, model.subject_id, recency_col)
            )
        ).all()
        groups: dict[tuple, list] = defaultdict(list)
        for rid, stype, sid, ts in rows:
            groups[(stype, str(sid))].append((rid, ts))
        losers: list = []
        for items in groups.values():
            if len(items) > 1:
                ordered = sorted(
                    items, key=lambda x: (x[1] is not None, x[1]), reverse=True
                )
                losers.extend(rid for rid, _ in ordered[1:])
        return losers

    async def _record_edge_repairs(
        self, edge_ids: list, *, change_type: str, reason: str
    ) -> None:
        if not edge_ids:
            return
        rows = (
            (await self.session.execute(select(Edge).where(Edge.id.in_(edge_ids))))
            .scalars()
            .all()
        )
        audit = KnowledgeAuditService(self.session)
        for edge in rows:
            await audit.record_change(
                node_type="edge",
                node_id=edge.id,
                change_type=change_type,
                reason=reason,
                actor="integrity_repair",
                subject_type=edge.source_type,
                subject_id=edge.source_id,
                metadata={
                    "source_type": edge.source_type,
                    "source_id": str(edge.source_id),
                    "target_type": edge.target_type,
                    "target_id": str(edge.target_id),
                    "relationship_type": edge.relationship_type,
                    "confidence": float(edge.confidence or 0.0),
                    "properties": edge.properties_json or {},
                },
            )

    async def _record_repair_rows(
        self,
        model,
        row_ids: list,
        *,
        node_type: str,
        change_type: str,
        reason: str,
        metadata_fields: tuple[str, ...],
    ) -> None:
        if not row_ids:
            return
        rows = (
            (await self.session.execute(select(model).where(model.id.in_(row_ids))))
            .scalars()
            .all()
        )
        audit = KnowledgeAuditService(self.session)
        for row in rows:
            metadata = {}
            for field in metadata_fields:
                value = getattr(row, field, None)
                metadata[field] = str(value) if value is not None else None
            label = (
                getattr(row, "question_text", None)
                or getattr(row, "current_thesis_summary", None)
                or getattr(row, "subject_type", None)
                or node_type
            )
            metadata["label"] = str(label)[:500] if label is not None else node_type
            await audit.record_change(
                node_type=node_type,
                node_id=row.id,
                change_type=change_type,
                reason=reason,
                actor="integrity_repair",
                subject_type=getattr(row, "subject_type", None),
                subject_id=getattr(row, "subject_id", None),
                metadata=metadata,
            )

    async def _cascade_delete_coverage(self, cov_ids: list) -> None:
        q_ids = (
            (
                await self.session.execute(
                    select(UnresolvedQuestion.id).where(
                        UnresolvedQuestion.coverage_map_id.in_(cov_ids)
                    )
                )
            )
            .scalars()
            .all()
        )
        c_ids = (
            (
                await self.session.execute(
                    select(MissingEvidenceClass.id).where(
                        MissingEvidenceClass.coverage_map_id.in_(cov_ids)
                    )
                )
            )
            .scalars()
            .all()
        )
        if q_ids or c_ids:
            await self.session.execute(
                delete(Resolution).where(
                    or_(
                        Resolution.unresolved_question_id.in_(q_ids or [None]),
                        Resolution.missing_evidence_class_id.in_(c_ids or [None]),
                    )
                )
            )
        await self.session.execute(
            delete(UnresolvedQuestion).where(
                UnresolvedQuestion.coverage_map_id.in_(cov_ids)
            )
        )
        await self.session.execute(
            delete(MissingEvidenceClass).where(
                MissingEvidenceClass.coverage_map_id.in_(cov_ids)
            )
        )
        await self.session.execute(
            delete(CoverageMap).where(CoverageMap.id.in_(cov_ids))
        )

    async def _cascade_delete_conclusions(self, conc_ids: list) -> None:
        await self.session.execute(
            delete(ConclusionRevision).where(
                ConclusionRevision.conclusion_state_id.in_(conc_ids)
            )
        )
        await self.session.execute(
            delete(ConclusionState).where(ConclusionState.id.in_(conc_ids))
        )

    @staticmethod
    def _repair_audit_dir() -> str:
        return os.path.join(
            os.path.dirname(settings.STORAGE_DIR), "maintenance", "integrity_repair"
        )

    def _write_repair_audit(self, summary: dict) -> None:
        base = self._repair_audit_dir()
        os.makedirs(base, exist_ok=True)
        ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        with open(os.path.join(base, f"{ts}.json"), "w") as f:
            json.dump(summary, f, indent=2)
        with open(os.path.join(base, "index.log"), "a") as f:
            a = summary["actions"]
            f.write(
                f"{summary['ran_at']} dry_run={summary['dry_run']} "
                f"orphan_edges={a['orphan_edges_removed']} dup_edges={a['duplicate_edges_removed']} "
                f"theses_reset={a['corrupt_theses_reset']} "
                f"artifact_questions={a['artifact_questions_obsoleted']} "
                f"dup_coverage={a['duplicate_coverage_maps_removed']} "
                f"dup_conclusions={a['duplicate_conclusions_removed']} "
                f"stale_holdings={a['stale_zero_holdings_closed']}\n"
            )

    def latest_repair_audit(self) -> dict | None:
        base = self._repair_audit_dir()
        if not os.path.isdir(base):
            return None
        files = sorted(f for f in os.listdir(base) if f.endswith(".json"))
        if not files:
            return None
        with open(os.path.join(base, files[-1])) as f:
            return json.load(f)
