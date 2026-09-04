from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from investos.models.catalog import SourceClaimRecord
from investos.models.evidence import RawEvidence, SourceItem
from investos.models.fundamental import FundamentalMetric
from investos.models.graph import Edge
from investos.models.knowledge import Claim, Event, Fact
from investos.models.market_setup import MarketSetupSignal
from investos.services.knowledge_audit import KnowledgeAuditService


@dataclass(frozen=True)
class EvidenceRelevanceAssessment:
    status: str
    target_supported: bool
    reason: str
    supported_subjects: tuple[str, ...]

    @classmethod
    def from_payload(
        cls, payload: dict[str, Any] | None
    ) -> EvidenceRelevanceAssessment:
        value = payload or {}
        status = str(value.get("status") or "uncertain").strip().lower()
        if status not in {"relevant", "adjacent", "irrelevant", "uncertain"}:
            status = "uncertain"
        reason = str(
            value.get("reason") or "No relevance assessment was returned."
        ).strip()
        subjects = tuple(
            dict.fromkeys(
                str(item).strip()
                for item in value.get("supported_subjects") or []
                if str(item).strip()
            )
        )
        return cls(
            status=status,
            target_supported=bool(value.get("target_supported")),
            reason=reason,
            supported_subjects=subjects,
        )

    @property
    def knowledge_eligible(self) -> bool:
        return self.status in {"relevant", "adjacent"}

    @property
    def processing_status(self) -> str:
        if self.status == "irrelevant":
            return "rejected_irrelevant"
        if self.status == "uncertain":
            return "quarantined_uncertain"
        if self.status == "adjacent":
            return "processed_adjacent_context"
        return "processed"

    def as_metadata(self) -> dict[str, Any]:
        value = asdict(self)
        value["supported_subjects"] = list(self.supported_subjects)
        return value


class EvidenceRelevanceService:
    """Own the boundary between attributable evidence and active knowledge."""

    _KNOWLEDGE_MODELS = {
        "fact": Fact,
        "claim": Claim,
        "event": Event,
    }
    _DERIVED_MODELS = {
        "fundamental_metric": FundamentalMetric,
        "market_setup_signal": MarketSetupSignal,
    }

    def __init__(self, session: AsyncSession):
        self.session = session

    async def evidence_id_for_knowledge_node(
        self, *, node_type: str, node_id: UUID
    ) -> UUID | None:
        clean_type = str(node_type or "").strip().lower()
        model = self._KNOWLEDGE_MODELS.get(clean_type)
        if model is None:
            return None

        source_item_id = None
        node = await self.session.get(model, node_id)
        if node is None:
            return None
        if clean_type in {"fact", "claim"}:
            source_item_id = node.source_item_id
        else:
            source_item_id = (
                await self.session.execute(
                    select(Edge.target_id).where(
                        Edge.source_type == "event",
                        Edge.source_id == node_id,
                        Edge.target_type == "source_item",
                        Edge.relationship_type == "extracted_from",
                    )
                )
            ).scalar_one_or_none()
        if source_item_id is None:
            return None
        return (
            await self.session.execute(
                select(SourceItem.raw_evidence_id).where(
                    SourceItem.id == source_item_id
                )
            )
        ).scalar_one_or_none()

    async def apply_quarantine(
        self,
        *,
        evidence: RawEvidence,
        source_item: SourceItem,
        assessment: EvidenceRelevanceAssessment,
        actor: str = "evidence_relevance",
    ) -> int:
        if assessment.knowledge_eligible:
            return 0

        source_item.processing_status = assessment.processing_status
        metadata = dict(evidence.metadata_json or {})
        metadata["relevance_assessment"] = assessment.as_metadata()
        metadata["knowledge_promotion_status"] = "quarantined"
        evidence.metadata_json = metadata

        audit = KnowledgeAuditService(self.session)
        reason = f"{assessment.status}: {assessment.reason}"
        await audit.record_change(
            node_type="source_item",
            node_id=source_item.id,
            change_type="quarantined",
            reason=reason,
            actor=actor,
            source_type="raw_evidence",
            source_id=evidence.id,
            metadata={"relevance_assessment": assessment.as_metadata()},
        )

        return await self.deprecate_source_derivatives(
            evidence=evidence,
            source_item=source_item,
            assessment=assessment,
            reason=reason,
            actor=actor,
        )

    async def deprecate_source_derivatives(
        self,
        *,
        evidence: RawEvidence,
        source_item: SourceItem,
        assessment: EvidenceRelevanceAssessment,
        reason: str,
        actor: str = "evidence_relevance",
    ) -> int:
        """Retire prior promoted objects while preserving source provenance."""
        audit = KnowledgeAuditService(self.session)

        links = (
            await self.session.execute(
                select(Edge.source_type, Edge.source_id).where(
                    Edge.target_type == "source_item",
                    Edge.target_id == source_item.id,
                    Edge.relationship_type == "extracted_from",
                    Edge.source_type.in_(tuple(self._KNOWLEDGE_MODELS)),
                )
            )
        ).all()
        deprecated = 0
        deprecated_claim_ids: list[UUID] = []
        affected_subjects: set[tuple[str, UUID]] = set()
        for node_type, node_id in links:
            model = self._KNOWLEDGE_MODELS.get(node_type)
            if model is None:
                continue
            node = await self.session.get(model, node_id)
            if node is None or bool(getattr(node, "is_deprecated", False)):
                continue
            node.is_deprecated = True
            node.deprecated_reason = reason
            subject_links = (
                await self.session.execute(
                    select(Edge.target_type, Edge.target_id).where(
                        Edge.source_type == node_type,
                        Edge.source_id == node.id,
                        Edge.target_type.in_(
                            ("entity", "theme", "position", "portfolio")
                        ),
                    )
                )
            ).all()
            affected_subjects.update(
                (subject_type, subject_id) for subject_type, subject_id in subject_links
            )
            if node_type == "claim":
                deprecated_claim_ids.append(node.id)
            await audit.record_change(
                node_type=node_type,
                node_id=node.id,
                change_type="deprecated",
                reason=reason,
                actor=actor,
                source_type="source_item",
                source_id=source_item.id,
                metadata={"relevance_assessment": assessment.as_metadata()},
            )
            deprecated += 1

        for node_type, model in self._DERIVED_MODELS.items():
            nodes = (
                (
                    await self.session.execute(
                        select(model).where(
                            or_(
                                model.source_item_id == source_item.id,
                                model.raw_evidence_id == evidence.id,
                            )
                        )
                    )
                )
                .scalars()
                .all()
            )
            for node in nodes:
                if node.is_deprecated:
                    continue
                node.is_deprecated = True
                node.deprecated_reason = reason
                metadata = dict(node.metadata_json or {})
                metadata["relevance_assessment"] = assessment.as_metadata()
                node.metadata_json = metadata
                await audit.record_change(
                    node_type=node_type,
                    node_id=node.id,
                    change_type="deprecated",
                    reason=reason,
                    actor=actor,
                    source_type="source_item",
                    source_id=source_item.id,
                    metadata={"relevance_assessment": assessment.as_metadata()},
                )
                deprecated += 1

        if deprecated_claim_ids:
            claim_records = (
                (
                    await self.session.execute(
                        select(SourceClaimRecord).where(
                            SourceClaimRecord.claim_id.in_(deprecated_claim_ids),
                            SourceClaimRecord.assessment == "pending",
                        )
                    )
                )
                .scalars()
                .all()
            )
            for record in claim_records:
                record.assessment = "indeterminate"
                record.assessment_time = datetime.now(UTC)
                record.next_assessment_at = None
                record.notes = reason

        if affected_subjects:
            from investos.services.operating_state import OperatingStateService
            from investos.workers.coverage import CoverageWorker

            await self.session.flush()
            operating_state = OperatingStateService(self.session)
            coverage_worker = CoverageWorker(self.session)
            for subject_type, subject_id in sorted(
                affected_subjects, key=lambda item: (item[0], str(item[1]))
            ):
                subject_name = await operating_state.subject_name(
                    subject_id, subject_type
                )
                await coverage_worker.refresh_subject_counts(
                    subject_id=subject_id,
                    subject_type=subject_type,
                    subject_name=subject_name,
                )
        return deprecated
