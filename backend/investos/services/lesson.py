from __future__ import annotations

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from investos.models.lesson import Lesson
from investos.schemas.lesson import LessonResponse


class LessonService:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def list_lessons(self) -> list[LessonResponse]:
        lessons = (
            (
                await self.session.execute(
                    select(Lesson).order_by(desc(Lesson.created_at)).limit(100)
                )
            )
            .scalars()
            .all()
        )
        return [
            LessonResponse(
                id=lesson.id,
                title=lesson.title,
                summary=lesson.summary,
                lesson_type=lesson.lesson_type,
                applicable_sectors=lesson.applicable_sectors or [],
                applicable_regimes=lesson.applicable_regimes or [],
                originating_decision_review_id=lesson.originating_decision_review_id,
                originating_experiment_result_id=lesson.originating_experiment_result_id,
                experiment_family_id=lesson.experiment_family_id,
                maturity_status=lesson.maturity_status,
                confidence_score=lesson.confidence_score,
                supporting_observations=lesson.supporting_observations,
                contradicting_observations=lesson.contradicting_observations,
                neutral_observations=lesson.neutral_observations,
                last_validated_at=lesson.last_validated_at,
                stale_after=lesson.stale_after,
                metadata_json=lesson.metadata_json or {},
                usage_count=lesson.usage_count,
                created_at=lesson.created_at,
            )
            for lesson in lessons
        ]
