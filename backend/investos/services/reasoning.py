import asyncio
import json
from time import perf_counter
from typing import Awaitable, Callable
from uuid import UUID

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from investos.config import settings
from investos.core.llm import (
    LLMProviderCooldownError,
    available_llm_json_recovery_providers,
    call_llm_json,
    compact_exception_message,
)
from investos.core.prompting import (
    compact_packet_context,
    estimate_tokens_from_payload,
    estimate_tokens_from_text,
    hash_llm_request,
)
from investos.core.providers import llm_provider_capability, normalize_llm_provider
from investos.models.conclusion import ConclusionRevision, ConclusionState
from investos.models.profile import Profile
from investos.models.reasoning import CritiqueRun, EvidencePacket, ReasoningRun
from investos.services.canonical_state import CanonicalStateService
from investos.services.corroboration import CorroborationService
from investos.services.retrieval import RetrievalService
from investos.services.runtime_settings import RuntimeSettingsStore

MATERIAL_ASSERTION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "statement": {"type": "string"},
        "subject_scope": {"type": "string"},
        "time_scope": {"type": ["string", "null"]},
        "scope_consistency": {
            "type": "string",
            "enum": ["matched", "mixed", "unknown"],
        },
        "scope_notes": {"type": "string"},
        "evidence_basis": {
            "type": "string",
            "enum": [
                "retrieved_source",
                "portfolio_ledger",
                "market_data_calculation",
                "model_assumption",
            ],
        },
        "supporting_context_paths": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Exact dot-separated paths into the supplied packet, using numeric list indexes, "
                "for example portfolio_context.top_holdings.0.weight_pct."
            ),
        },
        "supporting_evidence_ids": {"type": "array", "items": {"type": "string"}},
        "contradicting_evidence_ids": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "statement",
        "subject_scope",
        "time_scope",
        "scope_consistency",
        "scope_notes",
        "evidence_basis",
        "supporting_context_paths",
        "supporting_evidence_ids",
        "contradicting_evidence_ids",
    ],
}

ASSUMPTION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "statement": {"type": "string"},
        "is_material": {"type": "boolean"},
        "evidence_ids": {"type": "array", "items": {"type": "string"}},
        "falsifier": {"type": "string"},
    },
    "required": ["statement", "is_material", "evidence_ids", "falsifier"],
}

ALTERNATIVE_HYPOTHESIS_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "hypothesis": {"type": "string"},
        "supporting_evidence_ids": {"type": "array", "items": {"type": "string"}},
        "disconfirming_evidence_ids": {"type": "array", "items": {"type": "string"}},
        "decisive_test": {"type": "string"},
    },
    "required": [
        "hypothesis",
        "supporting_evidence_ids",
        "disconfirming_evidence_ids",
        "decisive_test",
    ],
}

REASONING_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "stance": {
            "type": "string",
            "enum": ["bullish", "bearish", "neutral", "uncertain", "no_view"],
        },
        "confidence_band": {
            "type": "string",
            "enum": ["very_low", "low", "medium", "high", "very_high"],
        },
        "thesis_summary": {"type": "string"},
        "reasoning": {"type": "string"},
        "what_would_falsify": {"type": "array", "items": {"type": "string"}},
        "what_would_strengthen": {"type": "array", "items": {"type": "string"}},
        "supporting_evidence_ids": {"type": "array", "items": {"type": "string"}},
        "contradicting_evidence_ids": {"type": "array", "items": {"type": "string"}},
        "bull_case": {"type": ["string", "null"]},
        "bear_case": {"type": ["string", "null"]},
        "active_contradictions": {"type": "array", "items": {"type": "string"}},
        "critique_text": {"type": "string"},
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
        "alternative_hypotheses": {
            "type": "array",
            "items": ALTERNATIVE_HYPOTHESIS_SCHEMA,
            "maxItems": 4,
        },
        "active_watchers": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "ticker": {"type": "string"},
                    "condition_type": {
                        "type": "string",
                        "description": (
                            "Specific snake_case trigger type. Use price_above/price_below for quoted price levels, "
                            "earnings_release/news_sentiment for those exact cases, or a precise open-ended catalyst, "
                            "metric, filing, reminder, contradiction, or setup trigger when that is more accurate."
                        ),
                    },
                    "threshold": {"type": ["number", "null"]},
                    "objective": {"type": "string"},
                    "adjustment_plan": {"type": "string"},
                    "deadline_hours": {"type": ["number", "null"]},
                },
                "required": [
                    "ticker",
                    "condition_type",
                    "threshold",
                    "objective",
                    "adjustment_plan",
                    "deadline_hours",
                ],
            },
        },
    },
    "required": [
        "stance",
        "confidence_band",
        "thesis_summary",
        "reasoning",
        "what_would_falsify",
        "what_would_strengthen",
        "supporting_evidence_ids",
        "contradicting_evidence_ids",
        "bull_case",
        "bear_case",
        "active_contradictions",
        "critique_text",
        "material_assertions",
        "assumptions",
        "alternative_hypotheses",
        "active_watchers",
    ],
}

CRITIQUE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "candidate_stance": {
            "type": "string",
            "enum": ["bullish", "bearish", "neutral", "uncertain", "no_view"],
        },
        "confidence_band": {
            "type": "string",
            "enum": ["very_low", "low", "medium", "high", "very_high"],
        },
        "independent_summary": {"type": "string"},
        "question_answerability": {
            "type": "string",
            "enum": ["adequate", "partial", "inadequate"],
        },
        "answerability_reason": {"type": "string"},
        "missing_information": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 6,
        },
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
        "alternative_hypotheses": {
            "type": "array",
            "items": ALTERNATIVE_HYPOTHESIS_SCHEMA,
            "maxItems": 4,
        },
        "issues_found": {"type": "array", "items": {"type": "string"}},
        "severity": {"type": "string", "enum": ["none", "minor", "major", "critical"]},
    },
    "required": [
        "candidate_stance",
        "confidence_band",
        "independent_summary",
        "question_answerability",
        "answerability_reason",
        "missing_information",
        "material_assertions",
        "assumptions",
        "alternative_hypotheses",
        "issues_found",
        "severity",
    ],
}


class ReasoningService:
    def __init__(self, session: AsyncSession):
        self.session = session

    def _interactive_timeout_seconds(self, *, thin_packet: bool = False) -> int:
        configured = int(
            settings.LLM_TIMEOUT_SECONDS or settings.CODEX_TIMEOUT_SECONDS or 30
        )
        upper_bound = 90 if thin_packet else 120
        return max(30, min(configured, upper_bound))

    def _provider_label(self) -> str:
        try:
            from investos.services.runtime_settings import RuntimeSettingsStore

            runtime = RuntimeSettingsStore.load().llm
            provider = runtime.provider
        except Exception:
            provider = settings.LLM_PROVIDER or "nvidia_nim"

        return normalize_llm_provider(provider)

    def _active_provider_unconfigured_label(self) -> str | None:
        try:
            runtime = RuntimeSettingsStore.load().llm
        except Exception:
            return None
        provider = normalize_llm_provider(
            runtime.provider or settings.LLM_PROVIDER or "nvidia_nim"
        )
        capability = llm_provider_capability(provider)
        if capability and capability.requires_api_key and not runtime.api_key:
            return f"{provider}_unconfigured"
        return None

    def _fallback_reason_label(self, exc: Exception | None = None) -> str:
        configured_issue = self._active_provider_unconfigured_label()
        if configured_issue:
            return configured_issue
        provider = self._provider_label().replace("-", "_")
        if exc is None:
            return f"{provider}_unavailable"
        message = compact_exception_message(exc).lower()
        if "unauthorized" in message or "401" in message:
            return f"{provider}_unauthorized"
        capability = llm_provider_capability(provider)
        if "api key" in message and capability and capability.requires_api_key:
            return f"{provider}_unconfigured"
        if isinstance(exc, LLMProviderCooldownError):
            return f"{provider}_rate_limited"
        if isinstance(exc, httpx.HTTPStatusError):
            status_code = exc.response.status_code
            if status_code == 429:
                return f"{provider}_rate_limited"
            if status_code == 503:
                return f"{provider}_service_unavailable"
            if status_code >= 500:
                return f"{provider}_service_error"
        if self._is_timeout_like(exc):
            return f"{provider}_timeout"
        if isinstance(exc, httpx.ConnectError):
            return f"{provider}_connection_error"
        if isinstance(exc, httpx.RequestError):
            return f"{provider}_network_error"
        if isinstance(exc, (ValueError, json.JSONDecodeError)) and any(
            marker in message
            for marker in ("valid json", "empty response", "structured", "schema")
        ):
            return f"{provider}_invalid_response"
        return f"{provider}_failed"

    def _model_used_label(self, result: dict | None = None) -> str:
        if (result or {}).get("is_fallback"):
            reason = str(
                (result or {}).get("fallback_reason") or self._fallback_reason_label()
            ).strip()
            return f"fallback:{reason}"
        provider = self._provider_label()
        mode = str((result or {}).get("recovery_mode") or "").strip()
        if mode.startswith("alternate_provider:"):
            return f"{provider}->{mode.removeprefix('alternate_provider:')}"
        if mode == "reduced_context":
            return f"{provider}:reduced_context"
        return provider

    def _analysis_cache_label(self) -> str:
        return f"reasoning_analysis:v4:{self._provider_label()}"

    def _is_timeout_like(self, exc: Exception) -> bool:
        name = exc.__class__.__name__.lower()
        return isinstance(exc, TimeoutError) or "timeout" in name

    async def run_analysis(
        self,
        packet_id: UUID,
        *,
        include_critique: bool = True,
        supplemental_context: dict | None = None,
        allow_state_update: bool = True,
        on_chunk: Callable[[str], Awaitable[None]] | None = None,
    ) -> tuple[ReasoningRun, dict]:
        packet = (
            await self.session.execute(
                select(EvidencePacket).where(EvidencePacket.id == packet_id)
            )
        ).scalar_one()
        packet_context = await RetrievalService(self.session).hydrate_packet(packet)
        if supplemental_context:
            packet_context = {**packet_context, **supplemental_context}
        prompt_context = compact_packet_context(packet_context)
        system_prompt = self._analysis_system_prompt()
        prompt_hash = hash_llm_request(
            label=self._analysis_cache_label(),
            system_prompt=system_prompt,
            user_payload=prompt_context,
            schema=REASONING_SCHEMA,
        )
        input_tokens = (
            estimate_tokens_from_text(system_prompt)
            + estimate_tokens_from_payload(prompt_context)
            + estimate_tokens_from_payload(REASONING_SCHEMA)
        )

        provider_unconfigured = self._active_provider_unconfigured_label()
        cached_run = (
            None
            if provider_unconfigured
            else await self._find_cached_run("analysis", prompt_hash)
        )
        if cached_run is not None and cached_run.structured_output_json:
            result = dict(cached_run.structured_output_json)
            result["cache_hit"] = True
            result["cache_source_run_id"] = str(cached_run.id)
            result["cache_source_created_at"] = cached_run.created_at.isoformat()
            result["cache_source_model_used"] = cached_run.model_used
            run = ReasoningRun(
                evidence_packet_id=packet.id,
                run_type="analysis",
                model_used="cache_hit:previous_analysis",
                prompt_hash=prompt_hash,
                input_tokens=0,
                output_tokens=0,
                cost_usd=0.0,
                duration_ms=1,
                output_text=result.get("reasoning"),
                structured_output_json=result,
            )
            self.session.add(run)
            await self.session.flush()
        else:
            started_at = perf_counter()
            result = await self._reason_with_llm(
                prompt_context, system_prompt=system_prompt, on_chunk=on_chunk
            )
            duration_ms = int((perf_counter() - started_at) * 1000)
            run = ReasoningRun(
                evidence_packet_id=packet.id,
                run_type="analysis",
                model_used=self._model_used_label(result),
                prompt_hash=prompt_hash,
                input_tokens=input_tokens,
                output_tokens=estimate_tokens_from_payload(result),
                duration_ms=duration_ms,
                output_text=result["reasoning"],
                structured_output_json=result,
            )
            self.session.add(run)
            await self.session.flush()

        corroboration = CorroborationService(
            minimum_independent_sources=settings.CORROBORATION_MIN_INDEPENDENT_SOURCES,
            near_duplicate_max_distance=settings.CORROBORATION_NEAR_DUPLICATE_MAX_DISTANCE,
        )
        corroboration.assess_result(result, packet_context)
        if include_critique:
            independent_review = await self._create_critique(
                run, prompt_context, result
            )
            corroboration.apply_independent_review(result, independent_review)
        if allow_state_update:
            state_updated = await self._update_conclusion_state(
                packet.subject_id, packet.subject_type, run, result
            )
            if state_updated:
                await self._update_profile(
                    packet.subject_id, packet.subject_type, result
                )
        else:
            result["state_update_blocked_reason"] = "fresh_search_context_not_promoted"
        run.structured_output_json = dict(result)
        await self.session.commit()
        return run, result

    async def _update_conclusion_state(
        self,
        subject_id: UUID,
        subject_type: str,
        run: ReasoningRun,
        result: dict,
    ) -> bool:
        state = (
            await self.session.execute(
                select(ConclusionState).where(
                    ConclusionState.subject_id == subject_id,
                    ConclusionState.subject_type == subject_type,
                )
            )
        ).scalar_one_or_none()

        corroboration = result.get("corroboration") or {}
        can_promote = bool(corroboration.get("can_promote"))
        blocked_for_corroboration = not result.get("is_fallback") and not can_promote
        if blocked_for_corroboration:
            result["state_update_blocked_reason"] = (
                "insufficient_independent_corroboration"
            )
            if state is not None:
                return False

        # A fallback is a non-conclusion (provider failed). It must never become the
        # central belief: don't overwrite a real thesis, and don't echo the prompt as one.
        if result.get("is_fallback") or blocked_for_corroboration:
            if state is not None:
                result["state_update_blocked_reason"] = "provider_fallback_not_promoted"
                return False
            new_stance = "no_view"
            new_confidence = "very_low"
            new_summary = ""
        else:
            new_stance = result["stance"]
            new_confidence = result["confidence_band"]
            new_summary = result["thesis_summary"]
        support_ids = self._uuid_list(result.get("supporting_evidence_ids", []))
        contradiction_ids = self._uuid_list(
            result.get("contradicting_evidence_ids", [])
        )
        canonical = CanonicalStateService(self.session)

        if not state:
            state = await canonical.ensure_conclusion_state(
                subject_type=subject_type,
                subject_id=subject_id,
                create=lambda: ConclusionState(
                    subject_id=subject_id,
                    subject_type=subject_type,
                    current_stance=new_stance,
                    confidence_band=new_confidence,
                    current_thesis_summary=new_summary,
                    key_supporting_evidence_ids=support_ids,
                    key_contradicting_evidence_ids=contradiction_ids,
                    what_would_falsify=result.get("what_would_falsify", []),
                    what_would_strengthen=result.get("what_would_strengthen", []),
                    reasoning_run_id=run.id,
                ),
            )
            self.session.add(
                ConclusionRevision(
                    conclusion_state_id=state.id,
                    previous_stance="none",
                    new_stance=new_stance,
                    previous_confidence="none",
                    new_confidence=new_confidence,
                    trigger_evidence_ids=support_ids,
                    revision_reasoning="Initial conclusion state creation.",
                    reasoning_run_id=run.id,
                )
            )
            return not blocked_for_corroboration and not result.get("is_fallback")

        if (
            state.current_stance != new_stance
            or state.confidence_band != new_confidence
        ):
            self.session.add(
                ConclusionRevision(
                    conclusion_state_id=state.id,
                    previous_stance=state.current_stance,
                    new_stance=new_stance,
                    previous_confidence=state.confidence_band,
                    new_confidence=new_confidence,
                    trigger_evidence_ids=support_ids,
                    revision_reasoning=result["reasoning"],
                    reasoning_run_id=run.id,
                )
            )

        state.current_stance = new_stance
        state.confidence_band = new_confidence
        state.current_thesis_summary = new_summary
        state.key_supporting_evidence_ids = support_ids
        state.key_contradicting_evidence_ids = contradiction_ids
        state.what_would_falsify = result.get("what_would_falsify", [])
        state.what_would_strengthen = result.get("what_would_strengthen", [])
        state.reasoning_run_id = run.id
        state.update_count += 1
        return True

    async def _update_profile(
        self, subject_id: UUID, subject_type: str, result: dict
    ) -> None:
        profile = (
            await self.session.execute(
                select(Profile).where(
                    Profile.subject_type == subject_type,
                    Profile.subject_id == subject_id,
                )
            )
        ).scalar_one_or_none()
        if not profile:
            profile = Profile(subject_type=subject_type, subject_id=subject_id)
            self.session.add(profile)

        new_summary = result.get("thesis_summary", "")
        is_empty_result = result.get("stance") in ("no_view", "uncertain") and any(
            phrase in (new_summary or "").lower()
            for phrase in [
                "no information",
                "no evidence",
                "evidence packet contains no",
                "cannot be formed",
                "no defensible stance",
            ]
        )
        if is_empty_result and profile.executive_summary:
            if result.get("bull_case"):
                profile.bull_case = result["bull_case"]
            if result.get("bear_case"):
                profile.bear_case = result["bear_case"]
            if result.get("active_contradictions"):
                profile.active_contradictions = result["active_contradictions"]
            return

        profile.executive_summary = new_summary
        profile.bull_case = result.get("bull_case")
        profile.bear_case = result.get("bear_case")
        profile.active_contradictions = result.get("active_contradictions", [])

    async def _create_critique(
        self, run: ReasoningRun, packet_context: dict, result: dict
    ) -> dict | None:
        # The second pass is blind to the first answer. Independence is more
        # useful than asking one model to endorse its own wording.
        if (
            result.get("is_fallback")
            or self._active_provider_unconfigured_label()
            or not settings.REASONING_INDEPENDENT_REVIEW_ENABLED
        ):
            return None
        system_prompt = (
            "Independently analyze this investment evidence packet without seeing another analyst's answer. "
            "First judge whether the packet directly answers the user's original question: adequate, partial, or "
            "inadequate. Adjacent portfolio facts, a different company, or a historical analogy that does not test "
            "the named question are not an answer. State exactly what information is missing. "
            "Return a concise candidate stance, material assertions with exact evidence IDs, explicit material "
            "assumptions and falsifiers, plausible alternative hypotheses, and any evidence-quality issues. "
            "Do not invent facts or fill gaps. Distinguish copied coverage from independent sources, check that "
            "the subject/security and time period match, and use uncertain or no_view when the packet cannot decide. "
            "Portfolio ledger values and deterministic calculations may be supported by exact packet context paths "
            "instead of external evidence IDs; do not mislabel those account facts as unsupported merely because "
            "they lack a publication. External factual or causal claims still require retrieved sources. "
            "For a claimed cause of a market move, separate timestamp coincidence from causal evidence and preserve "
            "credible competing explanations until a source, cross-asset pattern, or event sequence discriminates among them. "
            "The summary must be user-safe and must not expose private chain-of-thought."
        )
        critique_payload = {"packet_context": packet_context}
        try:
            critique = await call_llm_json(
                system_prompt=system_prompt,
                user_prompt=json_dumps(critique_payload),
                schema=CRITIQUE_SCHEMA,
            )
        except Exception:
            return None
        review = {
            "candidate_stance": critique["candidate_stance"],
            "confidence_band": critique["confidence_band"],
            "summary": critique["independent_summary"],
            "question_answerability": critique.get(
                "question_answerability", "inadequate"
            ),
            "answerability_reason": critique.get("answerability_reason", ""),
            "missing_information": critique.get("missing_information", []),
            "material_assertions": critique.get("material_assertions", []),
            "assumptions": critique.get("assumptions", []),
            "alternative_hypotheses": critique.get("alternative_hypotheses", []),
            "issues_found": critique.get("issues_found", []),
            "severity": critique["severity"],
        }
        self.session.add(
            CritiqueRun(
                reasoning_run_id=run.id,
                model_used=self._provider_label(),
                critique_text=critique["independent_summary"],
                issues_found=critique["issues_found"],
                severity=critique["severity"],
                input_tokens=(
                    estimate_tokens_from_text(system_prompt)
                    + estimate_tokens_from_payload(critique_payload)
                    + estimate_tokens_from_payload(CRITIQUE_SCHEMA)
                ),
                output_tokens=estimate_tokens_from_payload(critique),
                cost_usd=0.0,
            )
        )
        return review

    async def _reason_with_llm(
        self,
        packet_context: dict,
        *,
        system_prompt: str,
        on_chunk: Callable[[str], Awaitable[None]] | None = None,
    ) -> dict:
        if self._is_thin_packet(packet_context):
            return await self._thin_packet_reasoning(packet_context, on_chunk=on_chunk)
        try:
            result = await call_llm_json(
                system_prompt=system_prompt,
                user_prompt=json_dumps(packet_context),
                schema=REASONING_SCHEMA,
                timeout_seconds=self._interactive_timeout_seconds(),
                on_chunk=on_chunk,
            )
            result = self._bounded_reasoning_result(result)
            return self._attach_source_feedback_influence(result, packet_context)
        except Exception as exc:
            recovery_context = self._recovery_packet_context(packet_context)
            recovery_result = await self._reduced_context_recovery(
                packet_context,
                system_prompt=system_prompt,
                on_chunk=on_chunk,
            )
            if recovery_result is not None:
                return recovery_result
            alternate_result = await self._alternate_provider_recovery(
                recovery_context,
                system_prompt=system_prompt,
            )
            if alternate_result is not None:
                return alternate_result
            if self._is_timeout_like(exc):
                return self._timeout_fallback_reasoning(
                    packet_context,
                    failure_reason=self._fallback_reason_label(exc),
                )
            return self._fallback_reasoning(
                packet_context,
                failure_reason=self._fallback_reason_label(exc),
            )

    def _analysis_system_prompt(self) -> str:
        return (
            "Build a structured investment-research view from the supplied evidence packet. "
            "Use the 'reasoning' field to provide a concise, user-safe rationale summary. "
            "Do not expose private chain-of-thought. Record the answer's material factual and causal assertions in "
            "material_assertions, with their evidence basis, exact evidence IDs or structured context paths, "
            "subject/security scope, time/period scope, and whether those scopes match. Use retrieved_source for "
            "externally checkable facts, portfolio_ledger for account-owned facts, market_data_calculation for deterministic "
            "derived values already present in the packet, and model_assumption only for explicitly unverified propositions. "
            "A ledger fact or deterministic calculation does not need an outside publication, but it must name a real "
            "dot-separated supporting_context_path such as portfolio_context.top_holdings.0.weight_pct. External factual "
            "and causal claims still require evidence IDs. Record every "
            "load-bearing inference in assumptions with a falsifier; never treat an assumption "
            "as a fact. Generate plausible alternative_hypotheses and the evidence that would discriminate among them. "
            "For causal attribution, distinguish temporal coincidence from evidence of transmission and keep credible "
            "competing explanations open until event timing, cross-asset behavior, or direct reporting discriminates among them. "
            "Corroboration means genuinely independent source lineages, not multiple copies of the same report or multiple "
            "claims extracted from one publisher. A material investment conclusion should not be promoted from one source. "
            "Weigh support, contradictions, blind spots, and plausible counter-arguments before choosing the stance. "
            "When historical_analogies are present, use them as rhymes and confounder checks, not predictions. "
            "When historical_analogy_lenses are present, apply the lens explicitly: what rhymes, what would break the "
            "analogy, the current dominant-channel test, and the portfolio/watch implication. "
            "Connect the answer back to portfolio implications when the packet supports that connection. "
            "When fresh_research_context is present, use it as a current-event preflight: answer the fresh/current part "
            "only from those returned source snippets or from durable evidence that explicitly covers the same event. "
            "Name source titles or URLs when relying on fresh snippets. If fresh search failed, returned no results, or was "
            "rate-limited, say the current-event claim is unverified instead of filling with stale local context. "
            "Do not treat ephemeral fresh-search snippets as accepted thesis evidence until they have been ingested and extracted. "
            "For earnings, guidance, deals, announcements, or other market events, separate prior investor expectations "
            "from the actual result, market sentiment and positioning, institutional or ownership flows, price reaction, "
            "estimate/guidance revisions, peer read-through, and portfolio sizing or timing implication. "
            "Do not call an event bullish merely because the company overperformed; compare the actual result against the setup, "
            "including cases where investors expected overperformance and the result underperformed that higher hurdle. "
            "When portfolio_context contains market_setup_signals or subject_market_setup_signals, treat them as source-dated setup context: "
            "use them to explain the hurdle, sentiment, positioning, flows, metrics, and next evidence test, but do not promote them as accepted thesis evidence without corroboration. "
            "When portfolio_context contains fundamental_metrics or subject_fundamental_metrics, treat them as source-dated financial/operating evidence: "
            "cite the metric name, value, period, as-of/public time, source, investment relevance, and stale/next-test notes where available. "
            "When portfolio_context contains performance_attribution, use its measured gain, return, benchmark gap, coverage, and per-security contributions "
            "as the accounting baseline. Do not invent catalysts from price attribution; diagnose the largest measured contributors against dated evidence and competing explanations. "
            "When relevant, evaluate source-dated financial and competitive metrics as evidence. Examples include valuation, profitability, "
            "growth, margins, debt/leverage, liquidity, interest coverage, dilution, capital intensity, estimate revisions, peer comparisons, "
            "and sector KPIs, but treat that as an open ontology rather than a closed checklist. "
            "When it is decision-relevant, trace the business model through who pays, the value delivered, revenue and cost drivers, "
            "customer and supplier dependencies, reinvestment needs, and where durable value is captured. Also test material externalities "
            "or second-order effects that could change demand, costs, regulation, reputation, or stakeholder behavior. Do not force these "
            "dimensions onto every subject, and do not turn unsupported ethical, environmental, or social claims into investment facts. "
            "Treat active_watchers as durable alert rules, not commentary. Return an empty list unless the answer identifies a "
            "concrete future observable, trigger condition, and response that are not already represented in the supplied active "
            "watchers. Use a specific snake_case condition_type for the actual trigger; do not force catalysts, metric checks, "
            "filings, reminders, or thesis-contradiction tests into earnings_release or news_sentiment when a more precise label fits. "
            "For price alerts, threshold must be an absolute quoted price, never a percentage or ratio. Non-price alerts must use "
            "a null threshold and carry the observable test in objective and adjustment_plan. Usually propose no more than one new watch. "
            "If local evidence is thin or off-topic, say so and keep confidence low."
        )

    def _thin_packet_system_prompt(self) -> str:
        return (
            "The stored evidence packet for this question is thin. Give the best structured answer possible without pretending "
            "that local evidence exists. Use general market knowledge only as clearly labeled low-confidence context. "
            "Return explicit material_assertions, assumptions, and alternative_hypotheses. Empty evidence ID lists are more "
            "honest than fabricated support, and unsupported assumptions must remain unresolved. "
            "For a claimed cause of a market move, treat timestamp overlap as a hypothesis rather than proof and state the "
            "event-sequence or cross-asset observation that would distinguish it from other plausible causes. "
            "Identify the actual causal mechanism when one is apparent from the user question or known subject context; otherwise "
            "state what must be researched before a thesis can be accepted. "
            "For earnings, guidance, deals, announcements, or other market events, frame the missing evidence as an expectation-delta problem: "
            "what investors expected, whether the setup required overperformance, what happened, how sentiment/positioning/flows and price/estimates reacted, "
            "and how it transmits to the portfolio. "
            "If ordinary financial or competitive metrics would determine the answer but are absent, name the missing open-ended metric family "
            "such as valuation, profitability, growth, debt/leverage, liquidity, peer comparison, or sector KPI, and ask for source-dated values "
            "instead of improvising certainty. "
            "When historical_analogies are present, use them as rhymes and confounder checks, not predictions. "
            "When historical_analogy_lenses are present, state what rhymes, what breaks the analogy, and the current "
            "dominant-channel check before accepting the analogy as useful. "
            "Keep no_view or uncertain when the local graph cannot support an accepted thesis."
        )

    def _is_reusable_cached_output(self, payload: dict | None) -> bool:
        if not payload or payload.get("is_fallback"):
            return False
        critique = str(payload.get("critique_text") or "").strip().lower()
        fallback_markers = (
            "fallback used",
            "timeout fallback",
            "llm reasoning was not available",
        )
        return not any(marker in critique for marker in fallback_markers)

    async def _find_cached_run(
        self, run_type: str, prompt_hash: str
    ) -> ReasoningRun | None:
        runs = (
            (
                await self.session.execute(
                    select(ReasoningRun)
                    .where(
                        ReasoningRun.run_type == run_type,
                        ReasoningRun.prompt_hash == prompt_hash,
                        ReasoningRun.structured_output_json.is_not(None),
                    )
                    .order_by(ReasoningRun.created_at.desc())
                    .limit(10)
                )
            )
            .scalars()
            .all()
        )
        for run in runs:
            if self._is_reusable_cached_output(run.structured_output_json):
                return run
        return None

    def _is_thin_packet(self, packet_context: dict) -> bool:
        portfolio_context = packet_context.get("portfolio_context") or {}
        market_setup = (portfolio_context.get("subject_market_setup_signals") or []) + (
            portfolio_context.get("market_setup_signals") or []
        )
        fundamental_metrics = (
            portfolio_context.get("subject_fundamental_metrics") or []
        ) + (portfolio_context.get("fundamental_metrics") or [])
        return not any(
            [
                packet_context.get("direct_evidence"),
                packet_context.get("connected_evidence"),
                packet_context.get("historical_evidence"),
                packet_context.get("contradiction_evidence"),
                packet_context.get("lessons"),
                fundamental_metrics,
                market_setup,
            ]
        )

    async def _thin_packet_reasoning(
        self,
        packet_context: dict,
        on_chunk: Callable[[str], Awaitable[None]] | None = None,
    ) -> dict:
        thin_payload = {
            "query_text": packet_context.get("query_text"),
            "subject_type": packet_context.get("subject_type"),
            "subject_id": str(packet_context.get("subject_id")),
            "coverage": packet_context.get("coverage") or {},
            "gap_flags": packet_context.get("gap_flags") or [],
            "portfolio_context": packet_context.get("portfolio_context") or {},
            "fresh_research_context": packet_context.get("fresh_research_context")
            or {},
            "conversation_context": packet_context.get("conversation_context") or {},
            "lessons": packet_context.get("lessons") or [],
            "historical_analogies": packet_context.get("historical_analogies") or [],
        }
        try:
            result = await call_llm_json(
                system_prompt=self._thin_packet_system_prompt(),
                user_prompt=json_dumps(thin_payload),
                schema=REASONING_SCHEMA,
                timeout_seconds=self._interactive_timeout_seconds(thin_packet=True),
                on_chunk=on_chunk,
            )
            result = self._bounded_reasoning_result(result, max_evidence_ids=3)
            if result.get("confidence_band") not in {"very_low", "low", "medium"}:
                result["confidence_band"] = "low"
            return self._attach_source_feedback_influence(result, thin_payload)
        except Exception as exc:
            alternate_result = await self._alternate_provider_recovery(
                thin_payload,
                system_prompt=self._thin_packet_system_prompt(),
                max_evidence_ids=3,
            )
            if alternate_result is not None:
                if alternate_result.get("confidence_band") not in {
                    "very_low",
                    "low",
                    "medium",
                }:
                    alternate_result["confidence_band"] = "low"
                return self._attach_source_feedback_influence(
                    alternate_result, thin_payload
                )
            return self._fallback_reasoning(
                packet_context,
                failure_reason=self._fallback_reason_label(exc),
            )

    async def _reduced_context_recovery(
        self,
        packet_context: dict,
        *,
        system_prompt: str,
        on_chunk: Callable[[str], Awaitable[None]] | None = None,
    ) -> dict | None:
        recovery_context = self._recovery_packet_context(packet_context)
        if recovery_context == packet_context:
            return None
        try:
            result = await call_llm_json(
                system_prompt=system_prompt,
                user_prompt=json_dumps(recovery_context),
                schema=REASONING_SCHEMA,
                timeout_seconds=max(20, min(self._interactive_timeout_seconds(), 45)),
                on_chunk=on_chunk,
            )
            result = self._bounded_reasoning_result(result)
            result["recovery_mode"] = "reduced_context"
            return self._attach_source_feedback_influence(result, recovery_context)
        except Exception:
            return None

    def _bounded_reasoning_result(
        self, result: dict, *, max_evidence_ids: int = 5
    ) -> dict:
        result["supporting_evidence_ids"] = result.get("supporting_evidence_ids", [])[
            :max_evidence_ids
        ]
        result["contradicting_evidence_ids"] = result.get(
            "contradicting_evidence_ids", []
        )[:max_evidence_ids]
        result["what_would_falsify"] = result.get("what_would_falsify", [])[:3]
        result["what_would_strengthen"] = result.get("what_would_strengthen", [])[:3]
        result["active_contradictions"] = result.get("active_contradictions", [])[:3]
        result["active_watchers"] = result.get("active_watchers", [])[:2]
        result["material_assertions"] = result.get("material_assertions", [])[:5]
        result["assumptions"] = result.get("assumptions", [])[:5]
        result["alternative_hypotheses"] = result.get("alternative_hypotheses", [])[:4]
        return result

    async def _alternate_provider_recovery(
        self,
        recovery_context: dict,
        *,
        system_prompt: str,
        max_evidence_ids: int = 5,
    ) -> dict | None:
        for provider in await available_llm_json_recovery_providers(
            self._provider_label()
        ):
            try:
                result = await call_llm_json(
                    system_prompt=system_prompt,
                    user_prompt=json_dumps(recovery_context),
                    schema=REASONING_SCHEMA,
                    timeout_seconds=max(
                        20, min(self._interactive_timeout_seconds(), 60)
                    ),
                    provider_override=provider,
                )
                result = self._bounded_reasoning_result(
                    result, max_evidence_ids=max_evidence_ids
                )
                result["recovery_mode"] = f"alternate_provider:{provider}"
                return self._attach_source_feedback_influence(result, recovery_context)
            except Exception:
                continue
        return None

    def _attach_source_feedback_influence(
        self, result: dict, packet_context: dict
    ) -> dict:
        influence = self._source_feedback_influence(packet_context)
        if influence:
            result["source_feedback_influence"] = influence
        return result

    def _source_feedback_influence(self, packet_context: dict) -> dict | None:
        feedback = packet_context.get("source_feedback_context")
        if not isinstance(feedback, dict) or not feedback:
            feedback = (packet_context.get("portfolio_context") or {}).get(
                "source_feedback"
            )
        if not isinstance(feedback, dict) or not feedback:
            return None

        counts_payload = (
            feedback.get("counts") if isinstance(feedback.get("counts"), dict) else {}
        )
        counts = {
            "useful": int(counts_payload.get("useful") or 0),
            "not_useful": int(counts_payload.get("not_useful") or 0),
        }
        recent = [
            item
            for item in (feedback.get("recent") or [])[:3]
            if isinstance(item, dict) and item.get("rating")
        ]
        if counts["useful"] == 0 and counts["not_useful"] == 0 and not recent:
            return None

        count_bits: list[str] = []
        if counts["useful"]:
            count_bits.append(f"{counts['useful']} useful")
        if counts["not_useful"]:
            count_bits.append(f"{counts['not_useful']} not-useful")
        count_text = " and ".join(count_bits) if count_bits else "recent"
        signal_count = counts["useful"] + counts["not_useful"]
        summary = f"Source feedback available to this turn: {count_text} feedback signal{'s' if signal_count != 1 else ''}."
        if recent:
            first = recent[0]
            label = str(
                first.get("source_name") or first.get("title") or "source"
            ).strip()
            rating = str(first.get("rating") or "").replace("_", "-")
            note = str(first.get("note") or "").strip()
            if note:
                summary += f" Latest: {rating} feedback on {label}: {note[:180]}"
            else:
                summary += f" Latest: {rating} feedback on {label}."

        return {
            "counts": counts,
            "recent": recent,
            "summary": summary,
        }

    def _recovery_packet_context(self, packet_context: dict) -> dict:
        portfolio_context = packet_context.get("portfolio_context") or {}
        conversation_context = packet_context.get("conversation_context") or {}
        return {
            "analysis_mode": "reduced_context_recovery",
            "query_text": packet_context.get("query_text"),
            "subject_type": packet_context.get("subject_type"),
            "subject_id": str(packet_context.get("subject_id")),
            "subject_name": packet_context.get("subject_name"),
            "coverage": packet_context.get("coverage") or {},
            "gap_flags": (packet_context.get("gap_flags") or [])[:6],
            "counts": {
                "direct_evidence": packet_context.get(
                    "direct_evidence_count",
                    len(packet_context.get("direct_evidence") or []),
                ),
                "connected_evidence": packet_context.get(
                    "connected_evidence_count",
                    len(packet_context.get("connected_evidence") or []),
                ),
                "historical_evidence": packet_context.get(
                    "historical_evidence_count",
                    len(packet_context.get("historical_evidence") or []),
                ),
                "contradiction_evidence": packet_context.get(
                    "contradiction_evidence_count",
                    len(packet_context.get("contradiction_evidence") or []),
                ),
                "lessons": packet_context.get(
                    "lesson_count", len(packet_context.get("lessons") or [])
                ),
            },
            "direct_evidence": (packet_context.get("direct_evidence") or [])[:3],
            "connected_evidence": (packet_context.get("connected_evidence") or [])[:3],
            "historical_evidence": (packet_context.get("historical_evidence") or [])[
                :2
            ],
            "contradiction_evidence": (
                packet_context.get("contradiction_evidence") or []
            )[:3],
            "lessons": (packet_context.get("lessons") or [])[:3],
            "historical_analogies": (packet_context.get("historical_analogies") or [])[
                :2
            ],
            "historical_analogy_lenses": (
                packet_context.get("historical_analogy_lenses") or []
            )[:2],
            "fresh_research_context": packet_context.get("fresh_research_context")
            or {},
            "portfolio_context": {
                key: portfolio_context.get(key)
                for key in (
                    "tracked_positions",
                    "holdings_count",
                    "total_market_value",
                    "remaining_buying_power",
                    "pct_capital_deployed",
                    "top_holdings",
                    "subject_peer_exposures",
                    "peer_exposures",
                    "active_watchers",
                    "subject_watchers",
                    "fundamental_metrics",
                    "subject_fundamental_metrics",
                    "market_setup_signals",
                    "subject_market_setup_signals",
                    "trusted_sources",
                    "source_feedback",
                    "performance_attribution",
                )
                if key in portfolio_context
            },
            "conversation_context": (
                {
                    "subject_name": conversation_context.get("subject_name"),
                    "subject_type": conversation_context.get("subject_type"),
                    "recent_turns": (conversation_context.get("recent_turns") or [])[
                        -3:
                    ],
                }
                if conversation_context
                else {}
            ),
        }

    def _fallback_reasoning(
        self, packet_context: dict, *, failure_reason: str | None = None
    ) -> dict:
        query = (packet_context.get("query_text") or "").strip()
        subject_name = (
            packet_context.get("subject_name")
            or packet_context.get("subject_type")
            or "this subject"
        )
        subject_label = str(subject_name or "this subject").strip()
        direct = len(packet_context.get("direct_evidence") or [])
        connected = len(packet_context.get("connected_evidence") or [])
        historical = len(packet_context.get("historical_evidence") or [])
        contradiction = len(packet_context.get("contradiction_evidence") or [])
        gaps = packet_context.get("gap_flags") or []
        portfolio_context = packet_context.get("portfolio_context") or {}
        holdings = portfolio_context.get("top_holdings") or []
        performance_attribution = portfolio_context.get("performance_attribution")
        has_performance_attribution = isinstance(
            performance_attribution, dict
        ) and bool(performance_attribution.get("items"))
        holding_labels = [
            str(item.get("ticker") or item.get("name") or "").strip()
            for item in holdings[:4]
            if isinstance(item, dict)
            and str(item.get("ticker") or item.get("name") or "").strip()
        ]
        normalized_query = query.casefold()
        loss_question = any(
            term in normalized_query
            for term in (
                "lose",
                "losing",
                "lost",
                "loss",
                "drawdown",
                "down so much",
                "money lately",
            )
        )
        event_question = any(
            term in normalized_query
            for term in (
                "earnings",
                "report",
                "deal",
                "announcement",
                "today",
                "lately",
                "next week",
            )
        )

        if loss_question and has_performance_attribution:
            current_read = self._fallback_performance_attribution_context(
                performance_attribution
            )
            next_check = (
                "Best next check: investigate the largest measured contributors against dated company, sector, macro, and event evidence; "
                "keep competing explanations open until timing and cross-asset behavior discriminate among them."
            )
            strengthen = [
                "Dated source evidence that explains the largest measured contributors through a specific causal channel and market reaction.",
                "Independent corroboration showing whether the benchmark gap came from thesis failure, factor rotation, leverage decay, event reaction, or execution timing.",
            ]
            falsify = [
                "A refreshed transaction or corporate-action reconciliation materially changes the measured contribution ranking.",
                "Later evidence shows the proposed catalyst did not precede or transmit through the measured price move.",
            ]
        elif loss_question:
            current_read = (
                "Current read: this needs a source-backed loss attribution before it can become an investment conclusion. "
                f"The local evidence is only enough to frame {subject_label} as the working context."
            )
            next_check = (
                "Best next check: attribute P&L by holding, entry date, sector/benchmark move, and catalyst timing, then compare "
                "the largest losses against the stored thesis drivers."
            )
            strengthen = [
                "A source-backed P&L attribution that ties price moves to position weights, dates, benchmark/sector moves, and named catalysts.",
                "Fresh evidence showing whether the largest drawdowns came from thesis failure, factor rotation, event reaction, or stale price/account data.",
            ]
            falsify = [
                "Broker reconciliation shows the apparent loss is a cash-flow, split, stale-price, or synchronization artifact.",
                "Benchmark and sector moves explain most of the drawdown without a position-specific thesis break.",
            ]
        elif event_question:
            current_read = (
                "Current read: this is an event or recent-development question, so stale stored context is not enough by itself. "
                f"The local evidence can frame {subject_label}, but the answer still needs fresh, dated sources."
            )
            next_check = (
                "Best next check: compare the actual event against pre-event expectations, price reaction, estimate revisions, "
                "and portfolio exposure before changing any accepted thesis."
            )
            strengthen = [
                "Fresh primary or high-quality secondary evidence that states the event, timing, expectations, market reaction, and affected holdings.",
                "A dated bridge from the event to the relevant demand, supply, margin, valuation, financing, or timing channel.",
            ]
            falsify = [
                "The event is not real, is already stale, or has no measurable path to an active holding or thesis driver.",
                "Price and estimate reactions show the event was already discounted or not material to the portfolio exposure.",
            ]
        else:
            current_read = (
                "Current read: the stored packet is not strong enough to update the accepted thesis. "
                f"It can still frame the open question around {subject_label} and identify what evidence should be gathered next."
            )
            next_check = (
                "Best next check: turn the question into a falsifiable mechanism test with dated evidence, a measurable driver, "
                "and a portfolio transmission route."
            )
            strengthen = [
                "Direct, dated evidence for the named subject rather than adjacent portfolio context.",
                "A measurable bridge from the evidence to demand, supply, margins, valuation, financing, or timing.",
            ]
            falsify = [
                "New evidence shows the named driver is irrelevant, already priced in, or not connected to any active exposure.",
                "Contradictory evidence breaks the assumed portfolio transmission route.",
            ]

        local_bits = [current_read]
        evidence_line = f"Evidence state: direct {direct}, connected {connected}, historical {historical}, contradiction {contradiction}."
        if holding_labels:
            evidence_line += f" First exposures to map: {', '.join(holding_labels)}."
        local_bits.append(evidence_line)

        weak_points: list[str] = []
        if not any((direct, connected, historical, contradiction)):
            weak_points.append(
                "No direct or comparable evidence was retrieved for this exact question."
            )
        if gaps:
            weak_points.append(
                "Coverage gaps: " + ", ".join(str(gap) for gap in gaps[:4]) + "."
            )
        if loss_question and not has_performance_attribution:
            weak_points.append(
                "Missing attribution inputs: exact holdings, entry prices, fresh prices, benchmark/sector move, and event timing."
            )
        elif event_question:
            weak_points.append(
                "Missing current-event inputs: fresh source, timestamp, pre-event expectations, price reaction, and estimate revision path."
            )
        if weak_points:
            local_bits.append("Weak points in this read: " + " ".join(weak_points))

        watcher_bits = self._fallback_watcher_context(packet_context)
        if watcher_bits:
            local_bits.append(watcher_bits)
        market_setup_bits = self._fallback_market_setup_context(packet_context)
        if market_setup_bits:
            local_bits.append(market_setup_bits)
        metric_bits = self._fallback_fundamental_metric_context(packet_context)
        if metric_bits:
            local_bits.append(metric_bits)
        local_bits.append(next_check)
        result = {
            "stance": "no_view",
            "confidence_band": "very_low",
            "thesis_summary": "",
            "reasoning": "\n\n".join(local_bits),
            "what_would_falsify": falsify,
            "what_would_strengthen": strengthen,
            "supporting_evidence_ids": [],
            "contradicting_evidence_ids": [],
            "bull_case": None,
            "bear_case": None,
            "active_contradictions": [],
            "material_assertions": [],
            "assumptions": [],
            "alternative_hypotheses": [],
            "critique_text": "Fallback evidence frame used; treat as low-confidence until source-backed analysis succeeds.",
            "is_fallback": True,
            "fallback_reason": failure_reason or self._fallback_reason_label(),
        }
        return self._attach_source_feedback_influence(result, packet_context)

    def _timeout_fallback_reasoning(
        self, packet_context: dict, *, failure_reason: str | None = None
    ) -> dict:
        result = self._fallback_reasoning(
            packet_context,
            failure_reason=failure_reason or self._fallback_reason_label(),
        )
        result["critique_text"] = (
            "Timeout fallback evidence frame used; treat as low-confidence until source-backed analysis succeeds."
        )
        return result

    def _fallback_performance_attribution_context(self, attribution: dict) -> str:
        gain = float(attribution.get("gain") or 0.0)
        return_pct = attribution.get("return_pct")
        benchmark_return = attribution.get("benchmark_return_pct")
        benchmark_ticker = str(attribution.get("benchmark_ticker") or "benchmark")
        active_return = attribution.get("active_return_pct")
        period_start = str(attribution.get("period_start") or "")[:10]
        as_of = str(attribution.get("as_of") or "")[:10]
        items = [
            item for item in (attribution.get("items") or []) if isinstance(item, dict)
        ]
        drags = [item for item in items if float(item.get("gain") or 0.0) < 0][:4]
        drag_text = "; ".join(
            f"{item.get('ticker')} {float(item.get('gain') or 0.0):+,.2f} dollars"
            for item in drags
        )
        result = (
            f"Measured accounting read ({period_start} to {as_of}): the invested holdings "
            f"{'lost' if gain < 0 else 'gained'} ${abs(gain):,.2f}"
            + (
                f" ({float(return_pct):+.2f}%)"
                if isinstance(return_pct, (int, float))
                else ""
            )
            + "."
        )
        if isinstance(benchmark_return, (int, float)):
            result += f" {benchmark_ticker} returned {float(benchmark_return):+.2f}%"
            if isinstance(active_return, (int, float)):
                result += f", leaving {float(active_return):+.2f} percentage points of relative performance"
            result += "."
        if drag_text:
            result += f" Largest measured drags: {drag_text}."
        result += " This establishes what moved the book, not why; causal claims still require dated evidence."
        return result

    def _fallback_watcher_context(self, packet_context: dict) -> str | None:
        portfolio_context = packet_context.get("portfolio_context") or {}
        query = str(packet_context.get("query_text") or "").casefold()
        subject_name = str(packet_context.get("subject_name") or "").casefold()
        subject_watchers = [
            item
            for item in (portfolio_context.get("subject_watchers") or [])
            if isinstance(item, dict)
        ]
        all_watchers = [
            item
            for item in (portfolio_context.get("active_watchers") or [])
            if isinstance(item, dict)
        ]
        watcher_pool = subject_watchers or all_watchers
        if not watcher_pool:
            return None

        def relevant(watcher: dict) -> bool:
            ticker = str(watcher.get("ticker") or "").casefold()
            condition = (
                str(watcher.get("condition_type") or "").replace("_", " ").casefold()
            )
            objective = str(watcher.get("objective") or "").casefold()
            return bool(
                (ticker and (ticker in query or ticker in subject_name))
                or (condition and condition in query)
                or any(term in query for term in objective.split()[:4] if len(term) > 4)
            )

        selected = [watcher for watcher in watcher_pool if relevant(watcher)]
        if not selected:
            selected = watcher_pool[:2]

        parts: list[str] = []
        for watcher in selected[:3]:
            ticker = str(watcher.get("ticker") or "").strip()
            condition = str(watcher.get("condition_type") or "watch").replace("_", " ")
            objective = str(watcher.get("objective") or "").strip()
            adjustment = str(watcher.get("adjustment_plan") or "").strip()
            if not objective:
                continue
            label = f"{ticker} {condition}".strip()
            text = f"{label}: {objective}"
            if adjustment:
                text += f" Plan: {adjustment}"
            parts.append(text)

        if not parts:
            return None
        return "Active watch context already stored: " + " | ".join(parts) + "."

    def _fallback_market_setup_context(self, packet_context: dict) -> str | None:
        portfolio_context = packet_context.get("portfolio_context") or {}
        signals = [
            item
            for item in (
                (portfolio_context.get("subject_market_setup_signals") or [])
                + (portfolio_context.get("market_setup_signals") or [])
            )
            if isinstance(item, dict)
        ]
        if not signals:
            return None
        pieces: list[str] = []
        seen: set[str] = set()
        for signal in signals:
            key = str(signal.get("id") or signal.get("signal_name") or "")
            if key in seen:
                continue
            seen.add(key)
            name = str(signal.get("signal_name") or "market setup signal").strip()
            setup = str(signal.get("setup_context") or "").strip()
            actual = str(signal.get("actual_context") or "").strip()
            relevance = str(signal.get("investment_relevance") or "").strip()
            phrase = name
            if setup:
                phrase += f" setup: {setup[:180]}"
            if actual:
                phrase += f" actual: {actual[:180]}"
            if relevance:
                phrase += f" relevance: {relevance[:180]}"
            pieces.append(phrase)
            if len(pieces) >= 3:
                break
        if not pieces:
            return None
        return "Stored market setup context: " + " | ".join(pieces) + "."

    def _fallback_fundamental_metric_context(self, packet_context: dict) -> str | None:
        portfolio_context = packet_context.get("portfolio_context") or {}
        metrics = [
            item
            for item in (
                (portfolio_context.get("subject_fundamental_metrics") or [])
                + (portfolio_context.get("fundamental_metrics") or [])
            )
            if isinstance(item, dict)
        ]
        if not metrics:
            return None
        pieces: list[str] = []
        seen: set[str] = set()
        for metric in metrics:
            key = str(
                metric.get("id")
                or "|".join(
                    str(metric.get(part) or "")
                    for part in ("ticker", "metric_name", "period_label", "as_of")
                )
            )
            if key in seen:
                continue
            seen.add(key)
            name = str(metric.get("metric_name") or "fundamental metric").strip()
            family = str(metric.get("metric_family") or "").strip()
            ticker = str(metric.get("ticker") or "").strip()
            value = str(
                metric.get("value_text") or metric.get("numeric_value") or ""
            ).strip()
            period = str(
                metric.get("period_label") or metric.get("as_of") or ""
            ).strip()
            relevance = str(metric.get("investment_relevance") or "").strip()
            next_test = str(metric.get("next_test") or "").strip()
            label = f"{ticker} {name}".strip()
            phrase = label
            if family:
                phrase += f" ({family})"
            if value:
                phrase += f" value: {value[:120]}"
            if period:
                phrase += f" period/as-of: {period[:80]}"
            if relevance:
                phrase += f" relevance: {relevance[:180]}"
            if next_test:
                phrase += f" next test: {next_test[:140]}"
            pieces.append(phrase)
            if len(pieces) >= 4:
                break
        if not pieces:
            return None
        return "Stored fundamental metric context: " + " | ".join(pieces) + "."

    def _uuid_list(self, values: list[str]) -> list[UUID]:
        output: list[UUID] = []
        for value in values:
            try:
                output.append(UUID(str(value)))
            except ValueError:
                continue
        return output


def json_dumps(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=True, default=str)
