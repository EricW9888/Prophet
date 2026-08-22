from __future__ import annotations

from uuid import UUID

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from investos.models.conclusion import ConclusionState
from investos.models.decision import DecisionJournal, DecisionReview
from investos.models.entity import Entity, Security
from investos.models.lesson import Lesson
from investos.models.portfolio import Position
from investos.models.reasoning import EvidencePacket
from investos.schemas.decision import (
    DecisionJournalCreate,
    DecisionJournalResponse,
    DecisionReviewCreate,
    DecisionReviewResponse,
)
from investos.schemas.lesson import LessonResponse
from investos.services.retrieval import RetrievalService


class DecisionService:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def list_decisions(self) -> list[DecisionJournalResponse]:
        decisions = list(
            (
                await self.session.execute(
                    select(DecisionJournal)
                    .order_by(desc(DecisionJournal.created_at))
                    .limit(100)
                )
            )
            .scalars()
            .all()
        )
        return [await self._serialize_decision(item) for item in decisions]

    async def create_decision(
        self, payload: DecisionJournalCreate
    ) -> DecisionJournalResponse:
        conclusion_state_id = None
        evidence_packet_id = None
        if payload.position_id:
            position = (
                await self.session.execute(
                    select(Position).where(Position.id == payload.position_id)
                )
            ).scalar_one_or_none()
            if position:
                conclusion = (
                    await self.session.execute(
                        select(ConclusionState).where(
                            ConclusionState.subject_type == "position",
                            ConclusionState.subject_id == position.id,
                        )
                    )
                ).scalar_one_or_none()
                conclusion_state_id = conclusion.id if conclusion else None
                packet = await RetrievalService(self.session).retrieve_evidence(
                    query=f"Decision journal context for {payload.decision_type}",
                    subject_id=position.id,
                    subject_type="position",
                    max_depth=4,
                )
                evidence_packet_id = packet.id

        journal = DecisionJournal(
            position_id=payload.position_id,
            decision_type=payload.decision_type,
            rationale=payload.rationale,
            expected_catalyst_timeframe=payload.expected_catalyst_timeframe,
            expected_return=payload.expected_return,
            conclusion_state_id=conclusion_state_id,
            evidence_packet_id=evidence_packet_id,
        )
        self.session.add(journal)
        await self.session.commit()
        await self.session.refresh(journal)
        return await self._serialize_decision(journal)

    async def create_review(
        self, payload: DecisionReviewCreate
    ) -> DecisionReviewResponse:
        decision = (
            await self.session.execute(
                select(DecisionJournal).where(
                    DecisionJournal.id == payload.decision_journal_id
                )
            )
        ).scalar_one_or_none()
        if decision is None:
            raise ValueError("Decision journal not found.")
        review = DecisionReview(
            decision_journal_id=payload.decision_journal_id,
            outcome_assessment=payload.outcome_assessment,
            actual_return=payload.actual_return,
            mistake_preventable=payload.mistake_preventable,
            what_went_right=payload.what_went_right,
            what_went_wrong=payload.what_went_wrong,
            what_to_improve=payload.what_to_improve,
        )
        self.session.add(review)
        await self.session.flush()
        lessons = await self._extract_lessons_from_review(review)
        await self.session.commit()
        await self.session.refresh(review)
        return DecisionReviewResponse(
            id=review.id,
            decision_journal_id=review.decision_journal_id,
            outcome_assessment=review.outcome_assessment,
            actual_return=review.actual_return,
            mistake_preventable=review.mistake_preventable,
            what_went_right=review.what_went_right,
            what_went_wrong=review.what_went_wrong,
            what_to_improve=review.what_to_improve,
            extracted_lessons=lessons,
            reviewed_at=review.reviewed_at,
        )

    async def _serialize_decision(
        self, decision: DecisionJournal
    ) -> DecisionJournalResponse:
        reviews = list(
            (
                await self.session.execute(
                    select(DecisionReview)
                    .where(DecisionReview.decision_journal_id == decision.id)
                    .order_by(desc(DecisionReview.reviewed_at))
                )
            )
            .scalars()
            .all()
        )
        lesson_map = await self._lessons_for_reviews([review.id for review in reviews])
        return DecisionJournalResponse(
            id=decision.id,
            position_id=decision.position_id,
            position_label=await self._position_label(decision.position_id),
            decision_type=decision.decision_type,
            rationale=decision.rationale,
            expected_catalyst_timeframe=decision.expected_catalyst_timeframe,
            expected_return=decision.expected_return,
            created_at=decision.created_at,
            reviews=[
                DecisionReviewResponse(
                    id=review.id,
                    decision_journal_id=review.decision_journal_id,
                    outcome_assessment=review.outcome_assessment,
                    actual_return=review.actual_return,
                    mistake_preventable=review.mistake_preventable,
                    what_went_right=review.what_went_right,
                    what_went_wrong=review.what_went_wrong,
                    what_to_improve=review.what_to_improve,
                    extracted_lessons=lesson_map.get(review.id, []),
                    reviewed_at=review.reviewed_at,
                )
                for review in reviews
            ],
        )

    async def _position_label(self, position_id: UUID | None) -> str | None:
        if not position_id:
            return None
        position = (
            await self.session.execute(
                select(Position).where(Position.id == position_id)
            )
        ).scalar_one_or_none()
        if not position:
            return None
        security = (
            await self.session.execute(
                select(Security).where(Security.id == position.security_id)
            )
        ).scalar_one_or_none()
        if not security:
            return str(position.id)
        entity = (
            await self.session.execute(
                select(Entity).where(Entity.id == security.entity_id)
            )
        ).scalar_one_or_none()
        return security.ticker if not entity else f"{security.ticker} · {entity.name}"

    async def _extract_lessons_from_review(
        self, review: DecisionReview
    ) -> list[LessonResponse]:
        candidates: list[tuple[str, str, str]] = []
        if review.what_to_improve:
            candidates.append(
                (
                    "Improve next time",
                    review.what_to_improve.strip(),
                    "analytical_error",
                )
            )
        if review.what_went_wrong:
            candidates.append(
                (
                    "What went wrong",
                    review.what_went_wrong.strip(),
                    "bias",
                )
            )
        if review.what_went_right:
            candidates.append(
                (
                    "What worked",
                    review.what_went_right.strip(),
                    "market_mechanic",
                )
            )

        lessons: list[LessonResponse] = []
        created_ids: list[UUID] = []
        for title_prefix, summary, lesson_type in candidates[:3]:
            title = f"{title_prefix}: {summary[:72].rstrip()}"
            lesson = Lesson(
                title=title,
                summary=summary,
                lesson_type=lesson_type,
                originating_decision_review_id=review.id,
            )
            self.session.add(lesson)
            await self.session.flush()
            created_ids.append(lesson.id)
            lessons.append(
                LessonResponse(
                    id=lesson.id,
                    title=lesson.title,
                    summary=lesson.summary,
                    lesson_type=lesson.lesson_type,
                    applicable_sectors=lesson.applicable_sectors or [],
                    applicable_regimes=lesson.applicable_regimes or [],
                    originating_decision_review_id=lesson.originating_decision_review_id,
                    originating_experiment_result_id=lesson.originating_experiment_result_id,
                    usage_count=lesson.usage_count,
                    created_at=lesson.created_at,
                )
            )
        review.extracted_lesson_ids = created_ids or None
        return lessons

    async def _lessons_for_reviews(
        self, review_ids: list[UUID]
    ) -> dict[UUID, list[LessonResponse]]:
        if not review_ids:
            return {}
        lessons = (
            (
                await self.session.execute(
                    select(Lesson).where(
                        Lesson.originating_decision_review_id.in_(review_ids)
                    )
                )
            )
            .scalars()
            .all()
        )
        output: dict[UUID, list[LessonResponse]] = {}
        for lesson in lessons:
            if lesson.originating_decision_review_id is None:
                continue
            output.setdefault(lesson.originating_decision_review_id, []).append(
                LessonResponse(
                    id=lesson.id,
                    title=lesson.title,
                    summary=lesson.summary,
                    lesson_type=lesson.lesson_type,
                    applicable_sectors=lesson.applicable_sectors or [],
                    applicable_regimes=lesson.applicable_regimes or [],
                    originating_decision_review_id=lesson.originating_decision_review_id,
                    originating_experiment_result_id=lesson.originating_experiment_result_id,
                    usage_count=lesson.usage_count,
                    created_at=lesson.created_at,
                )
            )
        return output
