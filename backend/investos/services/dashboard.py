from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from investos.models.conclusion import ConclusionState
from investos.models.coverage import CoverageMap, UnresolvedQuestion
from investos.models.entity import Entity, Security
from investos.models.evidence import RawEvidence
from investos.models.knowledge import Claim, Event, Fact
from investos.models.lesson import Lesson
from investos.models.portfolio import Position, Transaction
from investos.models.profile import Profile
from investos.models.reasoning import ReasoningRun
from investos.models.review import ReviewQueueItem
from investos.models.shadow import ExperimentResult, ShadowExperiment
from investos.models.source import Source
from investos.schemas.automation import AutomationJobStatus
from investos.schemas.dashboard import (
    DashboardAgentActionResponse,
    DashboardLessonResponse,
    DashboardLlmUsageResponse,
    DashboardPortfolioMonitorResponse,
    DashboardQuestionResponse,
    DashboardResearchActionResponse,
    DashboardResearchActivityResponse,
    DashboardShadowSummaryResponse,
    DashboardSourceResponse,
    DashboardSummaryResponse,
    DashboardTransactionResponse,
)
from investos.schemas.profile import EvidenceNodeResponse, ProfileListItem
from investos.schemas.review import ReviewQueueItemResponse
from investos.services.agent_action_log import AgentActionLogService
from investos.services.operating_state import OperatingStateService
from investos.services.portfolio import PortfolioService
from investos.services.research import ResearchService
from investos.services.review import ReviewService
from investos.services.risk import RiskService
from investos.services.runtime_settings import RuntimeSettingsStore
from investos.services.transaction_provenance import transaction_source_summary


class DashboardService:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_summary(
        self,
        *,
        automation_enabled: bool,
        jobs: list[AutomationJobStatus],
    ) -> DashboardSummaryResponse:
        portfolio_overview = await PortfolioService(self.session).overview()
        holdings_count = len(portfolio_overview.holdings)
        watchlist_count = len(portfolio_overview.watchlist)
        considering_count = len(portfolio_overview.considering)
        total_market_value = sum(
            (float(h.market_value or 0.0) for h in portfolio_overview.holdings), 0.0
        )
        total_unrealized_pnl = sum(
            (float(h.unrealized_pnl or 0.0) for h in portfolio_overview.holdings), 0.0
        )
        portfolio_build_series = portfolio_overview.build_series

        profile_count = await self._scalar_count(select(Profile))
        evidence_node_count = sum(
            [
                await self._scalar_count(select(Fact)),
                await self._scalar_count(select(Claim)),
                await self._scalar_count(select(Event)),
            ]
        )
        active_evidence_node_count = sum(
            [
                await self._scalar_count(
                    select(Fact).where(Fact.is_deprecated.is_(False))
                ),
                await self._scalar_count(
                    select(Claim).where(Claim.is_deprecated.is_(False))
                ),
                await self._scalar_count(
                    select(Event).where(Event.is_deprecated.is_(False))
                ),
            ]
        )
        deprecated_evidence_node_count = sum(
            [
                await self._scalar_count(
                    select(Fact).where(Fact.is_deprecated.is_(True))
                ),
                await self._scalar_count(
                    select(Claim).where(Claim.is_deprecated.is_(True))
                ),
                await self._scalar_count(
                    select(Event).where(Event.is_deprecated.is_(True))
                ),
            ]
        )
        open_questions_count = await self._scalar_count(
            select(UnresolvedQuestion).where(UnresolvedQuestion.status == "open")
        )
        pending_shadow_experiments_count = await self._scalar_count(
            select(ShadowExperiment).where(
                ShadowExperiment.run_status.in_(["queued", "pending"])
            )
        )
        risk_summary = await RiskService(self.session).get_summary(refresh=False)

        return DashboardSummaryResponse(
            as_of=datetime.now(UTC),
            holdings_count=holdings_count,
            watchlist_count=watchlist_count,
            considering_count=considering_count,
            total_market_value=total_market_value,
            total_unrealized_pnl=total_unrealized_pnl,
            total_value=portfolio_overview.total_value,
            buying_power=portfolio_overview.buying_power,
            top_winners=portfolio_overview.top_winners,
            top_losers=portfolio_overview.top_losers,
            portfolio_build_series=portfolio_build_series,
            profile_count=profile_count,
            evidence_node_count=evidence_node_count,
            active_evidence_node_count=active_evidence_node_count,
            deprecated_evidence_node_count=deprecated_evidence_node_count,
            open_questions_count=open_questions_count,
            pending_shadow_experiments_count=pending_shadow_experiments_count,
            automation_enabled=automation_enabled,
            jobs=jobs,
            recent_transactions=await self._recent_transactions(),
            recent_evidence=await self._recent_evidence(),
            recent_profiles=await self._recent_profiles(),
            open_questions=await self._open_questions(),
            review_queue=await self._review_queue(),
            recent_lessons=await self._recent_lessons(),
            trusted_sources=await self._trusted_sources(),
            research_activity=await self._research_activity(jobs),
            recent_research_actions=self._recent_research_actions(),
            recent_agent_actions=self._recent_agent_actions(),
            portfolio_monitor=await self._portfolio_monitor(),
            recent_shadow_experiments=await self._recent_shadow_experiments(),
            llm_usage=await self._llm_usage(),
            active_benchmark_ticker=(
                None
                if risk_summary.active_benchmark is None
                else risk_summary.active_benchmark.ticker
            ),
            portfolio_return_pct=risk_summary.portfolio_return_pct,
            benchmark_return_pct=risk_summary.benchmark_return_pct,
            active_return_pct=risk_summary.active_return_pct,
            top_sector=risk_summary.top_sector,
            top_sector_weight_pct=risk_summary.top_sector_weight_pct,
            current_regime=(
                None
                if risk_summary.current_regime is None
                else risk_summary.current_regime.regime_type
            ),
        )

    async def _scalar_count(self, stmt) -> int:
        count_stmt = select(func.count()).select_from(stmt.subquery())
        return int((await self.session.execute(count_stmt)).scalar_one() or 0)

    async def _count_positions(
        self, list_type: str, require_open_quantity: bool = False
    ) -> int:
        stmt = select(Position).where(Position.list_type == list_type)
        if require_open_quantity:
            stmt = stmt.where(Position.quantity > 0)
        return await self._scalar_count(stmt)

    async def _sum_position_field(self, field) -> float:
        value = (
            await self.session.execute(
                select(func.coalesce(func.sum(field), 0)).where(
                    Position.list_type == "holding"
                )
            )
        ).scalar_one()
        return float(value or 0.0)

    async def _recent_transactions(self) -> list[DashboardTransactionResponse]:
        rows = (
            await self.session.execute(
                select(Transaction, Security, Entity)
                .outerjoin(Position, Transaction.position_id == Position.id)
                .outerjoin(Security, Position.security_id == Security.id)
                .outerjoin(Entity, Security.entity_id == Entity.id)
                .order_by(desc(Transaction.executed_at))
                .limit(6)
            )
        ).all()
        return [
            DashboardTransactionResponse(
                id=txn.id,
                ticker=getattr(security, "ticker", None)
                or ("CASH" if txn.position_id is None else "UNKNOWN"),
                entity_name=getattr(entity, "name", None),
                action=txn.action,
                quantity=float(txn.quantity),
                price=None if txn.price is None else float(txn.price),
                executed_at=txn.executed_at,
                **self._transaction_source_summary(txn),
            )
            for txn, security, entity in rows
        ]

    @staticmethod
    def _transaction_source_summary(txn: Transaction) -> dict[str, object]:
        return transaction_source_summary(txn)

    async def _recent_evidence(self) -> list[EvidenceNodeResponse]:
        items: list[EvidenceNodeResponse] = []

        facts = (
            (
                await self.session.execute(
                    select(Fact).order_by(desc(Fact.created_at)).limit(6)
                )
            )
            .scalars()
            .all()
        )
        items.extend(
            [
                EvidenceNodeResponse(
                    id=fact.id,
                    node_type="fact",
                    text=fact.statement,
                    tier=fact.tier,
                    created_at=fact.created_at,
                )
                for fact in facts
            ]
        )

        claims = (
            (
                await self.session.execute(
                    select(Claim).order_by(desc(Claim.created_at)).limit(6)
                )
            )
            .scalars()
            .all()
        )
        items.extend(
            [
                EvidenceNodeResponse(
                    id=claim.id,
                    node_type="claim",
                    text=claim.statement,
                    tier=claim.tier,
                    created_at=claim.created_at,
                )
                for claim in claims
            ]
        )

        events = (
            (
                await self.session.execute(
                    select(Event).order_by(desc(Event.created_at)).limit(6)
                )
            )
            .scalars()
            .all()
        )
        items.extend(
            [
                EvidenceNodeResponse(
                    id=event.id,
                    node_type="event",
                    text=event.description or event.title,
                    tier="event",
                    created_at=event.created_at,
                )
                for event in events
            ]
        )

        items.sort(key=lambda item: item.created_at, reverse=True)
        return items[:8]

    async def _recent_profiles(self) -> list[ProfileListItem]:
        profiles = (
            (
                await self.session.execute(
                    select(Profile).order_by(desc(Profile.updated_at)).limit(6)
                )
            )
            .scalars()
            .all()
        )

        items: list[ProfileListItem] = []
        for profile in profiles:
            conclusion = await self._load_conclusion(
                profile.subject_type, profile.subject_id
            )
            coverage = await self._load_coverage(
                profile.subject_type, profile.subject_id
            )
            items.append(
                ProfileListItem(
                    id=profile.id,
                    subject_type=profile.subject_type,
                    subject_id=profile.subject_id,
                    subject_name=await self._subject_name(
                        profile.subject_type, profile.subject_id
                    ),
                    executive_summary=profile.executive_summary,
                    current_stance=conclusion.current_stance if conclusion else None,
                    confidence_band=conclusion.confidence_band if conclusion else None,
                    coverage_score=(
                        coverage.overall_coverage_score if coverage else None
                    ),
                    updated_at=profile.updated_at,
                )
            )
        return items

    async def _open_questions(self) -> list[DashboardQuestionResponse]:
        questions = (
            await self.session.execute(
                select(UnresolvedQuestion, CoverageMap)
                .join(CoverageMap, UnresolvedQuestion.coverage_map_id == CoverageMap.id)
                .where(UnresolvedQuestion.status == "open")
                .order_by(
                    desc(UnresolvedQuestion.urgency),
                    desc(UnresolvedQuestion.created_at),
                )
                .limit(6)
            )
        ).all()
        return [
            DashboardQuestionResponse(
                id=question.id,
                subject_type=coverage.subject_type,
                subject_name=await self._subject_name(
                    coverage.subject_type, coverage.subject_id
                ),
                question_text=question.question_text,
                urgency=question.urgency,
                created_at=question.created_at,
            )
            for question, coverage in questions
        ]

    async def _recent_shadow_experiments(self) -> list[DashboardShadowSummaryResponse]:
        rows = (
            await self.session.execute(
                select(ShadowExperiment, ExperimentResult)
                .outerjoin(
                    ExperimentResult,
                    ExperimentResult.experiment_id == ShadowExperiment.id,
                )
                .order_by(desc(ShadowExperiment.created_at))
                .limit(5)
            )
        ).all()
        return [
            DashboardShadowSummaryResponse(
                id=experiment.id,
                name=experiment.name,
                policy_description=experiment.policy_description,
                run_status=experiment.run_status,
                created_at=experiment.created_at,
                completed_at=experiment.completed_at,
                alpha=None if result is None else result.alpha,
                shadow_return=None if result is None else result.shadow_return,
                actual_return=None if result is None else result.actual_return,
            )
            for experiment, result in rows
        ]

    async def _review_queue(self) -> list[ReviewQueueItemResponse]:
        items = (
            (
                await self.session.execute(
                    select(ReviewQueueItem)
                    .where(ReviewQueueItem.status.in_(["pending", "in_review"]))
                    .order_by(
                        desc(ReviewQueueItem.priority_score),
                        desc(ReviewQueueItem.created_at),
                    )
                    .limit(6)
                )
            )
            .scalars()
            .all()
        )
        service = ReviewService(self.session)
        return [await service._serialize(item) for item in items]

    async def _recent_lessons(self) -> list[DashboardLessonResponse]:
        lessons = (
            (
                await self.session.execute(
                    select(Lesson).order_by(desc(Lesson.created_at)).limit(6)
                )
            )
            .scalars()
            .all()
        )
        return [
            DashboardLessonResponse(
                id=lesson.id,
                title=lesson.title,
                summary=lesson.summary,
                lesson_type=lesson.lesson_type,
                created_at=lesson.created_at,
            )
            for lesson in lessons
        ]

    async def _trusted_sources(self) -> list[DashboardSourceResponse]:
        sources = (
            (
                await self.session.execute(
                    select(Source)
                    .where(Source.is_trusted.is_(True))
                    .order_by(desc(Source.updated_at))
                    .limit(6)
                )
            )
            .scalars()
            .all()
        )
        return [
            DashboardSourceResponse(
                id=source.id,
                name=source.name,
                source_type=source.source_type,
                is_trusted=source.is_trusted,
                updated_at=source.updated_at,
            )
            for source in sources
        ]

    async def _research_activity(
        self, jobs: list[AutomationJobStatus]
    ) -> DashboardResearchActivityResponse:
        runtime = RuntimeSettingsStore.load()
        pending_evidence_count = await self._scalar_count(
            select(RawEvidence).where(RawEvidence.is_processed.is_(False))
        )
        research_loop = next((job for job in jobs if job.name == "research_loop"), None)
        latest_research = (
            await self.session.execute(
                select(RawEvidence)
                .where(RawEvidence.source_item_type == "web_research")
                .order_by(desc(RawEvidence.created_at))
                .limit(1)
            )
        ).scalar_one_or_none()
        subject_name = None
        if latest_research is not None:
            metadata = latest_research.metadata_json or {}
            raw_subject_id = metadata.get("subject_id")
            raw_subject_type = metadata.get("subject_type")
            if raw_subject_id and raw_subject_type:
                try:
                    subject_name = await self._subject_name(
                        str(raw_subject_type), raw_subject_id
                    )
                except Exception as exc:
                    import logging

                    logging.getLogger(__name__).exception("Masked failure caught")
                    subject_name = None
        latest_detail = (
            None
            if research_loop is None
            else self._humanize_research_detail(
                research_loop.detail,
                subject_name=subject_name,
                latest_title=(
                    latest_research.title if latest_research is not None else None
                ),
            )
        )
        return DashboardResearchActivityResponse(
            automation_enabled=True,
            provider_configured=bool(runtime.research.api_key),
            open_question_count=await self._scalar_count(
                select(UnresolvedQuestion).where(UnresolvedQuestion.status == "open")
            ),
            pending_evidence_count=pending_evidence_count,
            latest_run_at=None if research_loop is None else research_loop.last_run_at,
            latest_status=None if research_loop is None else research_loop.last_status,
            latest_detail=latest_detail,
            latest_item_title=(
                None if latest_research is None else latest_research.title
            ),
            latest_item_subject_name=subject_name,
            latest_item_created_at=(
                None if latest_research is None else latest_research.created_at
            ),
            latest_item_processed=(
                False if latest_research is None else latest_research.is_processed
            ),
        )

    def _humanize_research_detail(
        self,
        detail: str | None,
        *,
        subject_name: str | None,
        latest_title: str | None,
    ) -> str | None:
        if not detail:
            return None

        clean_subject = subject_name or "the portfolio"
        if "research_failed" in detail:
            if latest_title:
                return f"Latest research for {clean_subject} needs another pass: {latest_title}."
            return f"Latest research for {clean_subject} needs another pass."
        if "research_ok" in detail or detail.strip().lower() == "ok":
            if latest_title:
                return f"Latest research completed for {clean_subject}: {latest_title}."
            return f"Latest research completed for {clean_subject}."
        if detail.startswith("question="):
            if latest_title:
                return (
                    f"Working from queued research for {clean_subject}: {latest_title}."
                )
            return f"Working from queued research for {clean_subject}."
        return detail.replace("_", " ")

    async def _portfolio_monitor(self) -> DashboardPortfolioMonitorResponse:
        payload = await OperatingStateService(self.session).portfolio_monitor_payload()
        return DashboardPortfolioMonitorResponse.model_validate(payload)

    def _recent_research_actions(self) -> list[DashboardResearchActionResponse]:
        items = ResearchService.recent_request_log(limit=4)
        return [
            DashboardResearchActionResponse(
                timestamp=str(item.get("timestamp") or ""),
                status=str(item.get("status") or "unknown"),
                summary=self._summarize_research_action(item),
                title=item.get("title"),
                query=item.get("query"),
                search_depth=item.get("search_depth"),
            )
            for item in items
        ]

    def _summarize_research_action(self, item: dict) -> str:
        status = str(item.get("status") or "unknown").strip().lower()
        title = item.get("title") or item.get("query") or "external research"
        clean_title = (
            str(title)
            .replace("Research on: ", "")
            .replace("Ad hoc portfolio research: ", "")
            .strip()
        )
        search_depth = item.get("search_depth")
        depth_suffix = f" · {search_depth}" if search_depth else ""
        if status in {"ok", "research_ok"}:
            return f"Searched {clean_title}{depth_suffix}"
        if status == "processed_with_errors":
            return f"Searched {clean_title}{depth_suffix} with downstream processing issues"
        if status in {"research_failed", "error"}:
            return f"Search failed for {clean_title}"
        if status == "no_result":
            return f"No results for {clean_title}"
        if status == "empty_result":
            return f"Empty result for {clean_title}"
        if status == "not_configured":
            return f"Research blocked for {clean_title}"
        return f"External research on {clean_title}"

    def _recent_agent_actions(self) -> list[DashboardAgentActionResponse]:
        items = self._merge_related_research_failures(
            AgentActionLogService.recent(limit=60)
        )
        curated: list[dict] = []
        seen_non_routine: set[tuple[str, str, str, str, str]] = set()
        seen_routine: set[tuple[str, str, str, str]] = set()
        routine_counts: dict[tuple[str, str, str, str], int] = {}

        for item in items:
            signature = (
                str(item.get("source") or ""),
                str(item.get("action_type") or ""),
                str(item.get("status") or ""),
                str(item.get("summary") or ""),
            )
            if self._is_routine_action(item):
                routine_counts[signature] = routine_counts.get(signature, 0) + 1

        for item in items:
            if self._is_routine_action(item):
                continue
            signature = (
                str(item.get("source") or ""),
                str(item.get("action_type") or ""),
                str(item.get("status") or ""),
                str(item.get("subject_name") or ""),
                str(item.get("summary") or ""),
            )
            if signature in seen_non_routine:
                continue
            seen_non_routine.add(signature)
            curated.append(item)
            if len(curated) >= 4:
                break

        for item in items:
            if not self._is_routine_action(item):
                continue
            signature = (
                str(item.get("source") or ""),
                str(item.get("action_type") or ""),
                str(item.get("status") or ""),
                str(item.get("summary") or ""),
            )
            if signature in seen_routine:
                continue
            seen_routine.add(signature)
            if routine_counts.get(signature, 1) > 1:
                metadata = (
                    item.get("metadata")
                    if isinstance(item.get("metadata"), dict)
                    else {}
                )
                item = {
                    **item,
                    "summary": f"{item.get('summary') or ''} Repeated {routine_counts[signature]} times recently.",
                    "metadata": {
                        **metadata,
                        "recent_count": routine_counts[signature],
                    },
                }
            curated.append(item)
            if len(curated) >= 6:
                break

        return [self._dashboard_action_response(item) for item in curated]

    def _merge_related_research_failures(self, items: list[dict]) -> list[dict]:
        merged: list[dict] = []
        skip_ids: set[str] = set()

        for item in items:
            item_id = str(item.get("id") or "")
            if item_id and item_id in skip_ids:
                continue

            metadata = (
                item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
            )
            question_id = str(metadata.get("question_id") or "").strip()
            status = str(item.get("status") or "").strip().lower()
            action_type = str(item.get("action_type") or "").strip().lower()

            if not question_id or status not in {
                "research_failed",
                "empty_result",
                "no_result",
            }:
                merged.append(item)
                continue

            partner_index = None
            for index, candidate in enumerate(items):
                candidate_id = str(candidate.get("id") or "")
                if (
                    not candidate_id
                    or candidate_id == item_id
                    or candidate_id in skip_ids
                ):
                    continue
                candidate_metadata = (
                    candidate.get("metadata")
                    if isinstance(candidate.get("metadata"), dict)
                    else {}
                )
                candidate_question_id = str(
                    candidate_metadata.get("question_id") or ""
                ).strip()
                if candidate_question_id != question_id:
                    continue
                candidate_status = str(candidate.get("status") or "").strip().lower()
                if candidate_status not in {
                    "research_failed",
                    "empty_result",
                    "no_result",
                }:
                    continue
                candidate_action = (
                    str(candidate.get("action_type") or "").strip().lower()
                )
                if {action_type, candidate_action} == {
                    "external_research",
                    "research_loop",
                }:
                    partner_index = index
                    break

            if partner_index is None:
                merged.append(item)
                continue

            partner = items[partner_index]
            partner_id = str(partner.get("id") or "")
            if partner_id:
                skip_ids.add(partner_id)

            query = (
                str(metadata.get("query") or "").strip()
                or str(metadata.get("title") or "").replace("Research on: ", "").strip()
            )
            if not query:
                partner_metadata = (
                    partner.get("metadata")
                    if isinstance(partner.get("metadata"), dict)
                    else {}
                )
                query = (
                    str(partner_metadata.get("query") or "").strip()
                    or str(partner_metadata.get("title") or "")
                    .replace("Research on: ", "")
                    .strip()
                )
            if not query:
                query = "research question"

            external = item if action_type == "external_research" else partner
            external_summary = str(external.get("summary") or "").strip()
            merged.append(
                {
                    **item,
                    "id": (
                        f"{item_id}:{partner_id}" if item_id or partner_id else item_id
                    ),
                    "source": "research",
                    "action_type": "research_attempt",
                    "status": status,
                    "summary": (
                        f"Research attempt for {query} failed during external retrieval or analysis."
                        + (f" {external_summary}" if external_summary else "")
                    ),
                    "metadata": {
                        **metadata,
                        "paired_actions": [
                            str(item.get("action_type") or ""),
                            str(partner.get("action_type") or ""),
                        ],
                        "question_id": question_id,
                        "query": query,
                    },
                }
            )

        return merged

    def _is_routine_action(self, item: dict) -> bool:
        return (
            str(item.get("source") or "") == "automation"
            and str(item.get("status") or "") == "ok"
            and str(item.get("action_type") or "")
            in {"market_data_refresh", "risk_refresh", "database_backup"}
        )

    def _dashboard_action_response(self, item: dict) -> DashboardAgentActionResponse:
        metadata = item.get("metadata")
        return DashboardAgentActionResponse(
            id=str(item.get("id") or ""),
            timestamp=str(item.get("timestamp") or ""),
            source=str(item.get("source") or "system"),
            action_type=str(item.get("action_type") or "activity"),
            status=str(item.get("status") or "ok"),
            summary=str(item.get("summary") or ""),
            subject_id=(
                None if item.get("subject_id") is None else str(item.get("subject_id"))
            ),
            subject_type=(
                None
                if item.get("subject_type") is None
                else str(item.get("subject_type"))
            ),
            subject_name=(
                None
                if item.get("subject_name") is None
                else str(item.get("subject_name"))
            ),
            metadata=metadata if isinstance(metadata, dict) else {},
        )

    def recent_agent_activity(
        self,
        *,
        limit: int = 100,
        source: str | None = None,
        action_type: str | None = None,
        status: str | None = None,
        session_id: str | None = None,
    ) -> list[DashboardAgentActionResponse]:
        return [
            self._dashboard_action_response(item)
            for item in AgentActionLogService.recent(
                limit=limit,
                source=source,
                action_type=action_type,
                status=status,
                session_id=session_id,
            )
        ]

    async def _llm_usage(self) -> DashboardLlmUsageResponse:
        since = datetime.now(UTC) - timedelta(hours=24)
        runs = (
            (
                await self.session.execute(
                    select(ReasoningRun).where(ReasoningRun.created_at >= since)
                )
            )
            .scalars()
            .all()
        )
        analysis_runs = [run for run in runs if run.run_type == "analysis"]
        cached_runs = [
            run for run in analysis_runs if run.model_used.startswith("cached:")
        ]
        verification_runs = [run for run in runs if run.run_type == "verification"]
        durations = [
            float(run.duration_ms or 0) for run in runs if run.duration_ms is not None
        ]
        return DashboardLlmUsageResponse(
            analysis_runs_24h=len(analysis_runs),
            cached_runs_24h=len(cached_runs),
            verification_runs_24h=len(verification_runs),
            total_input_tokens_24h=sum(int(run.input_tokens or 0) for run in runs),
            total_output_tokens_24h=sum(int(run.output_tokens or 0) for run in runs),
            avg_duration_ms=(
                0.0 if not durations else round(sum(durations) / len(durations), 1)
            ),
        )

    async def _load_conclusion(
        self, subject_type: str, subject_id
    ) -> ConclusionState | None:
        return (
            await self.session.execute(
                select(ConclusionState)
                .where(
                    ConclusionState.subject_type == subject_type,
                    ConclusionState.subject_id == subject_id,
                )
                .order_by(
                    desc(ConclusionState.last_updated_at),
                    desc(ConclusionState.last_verified_at),
                    desc(ConclusionState.id),
                )
                .limit(1)
            )
        ).scalar_one_or_none()

    async def _load_coverage(self, subject_type: str, subject_id) -> CoverageMap | None:
        return (
            await self.session.execute(
                select(CoverageMap)
                .where(
                    CoverageMap.subject_type == subject_type,
                    CoverageMap.subject_id == subject_id,
                )
                .order_by(desc(CoverageMap.last_computed_at), desc(CoverageMap.id))
                .limit(1)
            )
        ).scalar_one_or_none()

    async def _subject_name(self, subject_type: str, subject_id) -> str:
        if subject_type == "portfolio":
            return "Portfolio"
        if subject_type == "entity":
            entity = (
                await self.session.execute(
                    select(Entity).where(Entity.id == subject_id)
                )
            ).scalar_one_or_none()
            if entity:
                return entity.name
        if subject_type == "theme":
            from investos.models.theme import Theme

            theme = (
                await self.session.execute(select(Theme).where(Theme.id == subject_id))
            ).scalar_one_or_none()
            if theme:
                return theme.name
        if subject_type == "position":
            position = (
                await self.session.execute(
                    select(Position).where(Position.id == subject_id)
                )
            ).scalar_one_or_none()
            if position:
                security = (
                    await self.session.execute(
                        select(Security).where(Security.id == position.security_id)
                    )
                ).scalar_one_or_none()
                if security:
                    entity = (
                        await self.session.execute(
                            select(Entity).where(Entity.id == security.entity_id)
                        )
                    ).scalar_one_or_none()
                    return (
                        security.ticker
                        if not entity
                        else f"{security.ticker} · {entity.name}"
                    )
        return str(subject_id)
