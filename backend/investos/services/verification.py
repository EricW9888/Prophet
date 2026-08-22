from __future__ import annotations

import json
from time import perf_counter
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from investos.config import settings
from investos.core.llm import call_llm_json
from investos.core.prompting import (
    compact_packet_context,
    estimate_tokens_from_payload,
    estimate_tokens_from_text,
    hash_llm_request,
)
from investos.core.providers import normalize_llm_provider
from investos.models.conclusion import ConclusionRevision, ConclusionState
from investos.models.coverage import CoverageMap, MissingEvidenceClass
from investos.models.reasoning import ReasoningRun
from investos.models.verification import VerificationRun
from investos.schemas.verification import VerificationRequest, VerificationResponse
from investos.services.canonical_state import CanonicalStateService
from investos.services.corroboration import CorroborationService
from investos.services.reasoning import (
    ASSUMPTION_SCHEMA,
    MATERIAL_ASSERTION_SCHEMA,
    ReasoningService,
)
from investos.services.retrieval import RetrievalService
from investos.services.runtime_settings import RuntimeSettingsStore

VERIFICATION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "verified_stance": {
            "type": "string",
            "enum": ["bullish", "bearish", "neutral", "uncertain", "no_view"],
        },
        "confidence_band": {
            "type": "string",
            "enum": ["very_low", "low", "medium", "high", "very_high"],
        },
        "thesis_summary": {"type": "string"},
        "change_reasoning": {"type": "string"},
        "contradiction_coverage_status": {
            "type": "string",
            "enum": ["adequate", "thin", "missing"],
        },
        "missing_classes_found": {"type": "array", "items": {"type": "string"}},
        "what_would_falsify": {"type": "array", "items": {"type": "string"}},
        "supporting_evidence_ids": {"type": "array", "items": {"type": "string"}},
        "contradicting_evidence_ids": {"type": "array", "items": {"type": "string"}},
        "material_assertions": {
            "type": "array",
            "items": MATERIAL_ASSERTION_SCHEMA,
            "maxItems": 5,
        },
        "assumptions": {
            "type": "array",
            "items": ASSUMPTION_SCHEMA,
            "maxItems": 5,
        },
    },
    "required": [
        "verified_stance",
        "confidence_band",
        "thesis_summary",
        "change_reasoning",
        "contradiction_coverage_status",
        "missing_classes_found",
        "what_would_falsify",
        "supporting_evidence_ids",
        "contradicting_evidence_ids",
        "material_assertions",
        "assumptions",
    ],
}


class VerificationService:
    def __init__(self, session: AsyncSession):
        self.session = session

    def _provider_label(self) -> str:
        try:
            provider = RuntimeSettingsStore.load().llm.provider
        except Exception as exc:
            import logging

            logging.getLogger(__name__).exception("Masked failure caught")
            provider = settings.LLM_PROVIDER
        return normalize_llm_provider(provider or "nvidia_nim")

    async def run(self, payload: VerificationRequest) -> VerificationResponse:
        canonical = CanonicalStateService(self.session)
        conclusion = await canonical.get_conclusion_state(
            subject_type=payload.subject_type,
            subject_id=payload.subject_id,
        )
        prior_stance = conclusion.current_stance if conclusion else "no_view"

        retrieval = RetrievalService(self.session)
        packet = await retrieval.retrieve_evidence(
            query=payload.challenge_text
            or "Are you sure? Re-check contradiction and coverage.",
            subject_id=payload.subject_id,
            subject_type=payload.subject_type,
            max_depth=6,
        )
        packet_context = await retrieval.hydrate_packet(packet)
        prompt_context = compact_packet_context(
            packet_context, max_items_per_layer=4, max_chars=200
        )
        coverage = await self._coverage_snapshot(
            payload.subject_type, payload.subject_id
        )
        system_prompt = (
            "You are Prophet verification mode. "
            "Re-check higher-tier contradictions, missing coverage, and whether the current stance still holds. "
            "Revise only if justified. Be explicit about missing evidence. Group every material assertion with exact "
            "evidence IDs, subject/security and time scope, and record load-bearing assumptions with falsifiers. "
            "Copied coverage and multiple items from one publisher are one lineage, not corroboration."
        )
        verification_payload = {
            "trigger": payload.trigger,
            "challenge_text": payload.challenge_text,
            "prior_conclusion": (
                None
                if conclusion is None
                else {
                    "stance": conclusion.current_stance,
                    "confidence_band": conclusion.confidence_band,
                    "summary": conclusion.current_thesis_summary,
                    "what_would_falsify": conclusion.what_would_falsify or [],
                }
            ),
            "coverage": coverage,
            "packet_context": prompt_context,
        }
        prompt_hash = hash_llm_request(
            label=f"verification:{self._provider_label()}",
            system_prompt=system_prompt,
            user_payload=verification_payload,
            schema=VERIFICATION_SCHEMA,
        )

        started_at = perf_counter()
        verification = await call_llm_json(
            system_prompt=system_prompt,
            user_prompt=json.dumps(
                verification_payload, ensure_ascii=True, default=str
            ),
            schema=VERIFICATION_SCHEMA,
        )
        duration_ms = int((perf_counter() - started_at) * 1000)

        run = ReasoningRun(
            evidence_packet_id=packet.id,
            run_type="verification",
            model_used=self._provider_label(),
            prompt_hash=prompt_hash,
            input_tokens=(
                estimate_tokens_from_text(system_prompt)
                + estimate_tokens_from_payload(verification_payload)
                + estimate_tokens_from_payload(VERIFICATION_SCHEMA)
            ),
            output_tokens=estimate_tokens_from_payload(verification),
            cost_usd=0.0,
            duration_ms=duration_ms,
            output_text=verification["change_reasoning"],
            structured_output_json=verification,
        )
        self.session.add(run)
        await self.session.flush()

        candidate_stance = verification["verified_stance"]
        verification_run = VerificationRun(
            conclusion_state_id=(
                conclusion.id
                if conclusion
                else await self._ensure_placeholder_conclusion(payload, run.id)
            ),
            trigger=payload.trigger,
            evidence_packet_id=packet.id,
            higher_tier_evidence_checked=ReasoningService(self.session)._uuid_list(
                verification.get("supporting_evidence_ids", [])
            ),
            contradiction_coverage_status=verification["contradiction_coverage_status"],
            missing_classes_found=verification.get("missing_classes_found", []),
            prior_stance=prior_stance,
            verified_stance=verification["verified_stance"],
            conclusion_changed=prior_stance != verification["verified_stance"],
            change_reasoning=verification["change_reasoning"],
            reasoning_run_id=run.id,
        )
        self.session.add(verification_run)
        await self.session.flush()

        mapped_result = {
            "stance": verification["verified_stance"],
            "confidence_band": verification["confidence_band"],
            "thesis_summary": verification["thesis_summary"],
            "reasoning": verification["change_reasoning"],
            "what_would_falsify": verification.get("what_would_falsify", []),
            "what_would_strengthen": [],
            "supporting_evidence_ids": verification.get("supporting_evidence_ids", []),
            "contradicting_evidence_ids": verification.get(
                "contradicting_evidence_ids", []
            ),
            "bull_case": None,
            "bear_case": None,
            "active_contradictions": [],
            "critique_text": "",
            "material_assertions": verification.get("material_assertions", []),
            "assumptions": verification.get("assumptions", []),
            "alternative_hypotheses": [],
        }
        corroboration = CorroborationService(
            minimum_independent_sources=settings.CORROBORATION_MIN_INDEPENDENT_SOURCES,
            near_duplicate_max_distance=settings.CORROBORATION_NEAR_DUPLICATE_MAX_DISTANCE,
        )
        corroboration.assess_result(mapped_result, packet_context)
        state_updated = await ReasoningService(self.session)._update_conclusion_state(
            payload.subject_id,
            payload.subject_type,
            run,
            mapped_result,
        )
        if not state_updated:
            verification_run.conclusion_changed = False
            verification_run.verified_stance = prior_stance
            verification["candidate_stance"] = candidate_stance
            verification["verified_stance"] = prior_stance
            blocked_reason = str(
                mapped_result.get("state_update_blocked_reason")
                or "insufficient_independent_corroboration"
            )
            verification["change_reasoning"] = (
                f"Candidate stance {candidate_stance} was not promoted ({blocked_reason}); "
                f"the accepted stance remains {prior_stance}. "
                + str(verification.get("change_reasoning") or "")
            ).strip()
            verification_run.change_reasoning = verification["change_reasoning"]
            run.output_text = verification["change_reasoning"]
        verification["corroboration"] = mapped_result.get("corroboration")
        if mapped_result.get("state_update_blocked_reason"):
            verification["state_update_blocked_reason"] = mapped_result[
                "state_update_blocked_reason"
            ]
        verification["confidence_band"] = mapped_result["confidence_band"]
        run.structured_output_json = dict(verification)
        await self.session.commit()
        return VerificationResponse(
            id=verification_run.id,
            subject_id=payload.subject_id,
            subject_type=payload.subject_type,
            trigger=payload.trigger,
            prior_stance=prior_stance,
            verified_stance=verification["verified_stance"],
            confidence_band=verification["confidence_band"],
            conclusion_changed=verification_run.conclusion_changed,
            contradiction_coverage_status=verification["contradiction_coverage_status"],
            missing_classes_found=verification.get("missing_classes_found", []),
            change_reasoning=verification["change_reasoning"],
            what_would_falsify=verification.get("what_would_falsify", []),
            supporting_evidence_ids=verification.get("supporting_evidence_ids", []),
            contradicting_evidence_ids=verification.get(
                "contradicting_evidence_ids", []
            ),
            verified_at=verification_run.verified_at,
        )

    async def _coverage_snapshot(self, subject_type: str, subject_id: UUID) -> dict:
        coverage = await CanonicalStateService(self.session).get_coverage_map(
            subject_type=subject_type,
            subject_id=subject_id,
        )
        if not coverage:
            return {}
        missing_classes = list(
            (
                await self.session.execute(
                    select(MissingEvidenceClass).where(
                        MissingEvidenceClass.coverage_map_id == coverage.id,
                        MissingEvidenceClass.resolved_at.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        return {
            "coverage_score": coverage.overall_coverage_score,
            "high_tier_evidence_count": coverage.high_tier_evidence_count,
            "contradiction_count": coverage.contradiction_count,
            "missing_classes": [item.class_name for item in missing_classes],
        }

    async def _ensure_placeholder_conclusion(
        self, payload: VerificationRequest, reasoning_run_id: UUID
    ) -> UUID:
        state = await CanonicalStateService(self.session).ensure_conclusion_state(
            subject_type=payload.subject_type,
            subject_id=payload.subject_id,
            create=lambda: ConclusionState(
                subject_type=payload.subject_type,
                subject_id=payload.subject_id,
                current_thesis_summary="Verification initialized without prior conclusion.",
                current_stance="no_view",
                confidence_band="very_low",
                reasoning_run_id=reasoning_run_id,
            ),
        )
        self.session.add(
            ConclusionRevision(
                conclusion_state_id=state.id,
                previous_stance="none",
                new_stance="no_view",
                previous_confidence="none",
                new_confidence="very_low",
                trigger_evidence_ids=[],
                revision_reasoning="Verification created initial placeholder conclusion state.",
                reasoning_run_id=reasoning_run_id,
            )
        )
        return state.id
