from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import and_, desc, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from investos.config import settings
from investos.core.llm import call_llm_json
from investos.models.benchmark import Benchmark, BenchmarkConstituent
from investos.models.entity import Entity, Security
from investos.models.opportunity import (
    OpportunityCandidate,
    OpportunityCandidateObservation,
    OpportunityDiscoveryRun,
    OpportunityUniverseMember,
)
from investos.models.portfolio import Position
from investos.models.profile import Profile
from investos.schemas.opportunity import (
    OpportunityCandidateReview,
    OpportunityShadowTestRequest,
    OpportunityUniverseMemberCreate,
    OpportunityUniverseMemberUpdate,
)
from investos.schemas.shadow import ShadowExperimentCreate
from investos.services.market_data import MarketDataService
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
    UNIVERSE_IMPORT_SOURCES = {
        "tracked_positions": "Tracked positions",
        "researched_catalog": "Researched catalog",
        "benchmark_constituents": "Latest benchmark snapshots",
    }
    TRACKED_POSITION_LIST_TYPES = ("holding", "watchlist", "considering")
    ELIGIBLE_ASSET_CLASSES = {"equity", "etf"}
    DEFAULT_UNIVERSE_PRIORITY = 0.5

    @staticmethod
    def looks_like_discovery_request(text: str) -> bool:
        """Recognize explicit requests to canvass beyond the current holdings.

        This is a provider-outage fallback for capability routing. Candidate
        selection remains owned by the configured universe and discovery
        pipeline; this method never decides that an opportunity exists.
        """
        normalized = " ".join((text or "").casefold().split())
        if not normalized:
            return False
        target = r"(?:opportunit(?:y|ies)|investment ideas?|stock ideas?)"
        return bool(
            re.search(
                rf"\b(?:any|what|which|where are|are there)\b.{{0,60}}\b{target}\b",
                normalized,
            )
            or re.search(
                rf"\b(?:find|scan|surface|identify|show|canvass|look for)\b.{{0,80}}\b{target}\b",
                normalized,
            )
        )

    def __init__(self, session: AsyncSession):
        self.session = session
        self.runtime_settings = RuntimeSettingsStore.load()
        self.runtime = self.runtime_settings.opportunity_discovery
        self.market_data = MarketDataService(session)

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

    async def preview_universe_import(
        self,
        sources: list[str] | None = None,
    ) -> dict[str, Any]:
        """Preview additive universe construction from Prophet-owned durable state."""

        selected_sources = self._validated_import_sources(sources)
        captured_at = datetime.now(UTC)
        origins_by_security: dict[UUID, list[dict[str, Any]]] = defaultdict(list)
        catalog: dict[UUID, tuple[Security, Entity]] = {}
        skipped: list[dict[str, Any]] = []
        skipped_keys: set[tuple[str, UUID]] = set()

        def register(
            *,
            source_type: str,
            source_id: UUID,
            label: str,
            observed_at: datetime,
            security: Security,
            entity: Entity,
            metadata: dict[str, Any] | None = None,
        ) -> None:
            reason = self._universe_eligibility_reason(security)
            if reason is not None:
                skip_key = (source_type, security.id)
                if skip_key in skipped_keys:
                    return
                skipped_keys.add(skip_key)
                skipped.append(
                    {
                        "source_type": source_type,
                        "source_id": str(source_id),
                        "ticker": security.ticker,
                        "reason": reason,
                    }
                )
                return
            catalog[security.id] = (security, entity)
            origin = {
                "source_type": source_type,
                "source_id": str(source_id),
                "label": label,
                "observed_at": observed_at.isoformat(),
                "metadata": metadata or {},
            }
            existing_keys = {
                (item.get("source_type"), item.get("source_id"))
                for item in origins_by_security[security.id]
            }
            if (source_type, str(source_id)) not in existing_keys:
                origins_by_security[security.id].append(origin)

        if "tracked_positions" in selected_sources:
            rows = (
                await self.session.execute(
                    select(Position, Security, Entity)
                    .join(Security, Position.security_id == Security.id)
                    .join(Entity, Security.entity_id == Entity.id)
                    .where(Position.list_type.in_(self.TRACKED_POSITION_LIST_TYPES))
                )
            ).all()
            for position, security, entity in rows:
                register(
                    source_type="tracked_positions",
                    source_id=position.id,
                    label=f"{position.list_type.replace('_', ' ').title()} position",
                    observed_at=position.added_at,
                    security=security,
                    entity=entity,
                    metadata={"list_type": position.list_type},
                )

        if "researched_catalog" in selected_sources:
            ranked_profiles = (
                select(
                    Profile.id.label("profile_id"),
                    func.row_number()
                    .over(
                        partition_by=Profile.subject_id,
                        order_by=(
                            Profile.updated_at.desc(),
                            Profile.created_at.desc(),
                            Profile.id.desc(),
                        ),
                    )
                    .label("profile_rank"),
                )
                .where(Profile.subject_type == "entity")
                .subquery()
            )
            rows = (
                await self.session.execute(
                    select(Profile, Security, Entity)
                    .join(
                        ranked_profiles,
                        and_(
                            ranked_profiles.c.profile_id == Profile.id,
                            ranked_profiles.c.profile_rank == 1,
                        ),
                    )
                    .join(Entity, Profile.subject_id == Entity.id)
                    .join(Security, Security.entity_id == Entity.id)
                )
            ).all()
            for profile, security, entity in rows:
                register(
                    source_type="researched_catalog",
                    source_id=profile.id,
                    label="Entity research profile",
                    observed_at=profile.updated_at,
                    security=security,
                    entity=entity,
                    metadata={
                        "profile_version": profile.version,
                        "review_status": profile.review_status,
                    },
                )

        if "benchmark_constituents" in selected_sources:
            latest_snapshots = (
                select(
                    BenchmarkConstituent.benchmark_id.label("benchmark_id"),
                    func.max(BenchmarkConstituent.as_of_date).label("as_of_date"),
                )
                .group_by(BenchmarkConstituent.benchmark_id)
                .subquery()
            )
            rows = (
                await self.session.execute(
                    select(BenchmarkConstituent, Benchmark, Security, Entity)
                    .join(Benchmark, BenchmarkConstituent.benchmark_id == Benchmark.id)
                    .join(
                        latest_snapshots,
                        and_(
                            latest_snapshots.c.benchmark_id
                            == BenchmarkConstituent.benchmark_id,
                            latest_snapshots.c.as_of_date
                            == BenchmarkConstituent.as_of_date,
                        ),
                    )
                    .join(Security, BenchmarkConstituent.security_id == Security.id)
                    .join(Entity, Security.entity_id == Entity.id)
                )
            ).all()
            for constituent, benchmark, security, entity in rows:
                register(
                    source_type="benchmark_constituents",
                    source_id=constituent.id,
                    label=f"{benchmark.name} latest snapshot",
                    observed_at=constituent.as_of_date,
                    security=security,
                    entity=entity,
                    metadata={
                        "benchmark_id": str(benchmark.id),
                        "benchmark_ticker": benchmark.ticker,
                        "weight_pct": float(constituent.weight_pct),
                        "as_of_date": constituent.as_of_date.isoformat(),
                    },
                )

        existing_rows: list[OpportunityUniverseMember] = []
        if origins_by_security:
            existing_rows = list(
                (
                    await self.session.execute(
                        select(OpportunityUniverseMember).where(
                            OpportunityUniverseMember.security_id.in_(
                                list(origins_by_security)
                            )
                        )
                    )
                )
                .scalars()
                .all()
            )
        existing_security_ids = {row.security_id for row in existing_rows}
        candidates = []
        for security_id, origins in origins_by_security.items():
            security, entity = catalog[security_id]
            candidates.append(
                {
                    "security_id": security.id,
                    "entity_id": entity.id,
                    "ticker": security.ticker,
                    "entity_name": entity.name,
                    "asset_class": security.asset_class,
                    "instrument_type": security.instrument_type,
                    "status": (
                        "present" if security.id in existing_security_ids else "missing"
                    ),
                    "origins": sorted(
                        origins,
                        key=lambda item: (
                            str(item.get("source_type")),
                            str(item.get("label")),
                        ),
                    ),
                }
            )
        candidates.sort(key=lambda item: (item["ticker"], item["entity_name"]))

        summaries = []
        for source_type in selected_sources:
            source_candidates = [
                item
                for item in candidates
                if any(
                    origin.get("source_type") == source_type
                    for origin in item["origins"]
                )
            ]
            summaries.append(
                {
                    "source_type": source_type,
                    "label": self.UNIVERSE_IMPORT_SOURCES[source_type],
                    "eligible_count": len(source_candidates),
                    "missing_count": sum(
                        item["status"] == "missing" for item in source_candidates
                    ),
                    "existing_count": sum(
                        item["status"] == "present" for item in source_candidates
                    ),
                    "skipped_count": sum(
                        item.get("source_type") == source_type for item in skipped
                    ),
                }
            )
        return {
            "captured_at": captured_at,
            "source_summaries": summaries,
            "candidates": candidates,
            "skipped": skipped,
        }

    async def import_universe(self, sources: list[str]) -> dict[str, Any]:
        """Add eligible preview members without removing or reprioritizing state."""

        preview = await self.preview_universe_import(sources)
        security_ids = [candidate["security_id"] for candidate in preview["candidates"]]
        existing_rows: list[OpportunityUniverseMember] = []
        if security_ids:
            existing_rows = list(
                (
                    await self.session.execute(
                        select(OpportunityUniverseMember)
                        .where(OpportunityUniverseMember.security_id.in_(security_ids))
                        .with_for_update()
                    )
                )
                .scalars()
                .all()
            )
        members_by_security = {row.security_id: row for row in existing_rows}
        imported_count = 0
        provenance_updated_count = 0
        member_ids: list[UUID] = []
        for candidate in preview["candidates"]:
            security_id = candidate["security_id"]
            member = members_by_security.get(security_id)
            if member is None:
                inserted_id = (
                    await self.session.execute(
                        pg_insert(OpportunityUniverseMember)
                        .values(
                            security_id=security_id,
                            entity_id=candidate["entity_id"],
                            enabled=True,
                            priority=self.DEFAULT_UNIVERSE_PRIORITY,
                            source=candidate["origins"][0]["source_type"],
                            metadata_json={},
                        )
                        .on_conflict_do_nothing(
                            constraint="uq_opportunity_universe_members_security"
                        )
                        .returning(OpportunityUniverseMember.id)
                    )
                ).scalar_one_or_none()
                if inserted_id is not None:
                    member = await self.session.get(
                        OpportunityUniverseMember,
                        inserted_id,
                    )
                    imported_count += 1
                else:
                    member = (
                        await self.session.execute(
                            select(OpportunityUniverseMember)
                            .where(OpportunityUniverseMember.security_id == security_id)
                            .with_for_update()
                        )
                    ).scalar_one()
                if member is None:
                    raise RuntimeError(
                        "Opportunity universe upsert did not return a row."
                    )
                members_by_security[security_id] = member
            metadata = dict(member.metadata_json or {})
            merged_origins = self._merge_universe_origins(
                list(metadata.get("origins") or []),
                list(candidate["origins"]),
            )
            if merged_origins != list(metadata.get("origins") or []):
                metadata["origins"] = merged_origins
                metadata["last_origin_import_at"] = preview["captured_at"].isoformat()
                member.metadata_json = metadata
                provenance_updated_count += 1
            member_ids.append(member.id)
        await self.session.commit()
        return {
            "imported_count": imported_count,
            "existing_count": len(preview["candidates"]) - imported_count,
            "provenance_updated_count": provenance_updated_count,
            "member_ids": member_ids,
            "preview": preview,
        }

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
        await self.session.flush()
        metadata = dict(member.metadata_json or {})
        metadata["origins"] = self._merge_universe_origins(
            list(metadata.get("origins") or []),
            [
                {
                    "source_type": "manual",
                    "source_id": str(member.id),
                    "label": "Added by operator",
                    "observed_at": datetime.now(UTC).isoformat(),
                    "metadata": {},
                }
            ],
        )
        member.metadata_json = metadata
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
            outcome_summary = await self.evaluate_due_outcomes(
                now=run.captured_at,
                limit=self.runtime.max_subjects_per_run,
            )
            run.limits_json = {
                **(run.limits_json or {}),
                "outcome_evaluation": outcome_summary,
            }
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
        observations = await self._observations_by_candidate(
            [candidate.id for candidate in rows]
        )
        return [
            self._serialize_candidate(
                candidate,
                observations=observations.get(candidate.id, []),
            )
            for candidate in rows
        ]

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
        observations = await self._candidate_observations(candidate.id)
        return self._serialize_candidate(candidate, observations=observations)

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
            observations = await self._candidate_observations(candidate.id)
            return (
                self._serialize_candidate(candidate, observations=observations),
                candidate.shadow_experiment_id,
            )

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
        observations = await self._candidate_observations(candidate.id)
        return (
            self._serialize_candidate(candidate, observations=observations),
            experiment.id,
        )

    async def evaluate_due_outcomes(
        self,
        *,
        now: datetime | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """Anchor and evaluate immutable candidate observations without look-ahead."""

        as_of = now or datetime.now(UTC)
        if as_of.tzinfo is None:
            as_of = as_of.replace(tzinfo=UTC)
        batch_limit = max(
            1,
            min(int(limit or self.runtime.max_subjects_per_run), 100),
        )
        if not self.runtime_settings.market_data.enabled:
            return {
                "attempted": 0,
                "baselined": 0,
                "evaluated": 0,
                "pending": 0,
                "unavailable": 0,
                "failed": 0,
                "detail": "market_data_disabled",
            }
        if self.runtime_settings.market_data.provider != "yahoo_finance":
            return {
                "attempted": 0,
                "baselined": 0,
                "evaluated": 0,
                "pending": 0,
                "unavailable": 0,
                "failed": 0,
                "detail": "unsupported_market_data_provider",
            }

        observations = list(
            (
                await self.session.execute(
                    select(OpportunityCandidateObservation)
                    .where(OpportunityCandidateObservation.status == "pending")
                    .order_by(
                        OpportunityCandidateObservation.last_attempt_at.asc().nullsfirst(),
                        OpportunityCandidateObservation.due_at.asc(),
                        OpportunityCandidateObservation.captured_at.asc(),
                    )
                    .limit(batch_limit)
                )
            )
            .scalars()
            .all()
        )
        summary: dict[str, Any] = {
            "attempted": len(observations),
            "baselined": 0,
            "evaluated": 0,
            "pending": 0,
            "unavailable": 0,
            "failed": 0,
            "as_of": as_of.isoformat(),
            "settled_price_cutoff": as_of.date().isoformat(),
        }
        for observation in observations:
            had_baseline = observation.candidate_start_price is not None
            try:
                state = await self._evaluate_observation(
                    observation,
                    as_of=as_of,
                )
                if not had_baseline and observation.candidate_start_price is not None:
                    summary["baselined"] += 1
                if state == "evaluated":
                    summary["evaluated"] += 1
                elif state == "pending":
                    summary["pending"] += 1
                elif state == "unavailable":
                    summary["unavailable"] += 1
                else:
                    summary["failed"] += 1
            except Exception as exc:
                observation.attempt_count = int(observation.attempt_count or 0) + 1
                observation.last_attempt_at = as_of
                observation.last_error = f"{type(exc).__name__}: {str(exc)[:700]}"
                summary["failed"] += 1
        await self.session.commit()
        return summary

    async def _evaluate_observation(
        self,
        observation: OpportunityCandidateObservation,
        *,
        as_of: datetime,
    ) -> str:
        observation.attempt_count = int(observation.attempt_count or 0) + 1
        observation.last_attempt_at = as_of
        settled_cutoff = datetime(as_of.year, as_of.month, as_of.day, tzinfo=UTC)
        if settled_cutoff <= observation.captured_at:
            observation.last_error = None
            return "pending"

        period_start = observation.captured_at - timedelta(days=7)
        tickers = {observation.ticker, observation.benchmark_ticker}
        charts: dict[str, dict[str, object]] = {}
        for ticker in sorted(tickers):
            charts[ticker] = await self.market_data.fetch_chart_series(
                ticker,
                period_start=period_start,
                period_end=settled_cutoff,
            )
        candidate_prices = self._settled_prices(
            charts.get(observation.ticker) or {},
            as_of=as_of,
        )
        benchmark_prices = self._settled_prices(
            charts.get(observation.benchmark_ticker) or {},
            as_of=as_of,
        )
        shared_dates = sorted(set(candidate_prices) & set(benchmark_prices))
        baseline_date = next(
            (
                price_date
                for price_date in shared_dates
                if price_date > observation.captured_at.date()
            ),
            None,
        )
        if observation.candidate_start_price is None:
            if baseline_date is None:
                observation.last_error = "no_shared_settled_baseline_close"
                return "unavailable"
            candidate_time, candidate_price = candidate_prices[baseline_date]
            benchmark_time, benchmark_price = benchmark_prices[baseline_date]
            observation.candidate_start_time = candidate_time
            observation.candidate_start_price = candidate_price
            observation.benchmark_start_time = benchmark_time
            observation.benchmark_start_price = benchmark_price

        observation.evaluation_policy_json = {
            **(observation.evaluation_policy_json or {}),
            "data_fetched_at": as_of.isoformat(),
            "settled_price_cutoff": as_of.date().isoformat(),
            "market_control": observation.benchmark_ticker,
            "cash_control_return_pct": 0.0,
            "provider_revision_risk": (
                "Historical provider data can be corrected after this snapshot; "
                "stored prices are not rewritten automatically."
            ),
        }
        if as_of < observation.due_at:
            observation.last_error = None
            return "pending"

        end_date = next(
            (
                price_date
                for price_date in shared_dates
                if price_date >= observation.due_at.date()
                and observation.candidate_start_time is not None
                and price_date > observation.candidate_start_time.date()
            ),
            None,
        )
        if end_date is None:
            observation.last_error = "no_shared_settled_outcome_close_yet"
            return "unavailable"

        candidate_end_time, candidate_end_price = candidate_prices[end_date]
        benchmark_end_time, benchmark_end_price = benchmark_prices[end_date]
        candidate_start_price = float(observation.candidate_start_price or 0.0)
        benchmark_start_price = float(observation.benchmark_start_price or 0.0)
        if candidate_start_price <= 0 or benchmark_start_price <= 0:
            observation.last_error = "non_positive_baseline_price"
            return "unavailable"

        candidate_return = ((candidate_end_price / candidate_start_price) - 1.0) * 100
        benchmark_return = ((benchmark_end_price / benchmark_start_price) - 1.0) * 100
        excess_return = candidate_return - benchmark_return
        observation.candidate_end_time = candidate_end_time
        observation.candidate_end_price = candidate_end_price
        observation.benchmark_end_time = benchmark_end_time
        observation.benchmark_end_price = benchmark_end_price
        observation.candidate_return_pct = candidate_return
        observation.benchmark_return_pct = benchmark_return
        observation.excess_return_pct = excess_return
        observation.cash_return_pct = 0.0
        observation.result_label = self._outcome_label(
            expected_direction=observation.expected_relative_direction,
            excess_return_pct=excess_return,
        )
        observation.evaluated_at = as_of
        observation.status = "evaluated"
        observation.last_error = None
        return "evaluated"

    @staticmethod
    def _settled_prices(
        chart: dict[str, object],
        *,
        as_of: datetime,
    ) -> dict[date, tuple[datetime, float]]:
        source = list(chart.get("adjusted_series") or [])
        prices: dict[date, tuple[datetime, float]] = {}
        for timestamp, raw_price in source:
            if not isinstance(timestamp, datetime) or timestamp.date() >= as_of.date():
                continue
            try:
                price = float(raw_price)
            except (TypeError, ValueError):
                continue
            if price <= 0:
                continue
            prices[timestamp.date()] = (timestamp, price)
        return prices

    @staticmethod
    def _outcome_label(
        *,
        expected_direction: str,
        excess_return_pct: float,
    ) -> str:
        if expected_direction not in {"outperform", "underperform"}:
            return "direction_unrecorded"
        if excess_return_pct == 0:
            return "inconclusive"
        supported = (
            excess_return_pct > 0
            if expected_direction == "outperform"
            else excess_return_pct < 0
        )
        return "supported" if supported else "challenged"

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
        await self._record_candidate_observation(
            candidate=candidate,
            run=run,
            profile=profile,
        )
        return candidate

    async def _record_candidate_observation(
        self,
        *,
        candidate: OpportunityCandidate,
        run: OpportunityDiscoveryRun,
        profile: dict[str, Any],
    ) -> None:
        horizon_label = (
            self._clean_text(profile.get("horizon"), limit=120) or "adaptive"
        )
        horizon_days = ShadowService._profile_horizon_days(profile)
        if horizon_days is None:
            horizon_days = ShadowService._horizon_days(horizon_label)
        benchmark_ticker = (
            self.runtime_settings.portfolio.default_benchmark_ticker.strip().upper()
        )
        await self.session.execute(
            pg_insert(OpportunityCandidateObservation)
            .values(
                candidate_id=candidate.id,
                run_id=run.id,
                security_id=candidate.security_id,
                ticker=candidate.ticker,
                captured_at=run.captured_at,
                horizon_label=horizon_label,
                horizon_days=horizon_days,
                due_at=run.captured_at + timedelta(days=horizon_days),
                expected_relative_direction=self._clean_text(
                    profile.get("expected_relative_direction"),
                    limit=40,
                ).lower()
                or "unscored",
                status="pending",
                profile_snapshot_json=dict(profile),
                evidence_refs_json=list(profile.get("evidence_refs") or []),
                evidence_snapshot_json=list(profile.get("evidence_snapshot") or []),
                benchmark_ticker=benchmark_ticker,
                market_data_provider=self.runtime_settings.market_data.provider,
                evaluation_policy_json={
                    "baseline": (
                        "first shared adjusted daily close strictly after the "
                        "candidate capture date"
                    ),
                    "outcome": (
                        "first shared adjusted daily close on or after the fixed "
                        "due date"
                    ),
                    "look_ahead_guard": (
                        "only price dates strictly before the evaluator UTC date "
                        "are eligible"
                    ),
                    "market_control": benchmark_ticker,
                    "cash_control_return_pct": 0.0,
                },
            )
            .on_conflict_do_nothing(
                constraint="uq_opportunity_candidate_observations_candidate_run"
            )
        )

    async def _candidate_observations(
        self,
        candidate_id: UUID,
        *,
        limit: int = 12,
    ) -> list[OpportunityCandidateObservation]:
        return list(
            (
                await self.session.execute(
                    select(OpportunityCandidateObservation)
                    .where(OpportunityCandidateObservation.candidate_id == candidate_id)
                    .order_by(OpportunityCandidateObservation.captured_at.desc())
                    .limit(max(1, min(limit, 50)))
                )
            )
            .scalars()
            .all()
        )

    async def _observations_by_candidate(
        self,
        candidate_ids: list[UUID],
    ) -> dict[UUID, list[OpportunityCandidateObservation]]:
        if not candidate_ids:
            return {}
        rows = list(
            (
                await self.session.execute(
                    select(OpportunityCandidateObservation)
                    .where(
                        OpportunityCandidateObservation.candidate_id.in_(candidate_ids)
                    )
                    .order_by(OpportunityCandidateObservation.captured_at.desc())
                )
            )
            .scalars()
            .all()
        )
        grouped: dict[UUID, list[OpportunityCandidateObservation]] = defaultdict(list)
        for observation in rows:
            if len(grouped[observation.candidate_id]) < 12:
                grouped[observation.candidate_id].append(observation)
        return dict(grouped)

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

    @classmethod
    def _validated_import_sources(cls, sources: list[str] | None) -> list[str]:
        selected = list(cls.UNIVERSE_IMPORT_SOURCES if sources is None else sources)
        if not selected:
            raise ValueError("Select at least one opportunity universe source.")
        unknown = sorted(set(selected) - set(cls.UNIVERSE_IMPORT_SOURCES))
        if unknown:
            raise ValueError(
                f"Unknown opportunity universe sources: {', '.join(unknown)}"
            )
        return [source for source in cls.UNIVERSE_IMPORT_SOURCES if source in selected]

    @classmethod
    def _universe_eligibility_reason(cls, security: Security) -> str | None:
        if not (security.ticker or "").strip():
            return "missing_ticker"
        if not security.is_active or security.delisted_at is not None:
            return "inactive_or_delisted_security"
        if security.asset_class not in cls.ELIGIBLE_ASSET_CLASSES:
            return "unsupported_asset_class"
        return None

    @staticmethod
    def _merge_universe_origins(
        existing: list[dict[str, Any]],
        incoming: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        by_key: dict[tuple[str, str], dict[str, Any]] = {}
        for origin in [*existing, *incoming]:
            source_type = str(origin.get("source_type") or "").strip()
            source_id = str(origin.get("source_id") or "").strip()
            if not source_type or not source_id:
                continue
            by_key[(source_type, source_id)] = dict(origin)
        return sorted(
            by_key.values(),
            key=lambda item: (
                str(item.get("source_type")),
                str(item.get("label")),
                str(item.get("source_id")),
            ),
        )

    @staticmethod
    def _serialize_member(
        member: OpportunityUniverseMember,
        *,
        security: Security,
        entity: Entity,
    ) -> dict[str, Any]:
        origins = list((member.metadata_json or {}).get("origins") or [])
        if not any(origin.get("source_type") == member.source for origin in origins):
            origins = OpportunityDiscoveryService._merge_universe_origins(
                origins,
                [
                    {
                        "source_type": member.source,
                        "source_id": str(member.id),
                        "label": "Original universe source",
                        "observed_at": member.created_at.isoformat(),
                        "metadata": {},
                    }
                ],
            )
        return {
            "id": member.id,
            "security_id": security.id,
            "entity_id": entity.id,
            "ticker": security.ticker,
            "entity_name": entity.name,
            "enabled": member.enabled,
            "priority": float(member.priority or 0.0),
            "source": member.source,
            "origins": origins,
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
    def _serialize_candidate(
        candidate: OpportunityCandidate,
        *,
        observations: list[OpportunityCandidateObservation] | None = None,
    ) -> dict[str, Any]:
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
            "observations": [
                OpportunityDiscoveryService._serialize_observation(observation)
                for observation in observations or []
            ],
        }

    @staticmethod
    def _serialize_observation(
        observation: OpportunityCandidateObservation,
    ) -> dict[str, Any]:
        return {
            "id": observation.id,
            "run_id": observation.run_id,
            "captured_at": observation.captured_at,
            "horizon_label": observation.horizon_label,
            "horizon_days": observation.horizon_days,
            "due_at": observation.due_at,
            "expected_relative_direction": observation.expected_relative_direction,
            "status": observation.status,
            "profile_snapshot": dict(observation.profile_snapshot_json or {}),
            "evidence_refs": list(observation.evidence_refs_json or []),
            "evidence_snapshot": list(observation.evidence_snapshot_json or []),
            "benchmark_ticker": observation.benchmark_ticker,
            "market_data_provider": observation.market_data_provider,
            "candidate_start_time": observation.candidate_start_time,
            "candidate_start_price": observation.candidate_start_price,
            "benchmark_start_time": observation.benchmark_start_time,
            "benchmark_start_price": observation.benchmark_start_price,
            "evaluated_at": observation.evaluated_at,
            "candidate_end_time": observation.candidate_end_time,
            "candidate_end_price": observation.candidate_end_price,
            "benchmark_end_time": observation.benchmark_end_time,
            "benchmark_end_price": observation.benchmark_end_price,
            "candidate_return_pct": observation.candidate_return_pct,
            "benchmark_return_pct": observation.benchmark_return_pct,
            "excess_return_pct": observation.excess_return_pct,
            "cash_return_pct": observation.cash_return_pct,
            "result_label": observation.result_label,
            "attempt_count": observation.attempt_count,
            "last_attempt_at": observation.last_attempt_at,
            "last_error": observation.last_error,
            "evaluation_policy": dict(observation.evaluation_policy_json or {}),
        }

    @staticmethod
    def _clean_text(value: Any, *, limit: int) -> str:
        return " ".join(str(value or "").split())[:limit].strip()
