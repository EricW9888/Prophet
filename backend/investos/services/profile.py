from __future__ import annotations

from uuid import UUID

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from investos.models.conclusion import ConclusionState
from investos.models.coverage import (
    CoverageMap,
    MissingEvidenceClass,
    UnresolvedQuestion,
)
from investos.models.entity import Entity, Security
from investos.models.graph import Edge
from investos.models.knowledge import Claim, Event, Fact
from investos.models.portfolio import Position
from investos.models.profile import Profile
from investos.models.theme import Theme
from investos.schemas.profile import (
    EvidenceNodeResponse,
    MissingEvidenceResponse,
    ProfileDetailResponse,
    ProfileListItem,
    UnresolvedQuestionResponse,
)
from investos.services.fundamentals import FundamentalMetricService
from investos.services.historical import HistoricalEpisodeService
from investos.services.market_setup import MarketSetupSignalService


class ProfileService:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def list_profiles(self, *, show_all: bool = False) -> list[ProfileListItem]:
        profiles = list(
            (
                await self.session.execute(
                    select(Profile).order_by(desc(Profile.updated_at)).limit(100)
                )
            )
            .scalars()
            .all()
        )
        tracked_entity_ids = await self._tracked_entity_ids() if not show_all else set()
        items: list[ProfileListItem] = []
        for profile in profiles:
            subject_name = await self._subject_name(
                profile.subject_type, profile.subject_id
            )
            conclusion = await self._load_conclusion(
                profile.subject_type, profile.subject_id
            )
            coverage = await self._load_coverage(
                profile.subject_type, profile.subject_id
            )
            if not show_all and not self._is_portfolio_relevant(
                profile=profile,
                subject_name=subject_name,
                conclusion=conclusion,
                tracked_entity_ids=tracked_entity_ids,
            ):
                continue
            items.append(
                ProfileListItem(
                    id=profile.id,
                    subject_type=profile.subject_type,
                    subject_id=profile.subject_id,
                    subject_name=subject_name,
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

    async def _tracked_entity_ids(self) -> set[UUID]:
        rows = (
            (
                await self.session.execute(
                    select(Security.entity_id)
                    .join(Position, Position.security_id == Security.id)
                    .where(
                        Position.list_type.in_(
                            ["holding", "watchlist", "considering", "theme_basket"]
                        )
                    )
                )
            )
            .scalars()
            .all()
        )
        return set(rows)

    def _is_portfolio_relevant(
        self,
        *,
        profile: Profile,
        subject_name: str,
        conclusion: ConclusionState | None,
        tracked_entity_ids: set[UUID],
    ) -> bool:
        if profile.subject_type == "portfolio":
            return True
        if profile.subject_type == "position":
            return True
        if profile.subject_type == "entity":
            return profile.subject_id in tracked_entity_ids
        return False

    async def get_profile(self, profile_id: UUID) -> ProfileDetailResponse | None:
        profile = (
            await self.session.execute(select(Profile).where(Profile.id == profile_id))
        ).scalar_one_or_none()
        if not profile:
            return None

        subject_name = await self._subject_name(
            profile.subject_type, profile.subject_id
        )
        conclusion = await self._load_conclusion(
            profile.subject_type, profile.subject_id
        )
        coverage = await self._load_coverage(profile.subject_type, profile.subject_id)

        missing_evidence: list[MissingEvidenceResponse] = []
        unresolved_questions: list[UnresolvedQuestionResponse] = []
        if coverage:
            missing_evidence = [
                MissingEvidenceResponse.model_validate(item)
                for item in (
                    await self.session.execute(
                        select(MissingEvidenceClass)
                        .where(
                            MissingEvidenceClass.coverage_map_id == coverage.id,
                            MissingEvidenceClass.resolved_at.is_(None),
                        )
                        .order_by(desc(MissingEvidenceClass.identified_at))
                    )
                )
                .scalars()
                .all()
            ]
            unresolved_questions = [
                UnresolvedQuestionResponse.model_validate(item)
                for item in (
                    await self.session.execute(
                        select(UnresolvedQuestion)
                        .where(
                            UnresolvedQuestion.coverage_map_id == coverage.id,
                            UnresolvedQuestion.status.in_(["open", "investigating"]),
                        )
                        .order_by(
                            desc(UnresolvedQuestion.urgency),
                            desc(UnresolvedQuestion.created_at),
                        )
                    )
                )
                .scalars()
                .all()
            ]

        return ProfileDetailResponse(
            id=profile.id,
            subject_type=profile.subject_type,
            subject_id=profile.subject_id,
            subject_name=subject_name,
            executive_summary=profile.executive_summary,
            bull_case=profile.bull_case,
            bear_case=profile.bear_case,
            active_contradictions=profile.active_contradictions or [],
            current_stance=conclusion.current_stance if conclusion else None,
            confidence_band=conclusion.confidence_band if conclusion else None,
            current_thesis_summary=(
                conclusion.current_thesis_summary if conclusion else None
            ),
            what_would_falsify=(
                conclusion.what_would_falsify or [] if conclusion else []
            ),
            coverage_score=coverage.overall_coverage_score if coverage else None,
            missing_evidence=missing_evidence,
            unresolved_questions=unresolved_questions,
            recent_evidence=await self._recent_evidence(
                profile.subject_type, profile.subject_id
            ),
            historical_analogy_lenses=await self._historical_analogy_lenses(
                profile=profile,
                subject_name=subject_name,
                conclusion=conclusion,
            ),
            fundamental_metrics=await FundamentalMetricService(
                self.session
            ).relevant_metrics(
                subject_type=profile.subject_type,
                subject_id=profile.subject_id,
                limit=12,
            ),
            market_setup_signals=await MarketSetupSignalService(
                self.session
            ).relevant_signals(
                subject_type=profile.subject_type,
                subject_id=profile.subject_id,
                query=subject_name,
                limit=10,
            ),
            updated_at=profile.updated_at,
        )

    async def _historical_analogy_lenses(
        self,
        *,
        profile: Profile,
        subject_name: str,
        conclusion: ConclusionState | None,
    ) -> list[dict]:
        query = self._historical_analogy_query(
            profile=profile,
            subject_name=subject_name,
            conclusion=conclusion,
        )
        if not query:
            return []
        svc = HistoricalEpisodeService(self.session)
        analogies = await svc.find_analogies(query, limit=3)
        if not analogies:
            return []
        return HistoricalEpisodeService.application_lenses(
            analogies,
            query_text=query,
            subject_name=subject_name,
            portfolio_context=await self._portfolio_context_for_lenses(),
            limit=3,
        )

    @staticmethod
    def _historical_analogy_query(
        *,
        profile: Profile,
        subject_name: str,
        conclusion: ConclusionState | None,
    ) -> str:
        parts: list[str] = [
            subject_name,
            profile.subject_type,
            profile.executive_summary or "",
            profile.business_model or "",
            profile.bull_case or "",
            profile.bear_case or "",
            profile.key_drivers or "",
            profile.competitor_landscape or "",
            " ".join(profile.active_contradictions or []),
        ]
        if conclusion:
            parts.extend(
                [
                    conclusion.current_stance or "",
                    conclusion.current_thesis_summary or "",
                    " ".join(conclusion.what_would_falsify or []),
                ]
            )
        return " ".join(part.strip() for part in parts if part and part.strip())

    async def _portfolio_context_for_lenses(self) -> dict:
        rows = (
            await self.session.execute(
                select(Position, Security, Entity)
                .join(Security, Position.security_id == Security.id)
                .join(Entity, Security.entity_id == Entity.id)
                .where(
                    Position.list_type.in_(
                        ["holding", "watchlist", "considering", "theme_basket"]
                    )
                )
                .order_by(desc(Position.weight_pct), desc(Position.market_value))
                .limit(20)
            )
        ).all()
        return {
            "top_holdings": [
                {
                    "ticker": security.ticker,
                    "name": entity.name,
                    "entity_name": entity.name,
                    "sector": entity.sector,
                    "industry": entity.industry,
                    "weight_pct": float(position.weight_pct or 0),
                    "market_value": float(position.market_value or 0),
                }
                for position, security, entity in rows
            ]
        }

    async def _subject_name(self, subject_type: str, subject_id: UUID) -> str:
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
        if subject_type == "theme":
            theme = (
                await self.session.execute(select(Theme).where(Theme.id == subject_id))
            ).scalar_one_or_none()
            if theme:
                return theme.name
        return str(subject_id)

    async def _load_conclusion(
        self, subject_type: str, subject_id: UUID
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

    async def _load_coverage(
        self, subject_type: str, subject_id: UUID
    ) -> CoverageMap | None:
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

    async def _recent_evidence(
        self, subject_type: str, subject_id: UUID
    ) -> list[EvidenceNodeResponse]:
        edge_rows = list(
            (
                await self.session.execute(
                    select(Edge)
                    .where(
                        Edge.target_type == subject_type,
                        Edge.target_id == subject_id,
                        Edge.source_type.in_(["fact", "claim", "event"]),
                    )
                    .order_by(desc(Edge.created_at))
                    .limit(20)
                )
            )
            .scalars()
            .all()
        )

        nodes: list[EvidenceNodeResponse] = []
        for edge in edge_rows:
            if edge.source_type == "fact":
                node = (
                    await self.session.execute(
                        select(Fact).where(Fact.id == edge.source_id)
                    )
                ).scalar_one_or_none()
                if node:
                    nodes.append(
                        EvidenceNodeResponse(
                            id=node.id,
                            node_type="fact",
                            text=node.statement,
                            tier=node.tier,
                            created_at=node.created_at,
                        )
                    )
            elif edge.source_type == "claim":
                node = (
                    await self.session.execute(
                        select(Claim).where(Claim.id == edge.source_id)
                    )
                ).scalar_one_or_none()
                if node:
                    nodes.append(
                        EvidenceNodeResponse(
                            id=node.id,
                            node_type="claim",
                            text=node.statement,
                            tier=node.tier,
                            created_at=node.created_at,
                        )
                    )
            elif edge.source_type == "event":
                node = (
                    await self.session.execute(
                        select(Event).where(Event.id == edge.source_id)
                    )
                ).scalar_one_or_none()
                if node:
                    nodes.append(
                        EvidenceNodeResponse(
                            id=node.id,
                            node_type="event",
                            text=node.description or node.title,
                            tier="event",
                            created_at=node.created_at,
                        )
                    )
        nodes.sort(key=lambda item: item.created_at, reverse=True)
        return nodes[:10]
