from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from investos.models.reasoning import CritiqueRun, EvidencePacket, ReasoningRun
from investos.schemas.reasoning import (
    CritiqueTraceResponse,
    EvidencePacketSummaryResponse,
    ReasoningRunTraceResponse,
)


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
