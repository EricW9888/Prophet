from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import desc, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from investos.config import settings
from investos.core.llm import call_llm_json
from investos.models.entity import Entity, Security
from investos.models.opportunity import (
    OpportunityCandidate,
    OpportunityDiscoveryRun,
    OpportunityUniverseMember,
)
from investos.schemas.opportunity import (
    OpportunityCandidateReview,
    OpportunityShadowTestRequest,
    OpportunityUniverseMemberCreate,
    OpportunityUniverseMemberUpdate,
)
from investos.schemas.shadow import ShadowExperimentCreate
from investos.services.research import ResearchService
from investos.services.runtime_settings import RuntimeSettingsStore
from investos.services.security_catalog import SecurityCatalogService
from investos.services.shadow import ShadowService

OPPORTUNITY_QUERY_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "query": {"type": "string"},
        "research_goal": {"type": "string"},
    },
    "required": ["query", "research_goal"],
}


class OpportunityDiscoveryService:
    """Bounded orchestration around research and the existing shadow analyst."""

    ACTIVE_KEY = "opportunity_discovery"
    ACTIVE_RUN_STALE_AFTER = timedelta(minutes=15)
    CANDIDATE_STATUSES = {
        "new",
        "monitoring",
        "rejected",
        "expired",
        "shadow_tested",
    }

    def __init__(self, session: AsyncSession):
        self.session = session
        self.runtime = RuntimeSettingsStore.load().opportunity_discovery

    async def list_universe(self) -> list[dict[str, Any]]:
        rows = (
            await self.session.execute(
                select(OpportunityUniverseMember, Security, Entity)
                .join(Security, OpportunityUniverseMember.security_id == Security.id)
                .join(Entity, OpportunityUniverseMember.entity_id == Entity.id)
                .order_by(
                    OpportunityUniverseMember.enabled.desc(),
                    OpportunityUniverseMember.priority.desc(),
                    Security.ticker,
                )
            )
        ).all()
        return [
            self._serialize_member(member, security=security, entity=entity)
            for member, security, entity in rows
        ]

    async def upsert_universe_member(
        self,
        payload: OpportunityUniverseMemberCreate,
    ) -> dict[str, Any]:
        security = await SecurityCatalogService(self.session).resolve_or_create_equity(
            ticker=payload.ticker,
            entity_name=payload.entity_name,
        )
        member = (
            await self.session.execute(
                select(OpportunityUniverseMember).where(
                    OpportunityUniverseMember.security_id == security.id
                )
            )
        ).scalar_one_or_none()
        if member is None:
            member = OpportunityUniverseMember(
                security_id=security.id,
                entity_id=security.entity_id,
                enabled=payload.enabled,
                priority=payload.priority,
                source="manual",
                metadata_json={},
            )
            self.session.add(member)
        else:
            member.enabled = payload.enabled
            member.priority = payload.priority
        await self.session.commit()
        await self.session.refresh(member)
        entity = security.entity or await self.session.get(Entity, security.entity_id)
        if entity is None:
            raise ValueError("Opportunity universe security has no entity.")
        return self._serialize_member(member, security=security, entity=entity)

    async def update_universe_member(
        self,
        member_id: UUID,
        payload: OpportunityUniverseMemberUpdate,
    ) -> dict[str, Any]:
        member = await self.session.get(OpportunityUniverseMember, member_id)
        if member is None:
            raise ValueError("Opportunity universe member not found.")
        if payload.priority is not None:
            member.priority = payload.priority
        if payload.enabled is not None:
            member.enabled = payload.enabled
        await self.session.commit()
        await self.session.refresh(member)
        security = await self.session.get(Security, member.security_id)
        entity = await self.session.get(Entity, member.entity_id)
        if security is None or entity is None:
            raise ValueError("Opportunity universe member is missing catalog state.")
        return self._serialize_member(member, security=security, entity=entity)

    async def remove_universe_member(self, member_id: UUID) -> None:
        member = await self.session.get(OpportunityUniverseMember, member_id)
        if member is None:
            raise ValueError("Opportunity universe member not found.")
        await self.session.delete(member)
        await self.session.commit()

    async def run_discovery(self) -> dict[str, Any]:
        now = datetime.now(UTC)
        run, acquired = await self._acquire_run(now=now)
        if not acquired:
            return {
                **self._serialize_run(run),
                "detail": "another_discovery_run_is_active",
            }

        try:
            members = await self._run_members(run)
            for member, security, entity in members:
                await self._inspect_member(
                    run=run,
                    member=member,
                    security=security,
                    entity=entity,
                )

            await self._evaluate_run(run=run, members=members)
            run.status = "completed" if run.failed_count == 0 else "partial"
            run.detail = (
                "opportunity_candidate_evaluated"
                if run.planned_count
                else "no_due_opportunity_subjects"
            )
        except Exception as exc:
            run.status = "failed"
            run.detail = f"{type(exc).__name__}: {str(exc)[:800]}"
            raise
        finally:
            run.active_key = None
            run.owner_token = None
            run.completed_at = datetime.now(UTC)
            run.heartbeat_at = run.completed_at
            await self.session.commit()
        return self._serialize_run(run)

    async def list_runs(self, *, limit: int = 20) -> list[dict[str, Any]]:
        rows = list(
            (
                await self.session.execute(
                    select(OpportunityDiscoveryRun)
                    .order_by(desc(OpportunityDiscoveryRun.started_at))
                    .limit(max(1, min(limit, 100)))
                )
            )
            .scalars()
            .all()
        )
        return [self._serialize_run(row) for row in rows]

    async def list_candidates(
        self,
        *,
        status: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        await self.expire_candidates()
        query = select(OpportunityCandidate).order_by(
            desc(OpportunityCandidate.priority_score),
            desc(OpportunityCandidate.last_seen_at),
        )
        if status:
            if status not in self.CANDIDATE_STATUSES:
                raise ValueError("Unknown opportunity candidate status.")
            query = query.where(OpportunityCandidate.status == status)
        rows = list(
            (await self.session.execute(query.limit(max(1, min(limit, 500)))))
            .scalars()
            .all()
        )
        return [self._serialize_candidate(row) for row in rows]

    async def review_candidate(
        self,
        candidate_id: UUID,
        payload: OpportunityCandidateReview,
    ) -> dict[str, Any]:
        candidate = await self.session.get(OpportunityCandidate, candidate_id)
        if candidate is None:
            raise ValueError("Opportunity candidate not found.")
        if candidate.status == "shadow_tested":
            raise ValueError(
                "A shadow-tested candidate cannot be moved back to review."
            )
        candidate.status = payload.status
        candidate.review_reason = self._clean_text(payload.reason, limit=1200) or None
        await self.session.commit()
        await self.session.refresh(candidate)
        return self._serialize_candidate(candidate)

    async def shadow_test_candidate(
        self,
        candidate_id: UUID,
        payload: OpportunityShadowTestRequest,
    ) -> tuple[dict[str, Any], UUID]:
        candidate = await self.session.get(OpportunityCandidate, candidate_id)
        if candidate is None:
            raise ValueError("Opportunity candidate not found.")
        if candidate.status in {"rejected", "expired"}:
            raise ValueError(
                "Rejected or expired candidates cannot start a shadow test."
            )
        if candidate.shadow_experiment_id is not None:
            return self._serialize_candidate(candidate), candidate.shadow_experiment_id

        profile = dict(candidate.discovery_profile_json or {})
        experiment = await ShadowService(self.session).create_experiment(
            ShadowExperimentCreate(
                name=candidate.title,
                policy_description=str(
                    profile.get("policy") or candidate.investable_thesis
                ),
                trigger_type="opportunity_queue",
                trigger_reason=str(profile.get("trigger_reason") or candidate.why_now),
                horizon_label=str(profile.get("horizon") or "adaptive"),
                initiated_by="opportunity_discovery",
                operator_prompt=profile.get("operator_prompt") or None,
                discovery_profile=profile,
                subject_refs=[
                    {
                        "subject_type": "entity",
                        "subject_id": str(candidate.entity_id),
                        "security_id": str(candidate.security_id),
                    }
                ],
                account_basis=payload.account_basis,
                starting_cash=payload.starting_cash,
                auto_run=True,
            )
        )
        candidate.shadow_experiment_id = experiment.id
        candidate.status = "shadow_tested"
        await self.session.commit()
        await self.session.refresh(candidate)
        return self._serialize_candidate(candidate), experiment.id

    async def expire_candidates(self) -> int:
        result = await self.session.execute(
            update(OpportunityCandidate)
            .where(
                OpportunityCandidate.status.in_(["new", "monitoring"]),
                OpportunityCandidate.expires_at <= datetime.now(UTC),
            )
            .values(status="expired")
        )
        if result.rowcount:
            await self.session.commit()
        return int(result.rowcount or 0)

    async def _acquire_run(
        self,
        *,
        now: datetime,
    ) -> tuple[OpportunityDiscoveryRun, bool]:
        owner_token = str(uuid4())
        universe_size = int(
            (
                await self.session.execute(
                    select(func.count(OpportunityUniverseMember.id)).where(
                        OpportunityUniverseMember.enabled.is_(True)
                    )
                )
            ).scalar_one()
        )
        insert_result = await self.session.execute(
            pg_insert(OpportunityDiscoveryRun)
            .values(
                status="running",
                active_key=self.ACTIVE_KEY,
                owner_token=owner_token,
                captured_at=now,
                started_at=now,
                heartbeat_at=now,
                universe_size=universe_size,
                limits_json={
                    "max_subjects": self.runtime.max_subjects_per_run,
                    "revisit_hours": self.runtime.revisit_hours,
                    "candidate_ttl_days": self.runtime.candidate_ttl_days,
                },
            )
            .on_conflict_do_nothing(index_elements=["active_key"])
            .returning(OpportunityDiscoveryRun.id)
        )
        inserted_id = insert_result.scalar_one_or_none()
        await self.session.commit()
        if inserted_id is not None:
            run = await self.session.get(OpportunityDiscoveryRun, inserted_id)
            if run is None:
                raise RuntimeError("Created opportunity discovery run was not found.")
            return run, True

        active = (
            await self.session.execute(
                select(OpportunityDiscoveryRun).where(
                    OpportunityDiscoveryRun.active_key == self.ACTIVE_KEY
                )
            )
        ).scalar_one()
        if active.heartbeat_at > now - self.ACTIVE_RUN_STALE_AFTER:
            return active, False

        claimed = await self.session.execute(
            update(OpportunityDiscoveryRun)
            .where(
                OpportunityDiscoveryRun.id == active.id,
                OpportunityDiscoveryRun.owner_token == active.owner_token,
                OpportunityDiscoveryRun.heartbeat_at == active.heartbeat_at,
            )
            .values(owner_token=owner_token, heartbeat_at=now)
            .returning(OpportunityDiscoveryRun.id)
        )
        claimed_id = claimed.scalar_one_or_none()
        await self.session.commit()
        if claimed_id is None:
            refreshed = await self.session.get(OpportunityDiscoveryRun, active.id)
            if refreshed is None:
                raise RuntimeError("Active opportunity discovery run disappeared.")
            return refreshed, False
        resumed = await self.session.get(OpportunityDiscoveryRun, claimed_id)
        if resumed is None:
            raise RuntimeError("Resumed opportunity discovery run was not found.")
        return resumed, True

    async def _run_members(
        self,
        run: OpportunityDiscoveryRun,
    ) -> list[tuple[OpportunityUniverseMember, Security, Entity]]:
        if run.remaining_member_ids_json:
            member_ids = [UUID(value) for value in run.remaining_member_ids_json]
            rows = (
                await self.session.execute(
                    select(OpportunityUniverseMember, Security, Entity)
                    .join(
                        Security, OpportunityUniverseMember.security_id == Security.id
                    )
                    .join(Entity, OpportunityUniverseMember.entity_id == Entity.id)
                    .where(OpportunityUniverseMember.id.in_(member_ids))
                    .order_by(
                        OpportunityUniverseMember.priority.desc(), Security.ticker
                    )
                )
            ).all()
            return list(rows)

        now = run.captured_at
        rows = (
            await self.session.execute(
                select(OpportunityUniverseMember, Security, Entity)
                .join(Security, OpportunityUniverseMember.security_id == Security.id)
                .join(Entity, OpportunityUniverseMember.entity_id == Entity.id)
                .where(
                    OpportunityUniverseMember.enabled.is_(True),
                    Security.is_active.is_(True),
                    or_(
                        OpportunityUniverseMember.next_inspection_at.is_(None),
                        OpportunityUniverseMember.next_inspection_at <= now,
                    ),
                )
                .order_by(
                    OpportunityUniverseMember.priority.desc(),
                    OpportunityUniverseMember.last_inspected_at.asc().nullsfirst(),
                    Security.ticker,
                )
                .limit(self.runtime.max_subjects_per_run)
            )
        ).all()
        run.planned_count = len(rows)
        run.remaining_member_ids_json = [str(member.id) for member, _, _ in rows]
        await self.session.commit()
        return list(rows)

    async def _inspect_member(
        self,
        *,
        run: OpportunityDiscoveryRun,
        member: OpportunityUniverseMember,
        security: Security,
        entity: Entity,
    ) -> None:
        inspected = False
        try:
            query, research_goal = await self._research_query(
                security=security,
                entity=entity,
            )
            result = await ResearchService(self.session).run_ad_hoc_request(
                query=query,
                title=f"Opportunity discovery: {security.ticker} · {entity.name}",
                metadata_json={
                    "requested_via": "opportunity_discovery",
                    "opportunity_discovery_run_id": str(run.id),
                    "opportunity_universe_member_id": str(member.id),
                    "subject_type": "entity",
                    "subject_id": str(entity.id),
                    "subject_name": entity.name,
                    "security_id": str(security.id),
                    "ticker": security.ticker,
                    "research_goal": research_goal,
                    "captured_at": run.captured_at.isoformat(),
                },
            )
            if result.reason in {
                "research_provider_not_configured",
                "research_provider_budget_exhausted",
            }:
                run.skipped_count += 1
                run.skipped_json = [
                    *(run.skipped_json or []),
                    {"member_id": str(member.id), "reason": result.reason},
                ]
            else:
                inspected = True
                run.inspected_count += 1
                run.inspected_member_ids_json = [
                    *(run.inspected_member_ids_json or []),
                    str(member.id),
                ]
        except Exception as exc:
            run.failed_count += 1
            run.failures_json = [
                *(run.failures_json or []),
                {
                    "member_id": str(member.id),
                    "ticker": security.ticker,
                    "error": f"{type(exc).__name__}: {str(exc)[:500]}",
                },
            ]
        finally:
            if inspected:
                member.last_inspected_at = datetime.now(UTC)
                member.next_inspection_at = member.last_inspected_at + timedelta(
                    hours=self.runtime.revisit_hours
                )
            run.remaining_member_ids_json = [
                value
                for value in (run.remaining_member_ids_json or [])
                if value != str(member.id)
            ]
            run.heartbeat_at = datetime.now(UTC)
            await self._refresh_provider_telemetry(run)
            await self.session.commit()

    async def _research_query(
        self,
        *,
        security: Security,
        entity: Entity,
    ) -> tuple[str, str]:
        result = await call_llm_json(
            system_prompt=(
                "Draft one neutral web source-discovery query for an investment analyst. "
                "The query should look for a recent material change in the named company's economics, expectations, risks, or opportunity without assuming a bullish or bearish conclusion. "
                "Choose the useful angle from the supplied company context; do not apply a fixed checklist. "
                "Use the company name or ticker so the query cannot drift to another subject."
            ),
            user_prompt=(
                f"Ticker: {security.ticker}\nCompany: {entity.name}\n"
                f"Sector: {entity.sector or 'unknown'}\nIndustry: {entity.industry or 'unknown'}"
            ),
            schema=OPPORTUNITY_QUERY_SCHEMA,
            timeout_seconds=settings.OPPORTUNITY_DISCOVERY_LLM_TIMEOUT_SECONDS,
        )
        query = self._clean_text(result.get("query"), limit=500)
        goal = self._clean_text(result.get("research_goal"), limit=800)
        query_terms = re.findall(r"[a-z0-9]+", query.casefold())
        entity_terms = re.findall(r"[a-z0-9]+", entity.name.casefold())
        normalized_query = " ".join(query_terms)
        normalized_entity = " ".join(entity_terms)
        names_subject = bool(
            security.ticker.casefold() in query_terms
            or (normalized_entity and normalized_entity in normalized_query)
        )
        if not query or not names_subject:
            query = f'"{entity.name}" {security.ticker} latest material investor update'
        if not goal:
            goal = "Find a dated, attributable change that could alter the investment premise."
        return query, goal

    async def _evaluate_run(
        self,
        *,
        run: OpportunityDiscoveryRun,
        members: list[tuple[OpportunityUniverseMember, Security, Entity]],
    ) -> None:
        if not members:
            return
        context = await ShadowService(self.session).build_discovery_context(
            captured_at=run.captured_at,
            additional_security_ids=[security.id for _, security, _ in members],
        )
        profile, actionable, reason = await ShadowService(
            self.session
        ).evaluate_discovery_context(context)
        if not actionable:
            run.skipped_count += 1
            run.skipped_json = [
                *(run.skipped_json or []),
                {"stage": "candidate_evaluation", "reason": reason or "declined"},
            ]
            return

        members_by_ticker = {
            security.ticker.strip().upper(): (member, security, entity)
            for member, security, entity in members
        }
        cited_tickers = [
            str(item.get("ticker") or "").strip().upper()
            for item in profile.get("evidence_snapshot") or []
        ]
        primary = next(
            (
                members_by_ticker[ticker]
                for ticker in cited_tickers
                if ticker in members_by_ticker
            ),
            None,
        )
        if primary is None:
            run.skipped_count += 1
            run.skipped_json = [
                *(run.skipped_json or []),
                {
                    "stage": "candidate_evaluation",
                    "reason": "candidate_has_no_cited_opportunity_universe_subject",
                },
            ]
            return
        _, security, entity = primary
        await self._upsert_candidate(
            run=run,
            security=security,
            entity=entity,
            profile=profile,
        )

    async def _upsert_candidate(
        self,
        *,
        run: OpportunityDiscoveryRun,
        security: Security,
        entity: Entity,
        profile: dict[str, Any],
    ) -> OpportunityCandidate:
        fingerprint = self._candidate_fingerprint(
            ticker=security.ticker,
            family_key=str(profile.get("family_key") or ""),
            thesis=str(profile.get("investable_thesis") or ""),
        )
        now = datetime.now(UTC)
        candidate = (
            await self.session.execute(
                select(OpportunityCandidate).where(
                    OpportunityCandidate.fingerprint == fingerprint
                )
            )
        ).scalar_one_or_none()
        values = {
            "run_id": run.id,
            "entity_id": entity.id,
            "security_id": security.id,
            "ticker": security.ticker.strip().upper(),
            "title": self._clean_text(profile.get("name"), limit=500),
            "family_key": self._clean_text(profile.get("family_key"), limit=240)
            or None,
            "priority_score": float(profile.get("priority_score") or 0.0),
            "signal_stage": self._clean_text(profile.get("signal_stage"), limit=240)
            or None,
            "why_now": self._clean_text(profile.get("why_now"), limit=1600),
            "investable_thesis": self._clean_text(
                profile.get("investable_thesis"), limit=2400
            ),
            "portfolio_transmission": self._clean_text(
                profile.get("portfolio_transmission"), limit=1600
            ),
            "expected_edge": self._clean_text(profile.get("expected_edge"), limit=1600),
            "falsification_tests_json": list(profile.get("falsification_tests") or []),
            "assumptions_json": list(profile.get("assumptions") or []),
            "uncertainties_json": list(profile.get("uncertainties") or []),
            "evidence_refs_json": list(profile.get("evidence_refs") or []),
            "evidence_snapshot_json": list(profile.get("evidence_snapshot") or []),
            "ranking_json": {
                "priority_score": float(profile.get("priority_score") or 0.0),
                "signal_stage": profile.get("signal_stage"),
                "priced_in_assessment": profile.get("priced_in_assessment"),
                "evidence_count": len(profile.get("evidence_refs") or []),
                "basis": "model_ranked_with_deterministic_source-reference_validation",
            },
            "discovery_profile_json": profile,
            "captured_at": run.captured_at,
            "last_seen_at": now,
            "expires_at": now + timedelta(days=self.runtime.candidate_ttl_days),
        }
        if candidate is None:
            candidate = OpportunityCandidate(
                fingerprint=fingerprint,
                status="new",
                first_seen_at=now,
                **values,
            )
            self.session.add(candidate)
        else:
            for field, value in values.items():
                setattr(candidate, field, value)
            if candidate.status == "expired":
                candidate.status = "new"
        await self.session.flush()
        return candidate

    async def _refresh_provider_telemetry(
        self,
        run: OpportunityDiscoveryRun,
    ) -> None:
        attempts = []
        for entry in ResearchService.recent_request_log(limit=2000):
            metadata = entry.get("metadata") or {}
            if metadata.get("opportunity_discovery_run_id") != str(run.id):
                continue
            attempts.append(
                {
                    "timestamp": entry.get("timestamp"),
                    "provider": entry.get("provider"),
                    "status": entry.get("status"),
                    "query": entry.get("query"),
                    "result_count": entry.get("result_count"),
                    "estimated_credits": int(entry.get("estimated_credits") or 0),
                    "fallback_reason": entry.get("fallback_reason"),
                }
            )
        attempts.sort(key=lambda item: str(item.get("timestamp") or ""))
        run.provider_attempts_json = attempts
        run.estimated_credits = sum(
            int(item.get("estimated_credits") or 0) for item in attempts
        )

    @staticmethod
    def _candidate_fingerprint(*, ticker: str, family_key: str, thesis: str) -> str:
        normalized = "|".join(
            [
                ticker.strip().upper(),
                " ".join(re.findall(r"[a-z0-9]+", family_key.casefold())),
                " ".join(re.findall(r"[a-z0-9]+", thesis.casefold())),
            ]
        )
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32]

    @staticmethod
    def _serialize_member(
        member: OpportunityUniverseMember,
        *,
        security: Security,
        entity: Entity,
    ) -> dict[str, Any]:
        return {
            "id": member.id,
            "security_id": security.id,
            "entity_id": entity.id,
            "ticker": security.ticker,
            "entity_name": entity.name,
            "enabled": member.enabled,
            "priority": float(member.priority or 0.0),
            "source": member.source,
            "last_inspected_at": member.last_inspected_at,
            "next_inspection_at": member.next_inspection_at,
            "created_at": member.created_at,
            "updated_at": member.updated_at,
        }

    @staticmethod
    def _serialize_run(run: OpportunityDiscoveryRun) -> dict[str, Any]:
        return {
            "id": run.id,
            "status": run.status,
            "captured_at": run.captured_at,
            "started_at": run.started_at,
            "completed_at": run.completed_at,
            "universe_size": run.universe_size,
            "planned_count": run.planned_count,
            "inspected_count": run.inspected_count,
            "skipped_count": run.skipped_count,
            "failed_count": run.failed_count,
            "estimated_credits": run.estimated_credits,
            "remaining_member_ids": list(run.remaining_member_ids_json or []),
            "inspected_member_ids": list(run.inspected_member_ids_json or []),
            "skipped": list(run.skipped_json or []),
            "failures": list(run.failures_json or []),
            "provider_attempts": list(run.provider_attempts_json or []),
            "limits": dict(run.limits_json or {}),
            "detail": run.detail,
        }

    @staticmethod
    def _serialize_candidate(candidate: OpportunityCandidate) -> dict[str, Any]:
        return {
            "id": candidate.id,
            "run_id": candidate.run_id,
            "entity_id": candidate.entity_id,
            "security_id": candidate.security_id,
            "shadow_experiment_id": candidate.shadow_experiment_id,
            "ticker": candidate.ticker,
            "status": candidate.status,
            "title": candidate.title,
            "family_key": candidate.family_key,
            "priority_score": float(candidate.priority_score or 0.0),
            "signal_stage": candidate.signal_stage,
            "why_now": candidate.why_now,
            "investable_thesis": candidate.investable_thesis,
            "portfolio_transmission": candidate.portfolio_transmission,
            "expected_edge": candidate.expected_edge,
            "falsification_tests": list(candidate.falsification_tests_json or []),
            "assumptions": list(candidate.assumptions_json or []),
            "uncertainties": list(candidate.uncertainties_json or []),
            "evidence_refs": list(candidate.evidence_refs_json or []),
            "evidence_snapshot": list(candidate.evidence_snapshot_json or []),
            "ranking": dict(candidate.ranking_json or {}),
            "review_reason": candidate.review_reason,
            "captured_at": candidate.captured_at,
            "first_seen_at": candidate.first_seen_at,
            "last_seen_at": candidate.last_seen_at,
            "expires_at": candidate.expires_at,
        }

    @staticmethod
    def _clean_text(value: Any, *, limit: int) -> str:
        return " ".join(str(value or "").split())[:limit].strip()
