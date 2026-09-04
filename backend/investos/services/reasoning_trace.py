from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from investos.models.evidence import RawEvidence, SourceItem
from investos.models.graph import Edge
from investos.models.knowledge import Claim, Event, Fact
from investos.models.reasoning import CritiqueRun, EvidencePacket, ReasoningRun
from investos.models.source import Source
from investos.schemas.provenance import ReasoningEvidenceSourceResponse
from investos.schemas.reasoning import (
    CritiqueTraceResponse,
    EvidencePacketSummaryResponse,
    ReasoningRunTraceResponse,
)
from investos.services.evidence_provenance import build_evidence_source_reference


class ReasoningTraceService:
    def __init__(self, session: AsyncSession):
        self.session = session

    @staticmethod
    def effective_model_used(model_used: str, structured_output: dict | None) -> str:
        """Correct legacy fallback provenance without rewriting historical rows."""
        stored = str(model_used or "").strip()
        payload = structured_output or {}
        if not payload.get("is_fallback") or stored.startswith("fallback:"):
            return stored

        reason = str(payload.get("fallback_reason") or "").strip()
        if not reason:
            provider = stored.lower().replace("-", "_")
            if provider and not provider.startswith(("cache", "fallback")):
                reason = f"{provider}_unavailable"
            else:
                reason = "legacy_provider_failure"
        return f"fallback:{reason}"

    async def _packet_sources(
        self, packet: EvidencePacket
    ) -> list[ReasoningEvidenceSourceResponse]:
        role_order = ("direct", "connected", "historical", "contradiction")
        role_ids = {
            "direct": packet.direct_evidence_ids or [],
            "connected": packet.connected_evidence_ids or [],
            "historical": packet.historical_evidence_ids or [],
            "contradiction": packet.contradiction_evidence_ids or [],
        }
        node_roles: dict[UUID, set[str]] = {}
        for role in role_order:
            for node_id in role_ids[role]:
                node_roles.setdefault(node_id, set()).add(role)
        if not node_roles:
            return []

        node_ids = list(node_roles)
        node_source_items: dict[UUID, set[UUID]] = {}
        for model in (Fact, Claim):
            rows = (
                await self.session.execute(
                    select(model.id, model.source_item_id).where(model.id.in_(node_ids))
                )
            ).all()
            for node_id, source_item_id in rows:
                node_source_items.setdefault(node_id, set()).add(source_item_id)

        event_ids = set(
            (
                await self.session.execute(
                    select(Event.id).where(Event.id.in_(node_ids))
                )
            ).scalars()
        )
        if event_ids:
            event_source_rows = (
                await self.session.execute(
                    select(Edge.source_id, Edge.target_id).where(
                        Edge.source_type == "event",
                        Edge.source_id.in_(event_ids),
                        Edge.target_type == "source_item",
                        Edge.relationship_type == "extracted_from",
                    )
                )
            ).all()
            for node_id, source_item_id in event_source_rows:
                node_source_items.setdefault(node_id, set()).add(source_item_id)

        source_item_ids = {
            source_item_id
            for ids in node_source_items.values()
            for source_item_id in ids
        }
        if not source_item_ids:
            return []

        source_rows = (
            await self.session.execute(
                select(SourceItem, RawEvidence, Source)
                .join(RawEvidence, SourceItem.raw_evidence_id == RawEvidence.id)
                .join(Source, RawEvidence.source_id == Source.id)
                .where(SourceItem.id.in_(source_item_ids))
            )
        ).all()
        references_by_item = {
            source_item.id: build_evidence_source_reference(
                source_item_id=source_item.id,
                raw_evidence=raw_evidence,
                source=source,
            )
            for source_item, raw_evidence, source in source_rows
        }

        aggregated: dict[UUID, dict] = {}
        for node_id, item_ids in node_source_items.items():
            for source_item_id in item_ids:
                reference = references_by_item.get(source_item_id)
                if reference is None:
                    continue
                entry = aggregated.setdefault(
                    reference.raw_evidence_id,
                    {
                        "reference": reference,
                        "roles": set(),
                        "node_ids": set(),
                    },
                )
                entry["roles"].update(node_roles.get(node_id, set()))
                entry["node_ids"].add(node_id)

        results = [
            ReasoningEvidenceSourceResponse(
                **entry["reference"].model_dump(),
                evidence_roles=[role for role in role_order if role in entry["roles"]],
                knowledge_node_ids=sorted(entry["node_ids"], key=str),
            )
            for entry in aggregated.values()
        ]
        return sorted(results, key=lambda item: item.created_at, reverse=True)

    async def get_run_trace(self, run_id: UUID) -> ReasoningRunTraceResponse | None:
        run = (
            await self.session.execute(
                select(ReasoningRun).where(ReasoningRun.id == run_id)
            )
        ).scalar_one_or_none()
        if run is None:
            return None

        packet_summary = None
        if run.evidence_packet_id is not None:
            packet = (
                await self.session.execute(
                    select(EvidencePacket).where(
                        EvidencePacket.id == run.evidence_packet_id
                    )
                )
            ).scalar_one_or_none()
            if packet is not None:
                sources = await self._packet_sources(packet)
                packet_summary = EvidencePacketSummaryResponse(
                    id=packet.id,
                    query_text=packet.query_text,
                    subject_type=packet.subject_type,
                    subject_id=packet.subject_id,
                    assembled_at=packet.assembled_at,
                    retrieval_layers_used=packet.retrieval_layers_used or [],
                    gap_flags=packet.gap_flags or [],
                    total_token_estimate=packet.total_token_estimate,
                    direct_evidence_count=len(packet.direct_evidence_ids or []),
                    connected_evidence_count=len(packet.connected_evidence_ids or []),
                    historical_evidence_count=len(packet.historical_evidence_ids or []),
                    contradiction_evidence_count=len(
                        packet.contradiction_evidence_ids or []
                    ),
                    sources=sources,
                    coverage_snapshot=packet.coverage_map_snapshot_json or {},
                    portfolio_context=packet.portfolio_context_json or {},
                )

        critique = (
            await self.session.execute(
                select(CritiqueRun).where(CritiqueRun.reasoning_run_id == run.id)
            )
        ).scalar_one_or_none()

        structured_output = run.structured_output_json or {}
        return ReasoningRunTraceResponse(
            id=run.id,
            run_type=run.run_type,
            model_used=self.effective_model_used(run.model_used, structured_output),
            model_version=run.model_version,
            prompt_hash=run.prompt_hash,
            input_tokens=run.input_tokens,
            output_tokens=run.output_tokens,
            cost_usd=run.cost_usd,
            duration_ms=run.duration_ms,
            created_at=run.created_at,
            output_text=run.output_text,
            structured_output_json=structured_output,
            evidence_packet=packet_summary,
            critique=(
                None
                if critique is None
                else CritiqueTraceResponse(
                    id=critique.id,
                    model_used=critique.model_used,
                    critique_text=critique.critique_text,
                    issues_found=critique.issues_found or [],
                    severity=critique.severity,
                    input_tokens=critique.input_tokens,
                    output_tokens=critique.output_tokens,
                    created_at=critique.created_at,
                )
            ),
        )
