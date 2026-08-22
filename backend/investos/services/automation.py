from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Callable

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import desc, select

from investos.config import settings
from investos.db import async_session_maker
from investos.models.coverage import CoverageMap, UnresolvedQuestion
from investos.models.entity import Entity, Security
from investos.models.evidence import RawEvidence
from investos.models.portfolio import Position
from investos.models.review import ReviewQueueItem
from investos.models.theme import Theme
from investos.services.agent import AgentService
from investos.services.agent_action_log import AgentActionLogService
from investos.services.artifact_hygiene import (
    is_artifact_question_text,
    is_artifact_subject_name,
)
from investos.services.brokerage import BrokerageService
from investos.services.canonical_state import CanonicalStateService
from investos.services.database_backup import DatabaseBackupService
from investos.services.entity_hygiene import EntityHygieneService
from investos.services.fundamentals import FundamentalMetricService
from investos.services.integrity import IntegrityService
from investos.services.investment_object_backfill import InvestmentObjectBackfillService
from investos.services.market_data import MarketDataService
from investos.services.market_setup import MarketSetupSignalService
from investos.services.media_workspace import MediaIngestionPolicy
from investos.services.pattern_discovery import PatternDiscoveryService
from investos.services.relation_review import RelationReviewService
from investos.services.research import ResearchService
from investos.services.review import ReviewService
from investos.services.risk import RiskService
from investos.services.runtime_settings import RuntimeSettingsStore
from investos.services.shadow import ShadowService
from investos.services.source import SourceService
from investos.services.theme_hygiene import ThemeHygieneService
from investos.workers.extraction import ExtractionWorker


@dataclass
class JobTelemetry:
    name: str
    interval_seconds: int
    enabled: bool = True
    last_run_at: datetime | None = None
    last_status: str = "idle"
    detail: str | None = None


class AutomationCoordinator:
    def __init__(self) -> None:
        self.scheduler = AsyncIOScheduler(
            timezone="UTC",
            job_defaults={
                "coalesce": True,
                "max_instances": 1,
                "misfire_grace_time": 60,
            },
        )
        self.telemetry: dict[str, JobTelemetry] = {}
        self._shutting_down = False

    def start(self) -> None:
        if not settings.AUTOMATION_ENABLED:
            return
        self._shutting_down = False
        self._register_job(
            "database_backup", self._run_database_backup, 86400, settings.BACKUP_ENABLED
        )
        self._register_job("integrity_audit", self._run_integrity_audit, 3600, True)
        self._register_job(
            "research_loop",
            self._run_research_loop,
            settings.AUTOMATION_POLL_SECONDS,
            True,
        )
        self._register_job(
            "question_resolution", self._run_question_resolution, 3600, True
        )
        self._register_job("relation_review", self._run_relation_review, 1800, True)
        self._register_job("agent_reflection", self._run_agent_reflection, 900, True)
        self._register_job(
            "shadow_refresh",
            self._run_shadow_refresh,
            max(60, settings.SHADOW_REFRESH_INTERVAL_SECONDS),
            True,
        )
        self._register_job(
            "evidence_processing", self._run_evidence_processing, 120, True
        )
        self._register_job("strategist_cycle", self._run_strategist_cycle, 3600, True)
        self._register_job(
            "pattern_discovery",
            self._run_pattern_discovery,
            settings.PATTERN_DISCOVERY_INTERVAL_SECONDS,
            settings.PATTERN_DISCOVERY_ENABLED,
        )
        self._register_job("shadow_discovery", self._run_shadow_discovery, 21600, True)
        self._register_job(
            "watcher_loop",
            self._run_watcher_loop,
            settings.AUTOMATION_POLL_SECONDS or 300,
            True,
        )
        self._register_job("entity_hygiene", self._run_entity_hygiene, 21600, True)
        self._register_job("theme_hygiene", self._run_theme_hygiene, 21600, True)
        self._register_job("media_cleanup", self._run_media_cleanup, 21600, True)
        self._register_job(
            "source_claim_assessment",
            self._run_source_claim_assessment,
            settings.SOURCE_CLAIM_ASSESSMENT_INTERVAL_SECONDS,
            True,
        )
        self._register_job(
            "market_setup_assessment",
            self._run_market_setup_assessment,
            settings.MARKET_SETUP_ASSESSMENT_INTERVAL_SECONDS,
            True,
        )
        self._register_job(
            "fundamental_freshness", self._run_fundamental_freshness, 21600, True
        )
        self._register_job(
            "investment_object_backfill",
            self._run_investment_object_backfill,
            settings.INVESTMENT_OBJECT_BACKFILL_INTERVAL_SECONDS,
            settings.INVESTMENT_OBJECT_BACKFILL_ENABLED,
        )

        self._register_job(
            "market_data_refresh",
            self._run_market_data_refresh,
            settings.MARKET_DATA_REFRESH_SECONDS,
            False,
        )
        self._register_job(
            "risk_refresh",
            self._run_risk_refresh,
            max(settings.MARKET_DATA_REFRESH_SECONDS, 300),
            False,
        )
        self._register_job("gmail_sync", self._run_gmail_sync, 86400, False)
        self._register_job(
            "brokerage_reconcile", self._run_brokerage_reconcile, 21600, False
        )
        self.scheduler.start()
        self.sync_runtime_jobs()
        self._schedule_startup_run("integrity_audit", self._run_integrity_audit)
        self._schedule_startup_run("research_loop", self._run_research_loop)
        self._schedule_startup_run("evidence_processing", self._run_evidence_processing)
        self._schedule_startup_run("market_data_refresh", self._run_market_data_refresh)
        self._schedule_startup_run("risk_refresh", self._run_risk_refresh)
        self._schedule_startup_run("gmail_sync", self._run_gmail_sync)
        catchup_jobs = [
            ("source_claim_assessment", self._run_source_claim_assessment),
            ("market_setup_assessment", self._run_market_setup_assessment),
            ("fundamental_freshness", self._run_fundamental_freshness),
            ("investment_object_backfill", self._run_investment_object_backfill),
            ("pattern_discovery", self._run_pattern_discovery),
        ]
        self._schedule_startup_sequence(
            catchup_jobs,
            delay_seconds=settings.AUTOMATION_STARTUP_CATCHUP_DELAY_SECONDS,
        )

    async def shutdown(self) -> None:
        self._shutting_down = True
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)

    def status(self) -> list[JobTelemetry]:
        self.sync_runtime_jobs()
        return list(self.telemetry.values())

    def reset_state(self) -> None:
        for telemetry in self.telemetry.values():
            telemetry.last_run_at = None
            telemetry.detail = None
            telemetry.last_status = "idle" if telemetry.enabled else "disabled"
        self.sync_runtime_jobs()

    @staticmethod
    def _result_telemetry_status(result: dict) -> str:
        status = str(result.get("status") or "ok").strip().lower()
        if status in {"error", "warning", "waiting_for_config", "cancelled"}:
            return status
        return "ok"

    async def run_job(self, name: str) -> None:
        self.sync_runtime_jobs()
        mapping = {
            "database_backup": self._run_database_backup,
            "integrity_audit": self._run_integrity_audit,
            "market_data_refresh": self._run_market_data_refresh,
            "risk_refresh": self._run_risk_refresh,
            "evidence_processing": self._run_evidence_processing,
            "research_loop": self._run_research_loop,
            "question_resolution": self._run_question_resolution,
            "relation_review": self._run_relation_review,
            "agent_reflection": self._run_agent_reflection,
            "shadow_refresh": self._run_shadow_refresh,
            "shadow_discovery": self._run_shadow_discovery,
            "gmail_sync": self._run_gmail_sync,
            "brokerage_reconcile": self._run_brokerage_reconcile,
            "strategist_cycle": self._run_strategist_cycle,
            "pattern_discovery": self._run_pattern_discovery,
            "watcher_loop": self._run_watcher_loop,
            "entity_hygiene": self._run_entity_hygiene,
            "theme_hygiene": self._run_theme_hygiene,
            "media_cleanup": self._run_media_cleanup,
            "source_claim_assessment": self._run_source_claim_assessment,
            "market_setup_assessment": self._run_market_setup_assessment,
            "fundamental_freshness": self._run_fundamental_freshness,
            "investment_object_backfill": self._run_investment_object_backfill,
        }
        if name not in mapping:
            raise ValueError(f"Unknown job: {name}")
        await mapping[name]()

    def _register_job(
        self,
        name: str,
        func: Callable[[], object],
        interval_seconds: int,
        enabled: bool,
    ) -> None:
        self.telemetry[name] = JobTelemetry(
            name=name,
            interval_seconds=interval_seconds,
            enabled=enabled,
            last_status="disabled" if not enabled else "idle",
        )
        if enabled:
            self.scheduler.add_job(
                self._scheduled_job(name, func),
                "interval",
                seconds=interval_seconds,
                id=name,
                replace_existing=True,
            )

    def _schedule_startup_run(
        self,
        name: str,
        func: Callable[[], object],
        *,
        delay_seconds: int = 0,
    ) -> None:
        telemetry = self.telemetry.get(name)
        if telemetry is None or not telemetry.enabled:
            return
        self.scheduler.add_job(
            self._scheduled_job(name, func),
            "date",
            run_date=datetime.now(UTC)
            + timedelta(seconds=max(0, int(delay_seconds or 0))),
            id=f"{name}_startup",
            replace_existing=True,
        )

    def _schedule_startup_sequence(
        self,
        jobs: list[tuple[str, Callable[[], object]]],
        *,
        delay_seconds: int = 0,
    ) -> None:
        enabled_jobs = [
            (name, func)
            for name, func in jobs
            if self.telemetry.get(name) is not None and self.telemetry[name].enabled
        ]
        if not enabled_jobs:
            return

        async def runner() -> None:
            for name, func in enabled_jobs:
                await self._scheduled_job(name, func)()

        self.scheduler.add_job(
            runner,
            "date",
            run_date=datetime.now(UTC)
            + timedelta(seconds=max(0, int(delay_seconds or 0))),
            id="maintenance_catchup_startup",
            replace_existing=True,
        )

    def _scheduled_job(
        self, name: str, func: Callable[[], object]
    ) -> Callable[[], object]:
        async def runner() -> None:
            try:
                result = func()
                if hasattr(result, "__await__"):
                    await result
            except asyncio.CancelledError:
                if self._shutting_down:
                    telemetry = self.telemetry.get(name)
                    if telemetry is not None:
                        telemetry.last_status = "cancelled"
                        telemetry.detail = "shutdown_cancelled"
                    return
                raise

        return runner

    def sync_runtime_jobs(self) -> None:
        if not settings.AUTOMATION_ENABLED:
            return
        runtime = RuntimeSettingsStore.load()

        self._sync_job(
            "market_data_refresh",
            self._run_market_data_refresh,
            runtime.market_data.refresh_interval_seconds,
            runtime.market_data.enabled,
        )
        self._sync_job(
            "risk_refresh",
            self._run_risk_refresh,
            max(runtime.market_data.refresh_interval_seconds, 300),
            runtime.market_data.enabled,
        )
        self._sync_job(
            "gmail_sync",
            self._run_gmail_sync,
            86400,  # once a day
            runtime.gmail.enabled,
        )
        self._sync_job(
            "brokerage_reconcile",
            self._run_brokerage_reconcile,
            21600,
            bool(runtime.plaid.enabled and runtime.plaid.access_token),
        )

    def _sync_job(
        self,
        name: str,
        func: Callable[[], object],
        interval_seconds: int,
        enabled: bool,
    ) -> None:
        telemetry = self.telemetry[name]
        was_enabled = telemetry.enabled
        telemetry.interval_seconds = interval_seconds
        telemetry.enabled = enabled

        existing = self.scheduler.get_job(name)
        current_interval = None
        if existing is not None:
            trigger_interval = getattr(existing.trigger, "interval", None)
            if trigger_interval is not None:
                current_interval = int(trigger_interval.total_seconds())

        if enabled:
            if existing is None or current_interval != interval_seconds:
                if existing is not None:
                    self.scheduler.remove_job(name)
                self.scheduler.add_job(
                    self._scheduled_job(name, func),
                    "interval",
                    seconds=interval_seconds,
                    id=name,
                    replace_existing=True,
                )
                if telemetry.last_status == "disabled":
                    telemetry.last_status = "idle"
                    telemetry.detail = None
            if not was_enabled and self.scheduler.running:
                self._schedule_startup_run(name, func)
            return

        if existing is not None:
            self.scheduler.remove_job(name)
        telemetry.last_status = "disabled"
        telemetry.detail = "runtime_disabled"

    def _log_job_action(
        self,
        *,
        job_name: str,
        status: str,
        summary: str,
        subject_id: str | None = None,
        subject_type: str | None = None,
        subject_name: str | None = None,
        metadata: dict | None = None,
    ) -> None:
        AgentActionLogService.append(
            source="automation",
            action_type=job_name,
            status=status,
            summary=summary,
            subject_id=subject_id,
            subject_type=subject_type,
            subject_name=subject_name,
            metadata=metadata or {},
        )

    @staticmethod
    def _is_artifact_question(question: UnresolvedQuestion) -> bool:
        return is_artifact_question_text(question.question_text)

    async def _run_research_loop(self) -> None:
        telemetry = self.telemetry["research_loop"]
        telemetry.last_run_at = datetime.now(UTC)
        async with async_session_maker() as session:
            try:
                question, review_item, subject_context = (
                    await self._select_research_question(session)
                )
                if question is None:
                    telemetry.last_status = "ok"
                    telemetry.detail = "no_open_questions"
                    return
                if not RuntimeSettingsStore.load().research.api_key:
                    telemetry.last_status = "waiting_for_config"
                    telemetry.detail = "research_provider_not_configured"
                    self._log_job_action(
                        job_name="research_loop",
                        status="blocked",
                        summary="Research loop skipped because the research provider is not configured.",
                    )
                    return
                result = await ResearchService(session).run_targeted_question(question)
                telemetry.last_status = result.telemetry_status
                telemetry.detail = (
                    f"question={question.id} evidence={result.evidence_id}"
                    + (
                        ""
                        if result.loop_detail is None
                        else f" subject={result.loop_detail.get('subject_name')} stance={result.loop_detail.get('stance')} shadow={result.loop_detail.get('shadow', {}).get('triggered')}"
                    )
                    if result.evidence_id
                    else f"question={question.id} {result.reason}"
                )
                self._log_job_action(
                    job_name="research_loop",
                    status="ok" if result.started else result.reason,
                    summary=(
                        f"Research loop worked on {subject_context.get('subject_name')}: {question.question_text}"
                        if result.started
                        else f"Research loop could not complete {subject_context.get('subject_name')}: {question.question_text}: {result.reason}"
                    ),
                    subject_id=subject_context.get("subject_id"),
                    subject_type=subject_context.get("subject_type"),
                    subject_name=subject_context.get("subject_name"),
                    metadata={
                        "question_id": str(question.id),
                        "evidence_id": (
                            None
                            if result.evidence_id is None
                            else str(result.evidence_id)
                        ),
                        "review_item_id": (
                            None if review_item is None else str(review_item.id)
                        ),
                        "review_item_type": (
                            None if review_item is None else review_item.item_type
                        ),
                        "priority_score": (
                            None
                            if review_item is None
                            else float(review_item.priority_score or 0.0)
                        ),
                    },
                )
            except Exception as exc:
                telemetry.last_status = "error"
                telemetry.detail = str(exc)
                self._log_job_action(
                    job_name="research_loop",
                    status="error",
                    summary=f"Research loop failed: {exc}",
                )

    async def _run_question_resolution(self) -> None:
        telemetry = self.telemetry["question_resolution"]
        telemetry.last_run_at = datetime.now(UTC)
        async with async_session_maker() as session:
            try:
                question = await self._select_resolution_question(session)
                if question is None:
                    telemetry.last_status = "ok"
                    telemetry.detail = "no_investigating_questions_ready"
                    return
                resolved = await ResearchService(session)._maybe_resolve_question(
                    question,
                    question.originating_evidence_id,
                )
                telemetry.last_status = "ok"
                telemetry.detail = (
                    f"question={question.id} resolved={str(resolved).lower()}"
                )
                self._log_job_action(
                    job_name="question_resolution",
                    status="resolved" if resolved else "still_open",
                    summary=(
                        f"Question resolution closed: {question.question_text}"
                        if resolved
                        else f"Question resolution retained for more evidence: {question.question_text}"
                    ),
                    metadata={
                        "question_id": str(question.id),
                        "evidence_id": str(question.originating_evidence_id),
                    },
                )
            except Exception as exc:
                telemetry.last_status = "error"
                telemetry.detail = str(exc)
                self._log_job_action(
                    job_name="question_resolution",
                    status="error",
                    summary=f"Question resolution failed: {exc}",
                )

    async def _select_resolution_question(
        self,
        session,
    ) -> UnresolvedQuestion | None:
        candidates = (
            (
                await session.execute(
                    select(UnresolvedQuestion)
                    .where(
                        UnresolvedQuestion.status == "investigating",
                        UnresolvedQuestion.originating_evidence_id.is_not(None),
                    )
                    .order_by(
                        desc(UnresolvedQuestion.urgency),
                        UnresolvedQuestion.created_at.asc(),
                    )
                    .limit(50)
                )
            )
            .scalars()
            .all()
        )
        for question in candidates:
            if self._is_artifact_question(question):
                question.status = "obsolete"
                await session.commit()
                continue
            if await self._is_artifact_question_subject(session, question):
                question.status = "obsolete"
                await session.commit()
                continue
            if not await self._is_portfolio_relevant_question_subject(
                session, question
            ):
                continue
            if AgentActionLogService.has_recent_question_attempt(
                str(question.id),
                statuses=(
                    "question_resolved",
                    "question_still_open",
                    "resolution_assessment_deferred",
                ),
                within_seconds=24 * 60 * 60,
            ):
                continue
            return question
        return None

    async def _select_research_question(
        self,
        session,
    ) -> tuple[
        UnresolvedQuestion | None, ReviewQueueItem | None, dict[str, str | None]
    ]:
        await ReviewService(session).refresh_queue()
        review_items = (
            (
                await session.execute(
                    select(ReviewQueueItem)
                    .where(ReviewQueueItem.status.in_(["pending", "in_review"]))
                    .order_by(
                        desc(ReviewQueueItem.priority_score),
                        desc(ReviewQueueItem.created_at),
                    )
                    .limit(30)
                )
            )
            .scalars()
            .all()
        )
        for item in review_items:
            question = await self._question_for_review_item(session, item)
            if question is None:
                continue
            if AgentActionLogService.has_recent_question_attempt(str(question.id)):
                continue
            return (
                question,
                item,
                await self._question_subject_context(session, question),
            )

        fallback_questions = (
            (
                await session.execute(
                    select(UnresolvedQuestion)
                    .where(UnresolvedQuestion.status == "open")
                    .order_by(
                        desc(UnresolvedQuestion.urgency),
                        UnresolvedQuestion.created_at.asc(),
                    )
                )
            )
            .scalars()
            .all()
        )
        for question in fallback_questions:
            if self._is_artifact_question(question):
                question.status = "obsolete"
                await session.flush()
                await session.commit()
                continue
            if await self._is_artifact_question_subject(session, question):
                question.status = "obsolete"
                await session.flush()
                await session.commit()
                continue
            if not await self._is_portfolio_relevant_question_subject(
                session, question
            ):
                continue
            if AgentActionLogService.has_recent_question_attempt(str(question.id)):
                continue
            return (
                question,
                None,
                await self._question_subject_context(session, question),
            )
        return (
            None,
            None,
            {"subject_id": None, "subject_type": None, "subject_name": "portfolio"},
        )

    async def _question_for_review_item(
        self, session, item: ReviewQueueItem
    ) -> UnresolvedQuestion | None:
        coverage = await self._coverage_for_review_item(session, item)
        if coverage is None:
            coverage = await self._ensure_coverage_for_review_item(session, item)
        if coverage is None:
            return None

        existing_questions = (
            (
                await session.execute(
                    select(UnresolvedQuestion)
                    .where(
                        UnresolvedQuestion.coverage_map_id == coverage.id,
                        UnresolvedQuestion.status.in_(["open", "investigating"]),
                    )
                    .order_by(
                        desc(UnresolvedQuestion.urgency),
                        UnresolvedQuestion.created_at.asc(),
                    )
                )
            )
            .scalars()
            .all()
        )
        for question in existing_questions:
            if self._is_artifact_question(question):
                question.status = "obsolete"
                await session.flush()
                await session.commit()
                continue
            if await self._is_artifact_question_subject(session, question):
                question.status = "obsolete"
                await session.flush()
                await session.commit()
                continue
            if question.status == "open":
                return question
        if existing_questions:
            return None

        if item.item_type != "position" or float(item.priority_score or 0.0) < 40.0:
            return None

        question = UnresolvedQuestion(
            coverage_map_id=coverage.id,
            question_text=await self._default_position_research_question(
                session, item.item_id
            ),
            urgency=5 if float(item.priority_score or 0.0) >= 60.0 else 4,
        )
        session.add(question)
        await session.flush()
        return question

    async def _coverage_for_review_item(
        self, session, item: ReviewQueueItem
    ) -> CoverageMap | None:
        coverage = (
            await session.execute(
                select(CoverageMap)
                .where(
                    CoverageMap.subject_type == item.item_type,
                    CoverageMap.subject_id == item.item_id,
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if coverage is not None or item.item_type != "position":
            return coverage
        position = await session.get(Position, item.item_id)
        if position is None:
            return None
        security = await session.get(Security, position.security_id)
        if security is None:
            return None
        return (
            await session.execute(
                select(CoverageMap)
                .where(
                    CoverageMap.subject_type == "entity",
                    CoverageMap.subject_id == security.entity_id,
                )
                .limit(1)
            )
        ).scalar_one_or_none()

    async def _ensure_coverage_for_review_item(
        self, session, item: ReviewQueueItem
    ) -> CoverageMap | None:
        if item.item_type != "position":
            return None
        position = await session.get(Position, item.item_id)
        if position is None:
            return None
        return await CanonicalStateService(session).ensure_coverage_map(
            subject_type="position",
            subject_id=position.id,
            create=lambda: CoverageMap(
                subject_type="position",
                subject_id=position.id,
                evidence_class_coverage_json={},
                overall_coverage_score=0.0,
            ),
        )

    async def _default_position_research_question(self, session, position_id) -> str:
        position = await session.get(Position, position_id)
        if position is None:
            return "What evidence would materially strengthen or falsify the current view on this portfolio holding?"
        security = await session.get(Security, position.security_id)
        entity = (
            None if security is None else await session.get(Entity, security.entity_id)
        )
        ticker = "this holding" if security is None else security.ticker
        name = ticker if entity is None else f"{ticker} ({entity.name})"
        weight = float(position.weight_pct or 0.0)
        context = " ".join(
            [
                ticker,
                getattr(entity, "name", "") or "",
                getattr(entity, "sector", "") or "",
                getattr(entity, "industry", "") or "",
                getattr(entity, "description", "") or "",
            ]
        ).strip()
        metadata_clause = (
            f" Available profile context: {context[:360]}." if context else ""
        )
        focus = (
            "Focus on the concrete mechanism that could change fundamentals, valuation, competitive position, catalysts, "
            "contradictions, sizing risk, or relevant historical analogies."
        )
        size = f" as a {weight:.0f}% portfolio holding" if weight else ""
        return f"What evidence would materially strengthen or falsify the current view on {name}{size}? {focus}{metadata_clause}"

    async def _question_subject_context(
        self, session, question: UnresolvedQuestion
    ) -> dict[str, str | None]:
        coverage = await session.get(CoverageMap, question.coverage_map_id)
        if coverage is None:
            return {
                "subject_id": None,
                "subject_type": None,
                "subject_name": "portfolio",
            }
        return {
            "subject_id": str(coverage.subject_id),
            "subject_type": coverage.subject_type,
            "subject_name": await self._subject_name_for_context(
                session, coverage.subject_type, coverage.subject_id
            ),
        }

    async def _is_artifact_question_subject(
        self, session, question: UnresolvedQuestion
    ) -> bool:
        coverage = await session.get(CoverageMap, question.coverage_map_id)
        if coverage is None:
            return False
        name = await self._subject_name_for_context(
            session, coverage.subject_type, coverage.subject_id
        )
        return is_artifact_subject_name(name)

    async def _is_portfolio_relevant_question_subject(
        self, session, question: UnresolvedQuestion
    ) -> bool:
        coverage = await session.get(CoverageMap, question.coverage_map_id)
        if coverage is None:
            return False
        if coverage.subject_type == "portfolio":
            return True
        if coverage.subject_type == "position":
            position = await session.get(Position, coverage.subject_id)
            return bool(
                position
                and position.list_type in {"holding", "watchlist", "considering"}
            )
        if coverage.subject_type == "entity":
            linked_position = (
                await session.execute(
                    select(Position)
                    .join(Security, Position.security_id == Security.id)
                    .where(
                        Security.entity_id == coverage.subject_id,
                        Position.list_type.in_(["holding", "watchlist", "considering"]),
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            return linked_position is not None
        # Themes can be portfolio-relevant, but broad theme questions should be
        # selected by strategist/review paths rather than the blind fallback.
        return False

    async def _subject_name_for_context(
        self, session, subject_type: str, subject_id
    ) -> str:
        if subject_type == "position":
            position = await session.get(Position, subject_id)
            if position is not None:
                security = await session.get(Security, position.security_id)
                if security is not None:
                    entity = await session.get(Entity, security.entity_id)
                    return (
                        security.ticker
                        if entity is None
                        else f"{security.ticker} · {entity.name}"
                    )
        if subject_type == "entity":
            entity = await session.get(Entity, subject_id)
            if entity is not None:
                return entity.name
        if subject_type == "theme":
            theme = await session.get(Theme, subject_id)
            if theme is not None:
                return theme.name
        return str(subject_id)

    async def _run_database_backup(self) -> None:
        telemetry = self.telemetry["database_backup"]
        telemetry.last_run_at = datetime.now(UTC)
        try:
            result = await DatabaseBackupService.create_backup_async()
            created_name = (
                Path(result.created_path).name if result.created_path else None
            )
            removed = (
                ", ".join(result.removed_files) if result.removed_files else "none"
            )
            total_mb = result.total_bytes / (1024 * 1024)
            telemetry.last_status = "ok"
            telemetry.detail = (
                f"created={created_name} size_mb={result.created_bytes / (1024 * 1024):.1f} "
                f"removed={removed} total_mb={total_mb:.1f}"
            )
            self._log_job_action(
                job_name="database_backup",
                status="ok",
                summary=f"Database backup completed: {created_name}",
                metadata={
                    "created_path": result.created_path,
                    "created_bytes": result.created_bytes,
                    "removed_files": result.removed_files,
                    "remaining_files": result.remaining_files,
                    "total_bytes": result.total_bytes,
                },
            )
        except Exception as exc:
            telemetry.last_status = "error"
            telemetry.detail = str(exc)
            self._log_job_action(
                job_name="database_backup",
                status="error",
                summary=f"Database backup failed: {exc}",
            )

    async def _run_integrity_audit(self) -> None:
        telemetry = self.telemetry["integrity_audit"]
        telemetry.last_run_at = datetime.now(UTC)
        async with async_session_maker() as session:
            try:
                service = IntegrityService(session)
                lineage = await service.backfill_lineage_signatures(
                    limit=settings.CORROBORATION_LINEAGE_BACKFILL_BATCH_SIZE,
                )
                graph_repair = await service.restore_investment_object_edges(
                    limit=settings.INTEGRITY_INVESTMENT_EDGE_REPAIR_BATCH_SIZE,
                )
                # Self-heal every pass so neither the graph nor portfolio/canonical
                # state accumulates rot. repair_state is idempotent; it covers
                # classes the graph audit's `ok` flag doesn't track (e.g. stale
                # zero-share holdings, corrupt fallback theses).
                repair = await service.repair_state()
                audit = await service.audit_state()
                telemetry.last_status = "ok" if audit.ok else "warning"
                telemetry.detail = (
                    f"repaired={repair['total_repaired']} "
                    f"investment_edges_restored={graph_repair['edges_created']} "
                    f"lineage_enriched={lineage['enriched']} "
                    f"coverage_duplicates={len(audit.duplicate_coverage_subjects)} "
                    f"conclusion_duplicates={len(audit.duplicate_conclusion_subjects)} "
                    f"edge_duplicates={len(audit.duplicate_edges)} "
                    f"orphan_edges={audit.counts.orphan_edges} "
                    f"source_duplicates={len(audit.duplicate_sources)} "
                    f"unknown_node_types={','.join(audit.unknown_edge_node_types) or 'none'} "
                    f"missing_storage_objects={audit.counts.missing_storage_objects} "
                    f"profiles={audit.counts.profiles} edges={audit.counts.edges}"
                )
                unresolved_summary = "; ".join(
                    part
                    for part in (
                        (
                            f"{len(audit.duplicate_sources)} duplicate source group(s)"
                            if audit.duplicate_sources
                            else None
                        ),
                        (
                            f"{len(audit.duplicate_coverage_subjects)} duplicate coverage subject(s)"
                            if audit.duplicate_coverage_subjects
                            else None
                        ),
                        (
                            f"{len(audit.duplicate_conclusion_subjects)} duplicate conclusion subject(s)"
                            if audit.duplicate_conclusion_subjects
                            else None
                        ),
                        (
                            f"{len(audit.duplicate_edges)} duplicate edge group(s)"
                            if audit.duplicate_edges
                            else None
                        ),
                        (
                            f"{audit.counts.orphan_edges} orphan edge(s)"
                            if audit.counts.orphan_edges
                            else None
                        ),
                        (
                            "unknown graph node types: "
                            + ", ".join(audit.unknown_edge_node_types)
                            if audit.unknown_edge_node_types
                            else None
                        ),
                        (
                            f"{audit.counts.missing_storage_objects} missing storage object(s)"
                            if audit.counts.missing_storage_objects
                            else None
                        ),
                    )
                    if part
                )
                self._log_job_action(
                    job_name="integrity_audit",
                    status="ok" if audit.ok else "warning",
                    summary=(
                        f"Integrity audit needs review after repairing {repair['total_repaired']} issue(s) "
                        f"and restoring {graph_repair['edges_created']} investment edge(s): "
                        f"{unresolved_summary}."
                        if not audit.ok
                        else (
                            f"Integrity self-repair healed {repair['total_repaired']} issue(s); "
                            f"restored {graph_repair['edges_created']} investment edge(s); "
                            f"provenance enriched {lineage['enriched']} item(s); state is now clean."
                            if repair["total_repaired"] or graph_repair["edges_created"]
                            else "Integrity audit passed with no duplicate canonical rows or orphan edges; "
                            f"provenance enriched {lineage['enriched']} item(s)."
                        )
                    ),
                    metadata={
                        **audit.model_dump(mode="json"),
                        "repair": repair,
                        "investment_edge_repair": graph_repair,
                        "lineage_backfill": lineage,
                    },
                )
            except Exception as exc:
                telemetry.last_status = "error"
                telemetry.detail = str(exc)
                self._log_job_action(
                    job_name="integrity_audit",
                    status="error",
                    summary=f"Integrity audit failed: {exc}",
                )

    async def _run_entity_hygiene(self) -> None:
        telemetry = self.telemetry["entity_hygiene"]
        telemetry.last_run_at = datetime.now(UTC)
        async with async_session_maker() as session:
            try:
                summary = await EntityHygieneService(session).run()
                telemetry.last_status = "ok"
                telemetry.detail = (
                    f"scanned={summary['scanned']} merged={summary.get('duplicate_merge_count', 0)} "
                    f"deleted={summary['deleted_count']} "
                    f"reclassified={summary.get('reclassified_count', 0)} flagged={summary['flagged_count']}"
                )
                self._log_job_action(
                    job_name="entity_hygiene",
                    status="ok",
                    summary=(
                        f"Entity hygiene merged {summary.get('duplicate_merge_count', 0)} duplicate entity node(s), "
                        f"removed {summary['deleted_count']} orphaned junk node(s), "
                        f"reclassified {summary.get('reclassified_count', 0)} topic node(s), "
                        f"and flagged {summary['flagged_count']} for review."
                    ),
                    metadata={
                        "scanned": summary["scanned"],
                        "duplicate_merge_count": summary.get(
                            "duplicate_merge_count", 0
                        ),
                        "deleted_count": summary["deleted_count"],
                        "reclassified_count": summary.get("reclassified_count", 0),
                        "flagged_count": summary["flagged_count"],
                        "duplicate_merges": summary.get("duplicate_merges", [])[:50],
                        "deleted": summary["deleted"][:50],
                        "reclassified": summary.get("reclassified", [])[:50],
                        "flagged": summary["flagged"][:50],
                    },
                )
            except Exception as exc:
                telemetry.last_status = "error"
                telemetry.detail = str(exc)
                self._log_job_action(
                    job_name="entity_hygiene",
                    status="error",
                    summary=f"Entity hygiene failed: {exc}",
                )

    async def _run_theme_hygiene(self) -> None:
        telemetry = self.telemetry["theme_hygiene"]
        telemetry.last_run_at = datetime.now(UTC)
        async with async_session_maker() as session:
            try:
                summary = await ThemeHygieneService(session).run()
                telemetry.last_status = "ok"
                telemetry.detail = (
                    f"scanned={summary['scanned']} deleted={summary['deleted_count']} "
                    f"renamed={summary['renamed_count']} flagged={summary['flagged_count']}"
                )
                self._log_job_action(
                    job_name="theme_hygiene",
                    status="ok",
                    summary=(
                        f"Theme hygiene removed {summary['deleted_count']} placeholder wrapper theme(s) "
                        f"renamed {summary['renamed_count']} substantive wrapper theme(s), and flagged "
                        f"{summary['flagged_count']} artifact-like theme(s) for review."
                    ),
                    metadata={
                        "scanned": summary["scanned"],
                        "deleted_count": summary["deleted_count"],
                        "renamed_count": summary["renamed_count"],
                        "flagged_count": summary["flagged_count"],
                        "deleted": summary["deleted"][:50],
                        "renamed": summary["renamed"][:50],
                        "flagged": summary["flagged"][:50],
                    },
                )
            except Exception as exc:
                telemetry.last_status = "error"
                telemetry.detail = str(exc)
                self._log_job_action(
                    job_name="theme_hygiene",
                    status="error",
                    summary=f"Theme hygiene failed: {exc}",
                )

    async def _run_media_cleanup(self) -> None:
        telemetry = self.telemetry["media_cleanup"]
        telemetry.last_run_at = datetime.now(UTC)
        try:
            policy = MediaIngestionPolicy.from_settings()
            summary = policy.cleanup_stale_workspaces()
            telemetry.last_status = "ok"
            telemetry.detail = (
                f"scanned={summary['scanned']} deleted={summary['deleted']}"
            )
            self._log_job_action(
                job_name="media_cleanup",
                status="ok",
                summary=(
                    f"Media cleanup scanned {summary['scanned']} temporary media workspace(s) "
                    f"and removed {summary['deleted']} stale item(s)."
                ),
                metadata={
                    "scanned": summary["scanned"],
                    "deleted": summary["deleted"],
                    "temp_retention_hours": policy.temp_retention_hours,
                    "persist_raw_media": policy.persist_raw_media,
                    "max_download_mb": policy.max_download_mb,
                },
            )
        except Exception as exc:
            telemetry.last_status = "error"
            telemetry.detail = str(exc)
            self._log_job_action(
                job_name="media_cleanup",
                status="error",
                summary=f"Media cleanup failed: {exc}",
            )

    async def _run_source_claim_assessment(self) -> None:
        telemetry = self.telemetry["source_claim_assessment"]
        telemetry.last_run_at = datetime.now(UTC)
        telemetry.last_status = "running"
        async with async_session_maker() as session:
            try:
                result = await SourceService(session).assess_due_source_claims(
                    limit=settings.SOURCE_CLAIM_ASSESSMENT_BATCH_SIZE,
                    scan_limit=settings.SOURCE_CLAIM_ASSESSMENT_SCAN_LIMIT,
                    apply=True,
                    min_confidence=settings.SOURCE_CLAIM_ASSESSMENT_MIN_CONFIDENCE,
                    retry_hours=settings.SOURCE_CLAIM_ASSESSMENT_RETRY_HOURS,
                    retry_share=settings.SOURCE_CLAIM_ASSESSMENT_RETRY_SHARE,
                    research_missing_evidence=True,
                    research_limit=settings.SOURCE_CLAIM_ASSESSMENT_RESEARCH_LIMIT,
                )
                telemetry.last_status = "ok"
                telemetry.detail = (
                    f"scanned={result['scanned']} due={result['due']} "
                    f"portfolio_due={result.get('portfolio_relevant_eligible', 0)} "
                    f"portfolio_selected={result.get('selected_portfolio_relevant', 0)} "
                    f"deferred={result.get('deferred', 0)} "
                    f"proposed={result['proposed']} applied={result['applied']} "
                    f"research_started={result.get('research_started', 0)}"
                )
                self._log_job_action(
                    job_name="source_claim_assessment",
                    status="ok",
                    summary=(
                        f"Source claim assessment reviewed {result['proposed']} due claim(s) "
                        f"and applied {result['applied']} outcome(s); "
                        f"deferred {result.get('deferred', 0)} pending retry; "
                        f"started {result.get('research_started', 0)} follow-up research pass(es)."
                    ),
                    metadata={
                        "scanned": result["scanned"],
                        "due": result["due"],
                        "eligible": result.get("eligible", 0),
                        "portfolio_relevant_eligible": result.get(
                            "portfolio_relevant_eligible", 0
                        ),
                        "selected_portfolio_relevant": result.get(
                            "selected_portfolio_relevant", 0
                        ),
                        "deferred": result.get("deferred", 0),
                        "proposed": result["proposed"],
                        "applied": result["applied"],
                        "research_attempted": result.get("research_attempted", 0),
                        "research_started": result.get("research_started", 0),
                    },
                )
            except Exception as exc:
                telemetry.last_status = "error"
                telemetry.detail = str(exc)
                self._log_job_action(
                    job_name="source_claim_assessment",
                    status="error",
                    summary=f"Source claim assessment failed: {exc}",
                )

    async def _run_market_setup_assessment(self) -> None:
        telemetry = self.telemetry["market_setup_assessment"]
        telemetry.last_run_at = datetime.now(UTC)
        telemetry.last_status = "running"
        async with async_session_maker() as session:
            try:
                result = await MarketSetupSignalService(session).assess_due_signals(
                    limit=settings.MARKET_SETUP_ASSESSMENT_BATCH_SIZE,
                    scan_limit=settings.MARKET_SETUP_ASSESSMENT_SCAN_LIMIT,
                    apply=True,
                    min_confidence=settings.MARKET_SETUP_ASSESSMENT_MIN_CONFIDENCE,
                    grace_hours=settings.MARKET_SETUP_ASSESSMENT_GRACE_HOURS,
                    retry_hours=settings.MARKET_SETUP_ASSESSMENT_RETRY_HOURS,
                    research_missing_evidence=(
                        settings.MARKET_SETUP_ASSESSMENT_RESEARCH_LIMIT > 0
                    ),
                    research_limit=settings.MARKET_SETUP_ASSESSMENT_RESEARCH_LIMIT,
                )
                telemetry.last_status = "ok"
                telemetry.detail = (
                    f"scanned={result['scanned']} due={result['due']} "
                    f"deferred={result.get('deferred', 0)} "
                    f"proposed={result['proposed']} applied={result['applied']} "
                    f"research_started={result.get('research_started', 0)}"
                )
                self._log_job_action(
                    job_name="market_setup_assessment",
                    status="ok",
                    summary=(
                        f"Market setup assessment reviewed {result['proposed']} due signal(s) "
                        f"and applied {result['applied']} evidence-backed outcome(s); "
                        f"started {result.get('research_started', 0)} follow-up research pass(es)."
                    ),
                    metadata={
                        "scanned": result["scanned"],
                        "due": result["due"],
                        "deferred": result.get("deferred", 0),
                        "proposed": result["proposed"],
                        "applied": result["applied"],
                        "research_attempted": result.get("research_attempted", 0),
                        "research_started": result.get("research_started", 0),
                    },
                )
            except Exception as exc:
                telemetry.last_status = "error"
                telemetry.detail = str(exc)
                self._log_job_action(
                    job_name="market_setup_assessment",
                    status="error",
                    summary=f"Market setup assessment failed: {exc}",
                )

    async def _run_fundamental_freshness(self) -> None:
        telemetry = self.telemetry["fundamental_freshness"]
        telemetry.last_run_at = datetime.now(UTC)
        telemetry.last_status = "running"
        async with async_session_maker() as session:
            try:
                result = await FundamentalMetricService(session).refresh_freshness()
                telemetry.last_status = "ok"
                telemetry.detail = (
                    f"marked_stale={result['marked_stale']} "
                    f"restored_current={result['restored_current']}"
                )
                self._log_job_action(
                    job_name="fundamental_freshness",
                    status="ok",
                    summary=(
                        f"Fundamental freshness marked {result['marked_stale']} metric(s) stale "
                        f"and restored {result['restored_current']} metric(s) to current."
                    ),
                    metadata=result,
                )
            except Exception as exc:
                telemetry.last_status = "error"
                telemetry.detail = str(exc)
                self._log_job_action(
                    job_name="fundamental_freshness",
                    status="error",
                    summary=f"Fundamental freshness refresh failed: {exc}",
                )

    async def _run_investment_object_backfill(self) -> None:
        telemetry = self.telemetry["investment_object_backfill"]
        telemetry.last_run_at = datetime.now(UTC)
        telemetry.last_status = "running"
        async with async_session_maker() as session:
            try:
                result = await InvestmentObjectBackfillService(session).run(
                    apply=True,
                    scan_limit=settings.INVESTMENT_OBJECT_BACKFILL_SCAN_LIMIT,
                    max_model_calls=settings.INVESTMENT_OBJECT_BACKFILL_BATCH_SIZE,
                    min_confidence=settings.INVESTMENT_OBJECT_BACKFILL_MIN_CONFIDENCE,
                    portfolio_only=True,
                )
                telemetry.last_status = "warning" if result["errors"] else "ok"
                telemetry.detail = (
                    f"model_calls={result['model_calls']} metrics_created={result['metrics_created']} "
                    f"setup_created={result['setup_created']} errors={result['errors']}"
                )
                self._log_job_action(
                    job_name="investment_object_backfill",
                    status=telemetry.last_status,
                    summary=(
                        f"Historical investment-object reindexing created "
                        f"{result['metrics_created']} metric(s) and "
                        f"{result['setup_created']} setup signal(s) from "
                        f"{result['model_calls']} bounded extraction call(s)."
                    ),
                    metadata={
                        key: value for key, value in result.items() if key != "examples"
                    },
                )
            except Exception as exc:
                telemetry.last_status = "error"
                telemetry.detail = str(exc)
                self._log_job_action(
                    job_name="investment_object_backfill",
                    status="error",
                    summary=f"Historical investment-object reindexing failed: {exc}",
                )

    async def _run_agent_reflection(self) -> None:
        telemetry = self.telemetry["agent_reflection"]
        telemetry.last_run_at = datetime.now(UTC)
        async with async_session_maker() as session:
            try:
                result = await AgentService(session).run_reflection_cycle()
                telemetry.last_status = "ok"
                telemetry.detail = f"{result['detail']}"
                self._log_job_action(
                    job_name="agent_reflection",
                    status=str(result.get("status") or "ok"),
                    summary=str(result.get("detail") or "Reflection cycle completed."),
                    metadata={"actions": result.get("actions")},
                )
            except Exception as exc:
                telemetry.last_status = "error"
                telemetry.detail = str(exc)
                self._log_job_action(
                    job_name="agent_reflection",
                    status="error",
                    summary=f"Reflection cycle failed: {exc}",
                )

    async def _run_relation_review(self) -> None:
        telemetry = self.telemetry["relation_review"]
        telemetry.last_run_at = datetime.now(UTC)
        async with async_session_maker() as session:
            try:
                result = await RelationReviewService(session).review_next_subject()
                telemetry.last_status = str(result.get("status") or "ok")
                telemetry.detail = str(
                    result.get("detail") or "relation_review_complete"
                )
                self._log_job_action(
                    job_name="relation_review",
                    status=str(result.get("status") or "ok"),
                    summary=(
                        f"Relation review revisited {result.get('subject_name')} and added {result.get('links_added', 0)} links."
                        if result.get("subject_name")
                        else str(result.get("detail") or "Relation review completed.")
                    ),
                    subject_id=str(result.get("subject_id") or "") or None,
                    subject_type=str(result.get("subject_type") or "") or None,
                    subject_name=str(result.get("subject_name") or "") or None,
                    metadata=result,
                )
            except Exception as exc:
                telemetry.last_status = "error"
                telemetry.detail = str(exc)
                self._log_job_action(
                    job_name="relation_review",
                    status="error",
                    summary=f"Relation review failed: {exc}",
                )

    async def _run_market_data_refresh(self) -> None:
        telemetry = self.telemetry["market_data_refresh"]
        telemetry.last_run_at = datetime.now(UTC)
        async with async_session_maker() as session:
            try:
                runtime = RuntimeSettingsStore.load()
                if not runtime.market_data.enabled:
                    telemetry.last_status = "disabled"
                    telemetry.detail = "Market data is disabled in settings."
                    return

                result = await MarketDataService(session).refresh_live_prices()
                telemetry.last_status = "ok"
                telemetry.detail = (
                    f"updated={result['updated']} detail={result['detail']}"
                )
                self._log_job_action(
                    job_name="market_data_refresh",
                    status="ok",
                    summary=f"Market data refresh updated {result['updated']} prices.",
                    metadata=result,
                )
            except Exception as exc:
                telemetry.last_status = "error"
                telemetry.detail = str(exc)
                self._log_job_action(
                    job_name="market_data_refresh",
                    status="error",
                    summary=f"Market data refresh failed: {exc}",
                )

    async def _run_evidence_processing(self) -> None:
        telemetry = self.telemetry["evidence_processing"]
        telemetry.last_run_at = datetime.now(UTC)
        async with async_session_maker() as session:
            try:
                evidence = (
                    (
                        await session.execute(
                            select(RawEvidence)
                            .where(RawEvidence.is_processed.is_(False))
                            .order_by(RawEvidence.created_at.asc())
                        )
                    )
                    .scalars()
                    .first()
                )
                if not evidence:
                    telemetry.last_status = "ok"
                    telemetry.detail = "no_pending_evidence"
                    return
                loop_detail = await ExtractionWorker(session).process_evidence(
                    evidence.id
                )
                telemetry.last_status = "ok"
                if loop_detail is None:
                    telemetry.detail = f"processed={evidence.id}"
                else:
                    telemetry.detail = (
                        f"processed={evidence.id} subject={loop_detail.get('subject_name')} "
                        f"stance={loop_detail.get('stance')} shadow={loop_detail.get('shadow', {}).get('triggered')}"
                    )
                self._log_job_action(
                    job_name="evidence_processing",
                    status="ok",
                    summary=f"Processed new evidence item {evidence.id}.",
                    metadata={
                        "evidence_id": str(evidence.id),
                        "loop_detail": loop_detail,
                    },
                )
            except Exception as exc:
                telemetry.last_status = "error"
                telemetry.detail = str(exc)
                self._log_job_action(
                    job_name="evidence_processing",
                    status="error",
                    summary=f"Evidence processing failed: {exc}",
                )

    async def _run_risk_refresh(self) -> None:
        telemetry = self.telemetry["risk_refresh"]
        telemetry.last_run_at = datetime.now(UTC)
        async with async_session_maker() as session:
            try:
                runtime = RuntimeSettingsStore.load()
                if not runtime.portfolio.default_benchmark_ticker:
                    telemetry.last_status = "waiting_for_config"
                    telemetry.detail = (
                        "No default benchmark ticker set in portfolio settings."
                    )
                    return

                risk_service = RiskService(session)
                result = await risk_service.refresh_summary()
                attribution = await risk_service.get_performance_attribution(
                    window_days=21
                )
                benchmark = (
                    result.active_benchmark.ticker if result.active_benchmark else "n/a"
                )
                telemetry.last_status = "ok"
                telemetry.detail = (
                    f"benchmark={benchmark} attribution_return_pct={attribution.return_pct} "
                    f"attribution_coverage_pct={attribution.coverage_pct}"
                )
                self._log_job_action(
                    job_name="risk_refresh",
                    status="ok",
                    summary=f"Risk summary refreshed against {benchmark}.",
                    metadata={
                        "benchmark": benchmark,
                        "active_return_pct": attribution.active_return_pct,
                        "attribution_return_pct": attribution.return_pct,
                        "attribution_coverage_pct": attribution.coverage_pct,
                    },
                )
            except Exception as exc:
                telemetry.last_status = "error"
                telemetry.detail = str(exc)
                self._log_job_action(
                    job_name="risk_refresh",
                    status="error",
                    summary=f"Risk refresh failed: {exc}",
                )

    async def _run_shadow_refresh(self) -> None:
        telemetry = self.telemetry["shadow_refresh"]
        telemetry.last_run_at = datetime.now(UTC)
        async with async_session_maker() as session:
            try:
                service = ShadowService(session)
                account_events = (
                    await service.apply_portfolio_events_to_shadow_accounts()
                )
                transitioned = await service.refresh_pending_paper_orders()
                marked = await service.refresh_paper_account_marks()
                learning = await service.reconcile_shadow_learning()
                evidence_events = await service.attach_queued_evidence_events()
                experiments = await service.list_experiments()
                event_experiment_ids = set(evidence_events["experiment_ids"])
                queued = next(
                    (
                        item
                        for item in experiments
                        if item.id in event_experiment_ids
                        and ShadowService.normalize_run_status(item.run_status)
                        in {"queued", "running"}
                    ),
                    None,
                ) or next(
                    (
                        item
                        for item in experiments
                        if ShadowService.normalize_run_status(item.run_status)
                        in {"queued", "running"}
                    ),
                    None,
                )
                if not queued:
                    telemetry.last_status = "ok"
                    telemetry.detail = (
                        f"no_active_experiments pending_orders_transitioned={transitioned} "
                        f"paper_positions_marked={marked} "
                        f"account_events_applied={account_events['applied']} "
                        f"account_events_recorded={account_events['recorded']} "
                        f"account_event_reconciliation_required={account_events['reconciliation_required']} "
                        f"account_timelines_rebuilt={account_events['timelines_rebuilt']} "
                        f"account_timeline_rebuild_failures={account_events['timeline_rebuild_failures']} "
                        f"shadow_lessons_reconciled={learning['reconciled']} "
                        f"evidence_events_attached={evidence_events['attached']} "
                        f"evidence_events_skipped={evidence_events['skipped']}"
                    )
                    return
                if service.experiment_run_is_active(queued.id):
                    telemetry.last_status = "warning"
                    telemetry.detail = f"already_advancing={queued.id}"
                    return
                before_progress = (
                    (queued.final_portfolio_state_json or {})
                    .get("run_details", {})
                    .get("progress", {})
                )
                before_step = int(before_progress.get("step_count") or 0)
                experiment = await service.run_experiment(queued.id)
                after_progress = (
                    (experiment.final_portfolio_state_json or {})
                    .get("run_details", {})
                    .get("progress", {})
                )
                after_step = int(after_progress.get("step_count") or 0)
                if (
                    ShadowService.normalize_run_status(experiment.run_status)
                    == "running"
                    and after_step == before_step
                ):
                    telemetry.last_status = "ok"
                    telemetry.detail = (
                        f"waiting_for_checkpoint={experiment.id} "
                        f"next_checkpoint_at={after_progress.get('next_checkpoint_at')} "
                        f"step={after_step}/{after_progress.get('target_steps')}"
                    )
                    return
                telemetry.last_status = "ok"
                telemetry.detail = (
                    f"advanced={queued.id} status={experiment.run_status} "
                    f"pending_orders_transitioned={transitioned} paper_positions_marked={marked} "
                    f"account_events_applied={account_events['applied']} "
                    f"account_events_recorded={account_events['recorded']} "
                    f"account_event_reconciliation_required={account_events['reconciliation_required']} "
                    f"account_timelines_rebuilt={account_events['timelines_rebuilt']} "
                    f"account_timeline_rebuild_failures={account_events['timeline_rebuild_failures']} "
                    f"shadow_lessons_reconciled={learning['reconciled']} "
                    f"evidence_events_attached={evidence_events['attached']} "
                    f"evidence_events_skipped={evidence_events['skipped']}"
                )
                self._log_job_action(
                    job_name="shadow_refresh",
                    status="ok",
                    summary=f"Shadow run {experiment.name} advanced to {experiment.run_status}.",
                    metadata={
                        "experiment_id": str(experiment.id),
                        "run_status": experiment.run_status,
                        "account_events": account_events,
                        "shadow_learning": learning,
                        "evidence_events": {
                            **evidence_events,
                            "experiment_ids": [
                                str(item) for item in evidence_events["experiment_ids"]
                            ],
                        },
                    },
                )
            except Exception as exc:
                telemetry.last_status = "error"
                telemetry.detail = str(exc)
                self._log_job_action(
                    job_name="shadow_refresh",
                    status="error",
                    summary=f"Shadow refresh failed: {exc}",
                )

    async def _run_gmail_sync(self) -> None:
        telemetry = self.telemetry["gmail_sync"]
        telemetry.last_run_at = datetime.now(UTC)
        try:
            runtime = RuntimeSettingsStore.load()
            if not runtime.gmail.enabled:
                telemetry.last_status = "disabled"
                telemetry.detail = "Gmail sync is disabled."
                return

            async with async_session_maker() as session:
                from investos.services.mailbox import GmailMailboxService

                service = GmailMailboxService(session)
                result = await service.sync_recent_messages()
                processed_messages = int(
                    result.get("processed_messages") or result.get("processed") or 0
                )
                transactions_created = int(result.get("transactions_created") or 0)
                if result.get("status") == "ok" or str(
                    result.get("detail") or ""
                ).startswith("ok"):
                    telemetry.last_status = "ok"
                    telemetry.detail = f"Processed {processed_messages} messages, {transactions_created} transactions extracted."
                    self._log_job_action(
                        job_name="gmail_sync",
                        status="ok",
                        summary=(
                            f"Gmail sync processed {processed_messages} messages and extracted "
                            f"{transactions_created} transactions."
                        ),
                        metadata=result,
                    )
                else:
                    telemetry.last_status = "error"
                    telemetry.detail = str(result.get("detail", "Unknown error"))
                    self._log_job_action(
                        job_name="gmail_sync",
                        status="error",
                        summary=f"Gmail sync failed: {result.get('detail', 'Unknown error')}",
                    )
                await session.commit()
        except Exception as e:
            telemetry.last_status = "error"
            telemetry.detail = f"Gmail sync failed: {e}"
            self._log_job_action(
                job_name="gmail_sync",
                status="error",
                summary=f"Gmail sync failed: {e}",
            )

    async def _run_brokerage_reconcile(self) -> None:
        telemetry = self.telemetry["brokerage_reconcile"]
        telemetry.last_run_at = datetime.now(UTC)
        runtime = RuntimeSettingsStore.load()
        if not runtime.plaid.enabled or not runtime.plaid.access_token:
            telemetry.last_status = "disabled"
            telemetry.detail = "Plaid brokerage connection is not enabled and linked."
            return
        try:
            snapshot = await asyncio.to_thread(BrokerageService.fetch_holdings_snapshot)
            async with async_session_maker() as session:
                from investos.services.portfolio import PortfolioService

                result = await PortfolioService(session).reconcile_positions(
                    snapshot["holdings"],
                    broker_cash=snapshot.get("cash"),
                    create_review_items=True,
                )
            telemetry.last_status = "ok"
            telemetry.detail = (
                f"holdings={len(snapshot['holdings'])} differences={len(result['differences'])} "
                f"reviews={result['review_items_created']}"
            )
            self._log_job_action(
                job_name="brokerage_reconcile",
                status="ok",
                summary=(
                    f"Brokerage reconciliation checked {len(snapshot['holdings'])} holding(s) "
                    f"and found {len(result['differences'])} position difference(s)."
                ),
                metadata={
                    "holding_count": len(snapshot["holdings"]),
                    "difference_count": len(result["differences"]),
                    "review_items_created": result["review_items_created"],
                    "ignored_count": len(snapshot.get("ignored") or []),
                },
            )
        except Exception as exc:
            telemetry.last_status = "error"
            telemetry.detail = str(exc)
            self._log_job_action(
                job_name="brokerage_reconcile",
                status="error",
                summary=f"Brokerage reconciliation failed: {exc}",
            )

    async def _run_strategist_cycle(self) -> None:
        telemetry = self.telemetry["strategist_cycle"]
        telemetry.last_run_at = datetime.now(UTC)
        async with async_session_maker() as session:
            try:
                result = await AgentService(session).run_strategic_planning_cycle()
                telemetry.last_status = self._result_telemetry_status(result)
                telemetry.detail = str(result["detail"])
                self._log_job_action(
                    job_name="strategist_cycle",
                    status=str(result.get("status") or "ok"),
                    summary=str(result.get("detail") or "Strategist cycle completed."),
                )
            except Exception as exc:
                telemetry.last_status = "error"
                telemetry.detail = str(exc)
                self._log_job_action(
                    job_name="strategist_cycle",
                    status="error",
                    summary=f"Strategist cycle failed: {exc}",
                )

    async def _run_pattern_discovery(self) -> None:
        telemetry = self.telemetry["pattern_discovery"]
        telemetry.last_run_at = datetime.now(UTC)
        async with async_session_maker() as session:
            try:
                result = await PatternDiscoveryService(session).discover(apply=True)
                telemetry.last_status = self._result_telemetry_status(result)
                telemetry.detail = str(
                    result.get("detail") or "pattern_discovery_complete"
                )
                self._log_job_action(
                    job_name="pattern_discovery",
                    status=str(result.get("status") or "ok"),
                    summary=(
                        f"Pattern discovery created a provisional hypothesis across "
                        f"{', '.join(result.get('affected_tickers') or [])}."
                        if result.get("created")
                        else str(
                            result.get("detail")
                            or "Pattern discovery found no supported hypothesis."
                        )
                    ),
                    metadata=result,
                )
            except Exception as exc:
                telemetry.last_status = "error"
                telemetry.detail = str(exc)
                self._log_job_action(
                    job_name="pattern_discovery",
                    status="error",
                    summary=f"Pattern discovery failed: {exc}",
                )

    async def _run_shadow_discovery(self) -> None:
        telemetry = self.telemetry["shadow_discovery"]
        telemetry.last_run_at = datetime.now(UTC)
        async with async_session_maker() as session:
            try:
                service = ShadowService(session)
                count = await service.discover_and_queue_experiments()
                telemetry.last_status = "ok"
                telemetry.detail = f"discovered_count={count}"
                if count:
                    self._log_job_action(
                        job_name="shadow_discovery",
                        status="ok",
                        summary=f"Shadow discovery queued {count} new experiments.",
                        metadata={"discovered_count": count},
                    )
            except Exception as exc:
                telemetry.last_status = "error"
                telemetry.detail = str(exc)
                self._log_job_action(
                    job_name="shadow_discovery",
                    status="error",
                    summary=f"Shadow discovery failed: {exc}",
                )

    async def _run_watcher_loop(self) -> None:
        telemetry = self.telemetry["watcher_loop"]
        telemetry.last_run_at = datetime.now(UTC)
        async with async_session_maker() as session:
            try:
                from investos.services.watcher import WatcherService

                service = WatcherService(session)
                dedupe = await service.deduplicate_active_watchers()
                count = await service.evaluate_watchers()
                telemetry.last_status = "ok"
                telemetry.detail = f"deduplicated={dedupe['deduplicated_count']} triggered_count={count}"
                if count > 0 or dedupe["deduplicated_count"] > 0:
                    self._log_job_action(
                        job_name="watcher_loop",
                        status="ok",
                        summary=(
                            f"Watcher loop deduplicated {dedupe['deduplicated_count']} active watch(es) "
                            f"and triggered {count} active catalyst(s)."
                        ),
                        metadata={
                            "deduplicated_count": dedupe["deduplicated_count"],
                            "duplicate_group_count": dedupe["duplicate_group_count"],
                            "triggered_count": count,
                            "duplicate_groups": dedupe["duplicate_groups"][:50],
                        },
                    )
            except Exception as exc:
                telemetry.last_status = "error"
                telemetry.detail = str(exc)
                self._log_job_action(
                    job_name="watcher_loop",
                    status="error",
                    summary=f"Watcher loop failed: {exc}",
                )
