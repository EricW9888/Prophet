from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from investos.db import get_session
from investos.models.coverage import CoverageMap, QuestionHistory, UnresolvedQuestion
from investos.schemas.automation import AutomationJobStatus, AutomationStatusResponse
from investos.schemas.profile import UnresolvedQuestionResponse
from investos.services.question_lifecycle import (
    ACTIVE_QUESTION_STATUSES,
    QuestionLifecycleService,
)

router = APIRouter(prefix="/automation", tags=["automation"])


@router.get("/questions")
async def question_backlog(
    view: Literal["active", "history"] = "active",
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),
):
    lifecycle = QuestionLifecycleService(session)
    query = select(UnresolvedQuestion)
    if view == "active":
        query = query.join(CoverageMap).where(
            UnresolvedQuestion.status.in_(ACTIVE_QUESTION_STATUSES),
            lifecycle.portfolio_clause(),
        )
        query = query.order_by(
            UnresolvedQuestion.next_review_at.asc().nullsfirst(),
            UnresolvedQuestion.urgency.desc(),
            UnresolvedQuestion.id,
        )
    else:
        last_history = (
            select(func.max(QuestionHistory.created_at))
            .where(QuestionHistory.question_id == UnresolvedQuestion.id)
            .correlate(UnresolvedQuestion)
            .scalar_subquery()
        )
        query = query.where(
            UnresolvedQuestion.status.not_in(ACTIVE_QUESTION_STATUSES)
        ).order_by(
            func.coalesce(last_history, UnresolvedQuestion.created_at).desc(),
            UnresolvedQuestion.id,
        )
    total = await session.scalar(
        select(func.count()).select_from(query.order_by(None).subquery())
    )
    rows = (await session.scalars(query.offset(offset).limit(20))).all()
    return {
        **await lifecycle.snapshot(),
        "view": view,
        "offset": offset,
        "total": total,
        "items": [
            UnresolvedQuestionResponse.model_validate(row).model_dump(mode="json")
            for row in rows
        ],
    }


@router.get("/questions/{question_id}/history")
async def question_history(
    question_id: UUID,
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),
):
    if await session.get(UnresolvedQuestion, question_id) is None:
        raise HTTPException(404, "Question not found")
    rows = (
        await session.scalars(
            select(QuestionHistory)
            .where(QuestionHistory.question_id == question_id)
            .order_by(QuestionHistory.created_at.desc(), QuestionHistory.id)
            .offset(offset)
            .limit(100)
        )
    ).all()
    total = await session.scalar(
        select(func.count())
        .select_from(QuestionHistory)
        .where(QuestionHistory.question_id == question_id)
    )
    return {
        "total": total,
        "offset": offset,
        "items": [
            dict(
                id=str(row.id),
                event_type=row.event_type,
                detail=row.detail_json,
                created_at=row.created_at.isoformat(),
            )
            for row in rows
        ],
    }


class QuestionDecision(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    action: Literal["renew", "retire"]
    reason: str = Field(min_length=1, max_length=500)


@router.post(
    "/questions/{question_id}/decision", response_model=UnresolvedQuestionResponse
)
async def question_decision(
    question_id: UUID,
    payload: QuestionDecision,
    session: AsyncSession = Depends(get_session),
):
    question = await session.get(UnresolvedQuestion, question_id, with_for_update=True)
    if question is None:
        raise HTTPException(404, "Question not found")
    if question.superseded_by_id is not None:
        raise HTTPException(
            409, "Use the canonical question before changing its work budget"
        )
    lifecycle = QuestionLifecycleService(session)
    if payload.action == "renew" and not await session.scalar(
        select(CoverageMap.id).where(
            CoverageMap.id == question.coverage_map_id, lifecycle.portfolio_clause()
        )
    ):
        raise HTTPException(
            409,
            "Track this subject in the portfolio before renewing automated research",
        )
    lifecycle.record(
        question,
        "owner_decision",
        action=payload.action,
        reason=payload.reason,
        previous_research_attempts=question.research_attempt_count,
        previous_resolution_attempts=question.resolution_attempt_count,
    )
    if payload.action == "renew":
        question.research_attempt_count = question.resolution_attempt_count = 0
        lifecycle.transition(
            question,
            status="needs_research",
            outcome="owner_renewed",
            next_action="research",
        )
    else:
        lifecycle.transition(question, status="archived", outcome="owner_retired")
    await session.commit()
    return question


@router.get("/status", response_model=AutomationStatusResponse)
async def get_automation_status(request: Request):
    coordinator = getattr(request.app.state, "automation", None)
    jobs = []
    if coordinator:
        jobs = [
            AutomationJobStatus(
                name=item.name,
                enabled=item.enabled,
                interval_seconds=item.interval_seconds,
                last_run_at=item.last_run_at,
                last_status=item.last_status,
                detail=item.detail,
            )
            for item in coordinator.status()
        ]
    return AutomationStatusResponse(
        automation_enabled=bool(coordinator),
        jobs=jobs,
    )


@router.post("/run/{job_name}", response_model=AutomationStatusResponse)
async def run_automation_job(job_name: str, request: Request):
    coordinator = getattr(request.app.state, "automation", None)
    if not coordinator:
        raise HTTPException(
            status_code=503, detail="Automation coordinator is not running"
        )
    try:
        await coordinator.run_job(job_name)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    jobs = [
        AutomationJobStatus(
            name=item.name,
            enabled=item.enabled,
            interval_seconds=item.interval_seconds,
            last_run_at=item.last_run_at,
            last_status=item.last_status,
            detail=item.detail,
        )
        for item in coordinator.status()
    ]
    return AutomationStatusResponse(automation_enabled=True, jobs=jobs)
