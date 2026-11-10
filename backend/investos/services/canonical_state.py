from __future__ import annotations

from collections.abc import Callable
from uuid import UUID

from sqlalchemy import desc, select, tuple_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from investos.models.conclusion import ConclusionState
from investos.models.coverage import CoverageMap


class CanonicalStateService:
    """Central access to current per-subject derived state.

    Coverage and conclusion rows are intended to be canonical single-row
    summaries for a given subject. This service keeps the read/write path
    explicit so callers do not have to guess which row is current.
    """

    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_conclusion_state(
        self, *, subject_type: str, subject_id: UUID
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

    async def get_coverage_map(
        self, *, subject_type: str, subject_id: UUID
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

    async def conclusions_for_subjects(
        self, subjects: list[tuple[str, UUID]]
    ) -> dict[tuple[str, UUID], ConclusionState]:
        rows = await self.session.scalars(
            select(ConclusionState).where(
                tuple_(ConclusionState.subject_type, ConclusionState.subject_id).in_(
                    subjects
                )
            )
        )
        return {(row.subject_type, row.subject_id): row for row in rows}

    async def coverage_maps_for_subjects(
        self, subjects: list[tuple[str, UUID]]
    ) -> dict[tuple[str, UUID], CoverageMap]:
        rows = await self.session.scalars(
            select(CoverageMap).where(
                tuple_(CoverageMap.subject_type, CoverageMap.subject_id).in_(subjects)
            )
        )
        return {(row.subject_type, row.subject_id): row for row in rows}

    async def ensure_conclusion_state(
        self,
        *,
        subject_type: str,
        subject_id: UUID,
        create: Callable[[], ConclusionState],
    ) -> ConclusionState:
        state = await self.get_conclusion_state(
            subject_type=subject_type, subject_id=subject_id
        )
        if state is not None:
            return state
        candidate = create()
        try:
            async with self.session.begin_nested():
                self.session.add(candidate)
                await self.session.flush()
            return candidate
        except IntegrityError:
            state = await self.get_conclusion_state(
                subject_type=subject_type, subject_id=subject_id
            )
            if state is None:
                raise
            return state

    async def ensure_coverage_map(
        self,
        *,
        subject_type: str,
        subject_id: UUID,
        create: Callable[[], CoverageMap],
    ) -> CoverageMap:
        coverage = await self.get_coverage_map(
            subject_type=subject_type, subject_id=subject_id
        )
        if coverage is not None:
            return coverage
        candidate = create()
        try:
            async with self.session.begin_nested():
                self.session.add(candidate)
                await self.session.flush()
            return candidate
        except IntegrityError:
            coverage = await self.get_coverage_map(
                subject_type=subject_type, subject_id=subject_id
            )
            if coverage is None:
                raise
            return coverage
