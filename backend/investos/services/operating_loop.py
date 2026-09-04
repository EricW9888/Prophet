from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from investos.models.conclusion import ConclusionState
from investos.models.coverage import CoverageMap
from investos.models.entity import Entity, Security
from investos.models.portfolio import Position
from investos.models.verification import VerificationRun
from investos.schemas.shadow import ShadowExperimentCreate
from investos.services.canonical_state import CanonicalStateService
from investos.services.reasoning import ReasoningService
from investos.services.retrieval import RetrievalService
from investos.services.review import ReviewService
from investos.services.shadow import ShadowService
from investos.services.verification import VerificationRequest, VerificationService


class OperatingLoopService:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def refresh_subject(
        self,
        *,
        subject_id: UUID,
        subject_type: str,
        trigger_reason: str,
        subject_name: str | None = None,
        raw_evidence_id: UUID | None = None,
    ) -> dict[str, object]:
        subject_name = subject_name or await self._subject_name(
            subject_id, subject_type
        )
        previous_state = await self._conclusion_state(subject_id, subject_type)
        packet = await RetrievalService(self.session).retrieve_evidence(
            query=(
                f"Operating loop refresh for {subject_name}. "
                f"Trigger: {trigger_reason}. Refresh accepted state, surface contradictions, "
                "and identify what matters for the actual portfolio."
            ),
            subject_id=subject_id,
            subject_type=subject_type,
            max_depth=6,
        )
        reasoning_run, reasoning_result = await ReasoningService(
            self.session
        ).run_analysis(
            packet.id,
            include_critique=False,
        )
        current_state = await self._conclusion_state(subject_id, subject_type)
        coverage = await self._coverage_map(subject_id, subject_type)
        verification_summary = await self._maybe_run_verification(
            subject_id=subject_id,
            subject_type=subject_type,
            subject_name=subject_name,
            trigger_reason=trigger_reason,
            coverage=coverage,
        )
        current_state = await self._conclusion_state(subject_id, subject_type)
        shadow_summary = await self._maybe_trigger_shadow(
            subject_id=subject_id,
            subject_type=subject_type,
            subject_name=subject_name,
            trigger_reason=trigger_reason,
            previous_state=previous_state,
            current_state=current_state,
            coverage=coverage,
            raw_evidence_id=raw_evidence_id,
        )
        queue = await ReviewService(self.session).refresh_queue()
        return {
            "subject_id": str(subject_id),
            "subject_type": subject_type,
            "subject_name": subject_name,
            "reasoning_run_id": str(reasoning_run.id),
            "stance": reasoning_result.get("stance"),
            "confidence_band": reasoning_result.get("confidence_band"),
            "coverage_score": (
                None
                if coverage is None
                else float(coverage.overall_coverage_score or 0.0)
            ),
            "verification": verification_summary,
            "review_queue_items": len(queue),
            "shadow": shadow_summary,
        }

    async def _maybe_run_verification(
        self,
        *,
        subject_id: UUID,
        subject_type: str,
        subject_name: str,
        trigger_reason: str,
        coverage: CoverageMap | None,
    ) -> dict[str, object]:
        contradiction_count = (
            0.0 if coverage is None else float(coverage.contradiction_count or 0.0)
        )
        if contradiction_count < 2.0:
            return {
                "triggered": False,
                "reason": "contradiction_pressure_below_threshold",
            }

        cutoff = datetime.now(UTC) - timedelta(hours=12)
        recent = (
            (
                await self.session.execute(
                    select(VerificationRun)
                    .join(
                        ConclusionState,
                        VerificationRun.conclusion_state_id == ConclusionState.id,
                    )
                    .where(
                        ConclusionState.subject_id == subject_id,
                        ConclusionState.subject_type == subject_type,
                        VerificationRun.trigger == "contradiction_spike",
                        VerificationRun.verified_at >= cutoff,
                    )
                    .order_by(desc(VerificationRun.verified_at))
                    .limit(1)
                )
            )
            .scalars()
            .first()
        )
        if recent is not None:
            return {
                "triggered": False,
                "reason": "recent_verification_exists",
                "verification_id": str(recent.id),
            }

        verification = await VerificationService(self.session).run(
            VerificationRequest(
                subject_id=subject_id,
                subject_type=subject_type,
                trigger="contradiction_spike",
                challenge_text=(
                    f"Contradiction pressure increased for {subject_name} after {trigger_reason}. "
                    "Re-check higher-tier contradictory evidence, missing coverage, and whether the current stance still holds."
                ),
            )
        )
        return {
            "triggered": True,
            "reason": "verification_run",
            "verification_id": str(verification.id),
            "verified_stance": verification.verified_stance,
            "confidence_band": verification.confidence_band,
        }

    async def _maybe_trigger_shadow(
        self,
        *,
        subject_id: UUID,
        subject_type: str,
        subject_name: str,
        trigger_reason: str,
        previous_state: ConclusionState | None,
        current_state: ConclusionState | None,
        coverage: CoverageMap | None,
        raw_evidence_id: UUID | None,
    ) -> dict[str, object]:
        position, security = await self._active_position_context(
            subject_id, subject_type
        )
        if position is None or security is None or current_state is None:
            return {"triggered": False, "reason": "no_active_portfolio_position"}

        contradiction_count = (
            0.0 if coverage is None else float(coverage.contradiction_count or 0.0)
        )
        coverage_score = (
            0.0 if coverage is None else float(coverage.overall_coverage_score or 0.0)
        )
        stance_changed = previous_state is not None and (
            previous_state.current_stance != current_state.current_stance
            or previous_state.confidence_band != current_state.confidence_band
        )
        high_confidence = current_state.current_stance not in {
            "no_view",
            "uncertain",
        } and current_state.confidence_band in {
            "high",
            "very_high",
        }
        contradiction_spike = contradiction_count >= 2.0

        if not any([stance_changed, high_confidence, contradiction_spike]):
            return {"triggered": False, "reason": "trigger_threshold_not_met"}

        shadow_service = ShadowService(self.session)
        active = await shadow_service.find_subject_experiment(
            subject_type=subject_type,
            subject_id=subject_id,
            security_id=security.id,
        )
        triggers: list[str] = []
        if stance_changed:
            triggers.append("accepted_state_changed")
        if high_confidence:
            triggers.append("high_confidence_state")
        if contradiction_spike:
            triggers.append("contradiction_pressure")
        if active is not None:
            event = await shadow_service.queue_subject_evidence_event(
                experiment=active,
                subject_type=subject_type,
                subject_id=subject_id,
                security_id=security.id,
                trigger_reason=trigger_reason,
                raw_evidence_id=raw_evidence_id,
                metadata={
                    "triggers": triggers,
                    "stance": current_state.current_stance,
                    "confidence": current_state.confidence_band,
                    "coverage_score": coverage_score,
                    "contradictions": contradiction_count,
                },
            )
            return {
                "triggered": False,
                "reason": "active_shadow_woken_by_evidence",
                "experiment_id": str(active.id),
                "run_status": self._normalized_shadow_status(active.run_status),
                "triggers": triggers,
                "evidence_event": event,
            }

        recent = await shadow_service.find_subject_experiment(
            subject_type=subject_type,
            subject_id=subject_id,
            security_id=security.id,
            statuses={"completed"},
        )
        now = datetime.now(UTC)
        if (
            recent is not None
            and (now - recent.created_at) < timedelta(hours=24)
            and not stance_changed
            and not contradiction_spike
        ):
            return {
                "triggered": False,
                "reason": "recent_completed_shadow_still_covers_state",
                "experiment_id": str(recent.id),
            }

        policy = (
            f"Stress-test the current {security.ticker} thesis after {trigger_reason}. "
            "Compare immediate action, confirmation-seeking, and concentration-aware responses "
            "against the live portfolio baseline."
        )
        experiment = await shadow_service.create_experiment(
            ShadowExperimentCreate(
                name=f"Operating loop: {security.ticker}",
                policy_description=policy,
                trigger_type="operating_loop",
                trigger_reason=(
                    f"{trigger_reason}; triggers={','.join(triggers)}; "
                    f"stance={current_state.current_stance}; confidence={current_state.confidence_band}; "
                    f"coverage_score={coverage_score:.1f}; contradictions={contradiction_count:.0f}"
                ),
                horizon_label="adaptive",
                initiated_by="system",
                operator_prompt=(
                    f"Evaluate {security.ticker} against the real portfolio. "
                    "Test immediate action, wait-for-confirmation, and concentration-control variants. "
                    "Focus on whether the new accepted state justifies action now or patience."
                ),
                subject_refs=[
                    {
                        "subject_type": subject_type,
                        "subject_id": str(subject_id),
                        "security_id": str(security.id),
                    }
                ],
            )
        )
        return {
            "triggered": True,
            "reason": "auto_shadow_queued",
            "experiment_id": str(experiment.id),
            "run_status": experiment.run_status,
            "triggers": triggers,
            "subject_name": subject_name,
        }

    @staticmethod
    def _normalized_shadow_status(status: str | None) -> str:
        return ShadowService.normalize_run_status(status)

    async def _active_position_context(
        self,
        subject_id: UUID,
        subject_type: str,
    ) -> tuple[Position | None, Security | None]:
        if subject_type == "position":
            position = (
                await self.session.execute(
                    select(Position).where(Position.id == subject_id)
                )
            ).scalar_one_or_none()
            if position is None:
                return None, None
            security = (
                await self.session.execute(
                    select(Security).where(Security.id == position.security_id)
                )
            ).scalar_one_or_none()
            return position, security

        if subject_type != "entity":
            return None, None

        row = (
            await self.session.execute(
                select(Position, Security)
                .join(Security, Position.security_id == Security.id)
                .where(
                    Security.entity_id == subject_id,
                    Position.list_type == "holding",
                    Position.quantity > 0,
                )
                .order_by(desc(Position.market_value))
                .limit(1)
            )
        ).first()
        if row is None:
            return None, None
        return row[0], row[1]

    async def _subject_name(self, subject_id: UUID, subject_type: str) -> str:
        if subject_type == "entity":
            entity = (
                await self.session.execute(
                    select(Entity).where(Entity.id == subject_id)
                )
            ).scalar_one_or_none()
            return entity.name if entity is not None else str(subject_id)
        if subject_type == "position":
            row = (
                await self.session.execute(
                    select(Position, Security, Entity)
                    .join(Security, Position.security_id == Security.id)
                    .join(Entity, Security.entity_id == Entity.id)
                    .where(Position.id == subject_id)
                    .limit(1)
                )
            ).first()
            if row is None:
                return str(subject_id)
            position, security, entity = row
            return f"{security.ticker} · {entity.name}"
        return str(subject_id)

    async def _conclusion_state(
        self, subject_id: UUID, subject_type: str
    ) -> ConclusionState | None:
        return await CanonicalStateService(self.session).get_conclusion_state(
            subject_type=subject_type,
            subject_id=subject_id,
        )

    async def _coverage_map(
        self, subject_id: UUID, subject_type: str
    ) -> CoverageMap | None:
        return await CanonicalStateService(self.session).get_coverage_map(
            subject_type=subject_type,
            subject_id=subject_id,
        )
