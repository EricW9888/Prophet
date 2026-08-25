import asyncio
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from investos.db import async_session_maker, get_session
from investos.schemas.agent import (
    AgentActionLogEntryResponse,
    AgentActionLogResponse,
    AgentConversationHistoryResponse,
    AgentConversationListResponse,
    AgentConversationSummaryResponse,
    AgentConversationUpdateRequest,
    AgentResolveResponse,
    AgentTurnJobListResponse,
    AgentTurnJobResponse,
    AgentTurnRequest,
    AgentTurnResponse,
)
from investos.services.agent import AgentService
from investos.services.agent_action_log import AgentActionLogService
from investos.services.live_jobs import LiveJobTracker

router = APIRouter(prefix="/agent", tags=["agent"])


def _job_response(tracker: LiveJobTracker, job_id: UUID) -> AgentTurnJobResponse:
    record = tracker.get(job_id)
    if record is None or record.job_kind != "agent_turn":
        raise HTTPException(status_code=404, detail="job_not_found")
    result = AgentTurnResponse.model_validate(record.result) if record.result else None
    return AgentTurnJobResponse(
        job_id=record.id,
        status=record.status,
        request_message=record.request_message,
        session_id=record.session_id,
        created_at=record.created_at,
        updated_at=record.updated_at,
        events=[
            {
                "phase": event.phase,
                "message": event.message,
                "created_at": event.created_at,
                "detail": event.detail,
            }
            for event in record.events
        ],
        result=result,
        error=record.error,
    )


@router.post("/turn", response_model=AgentTurnResponse)
async def run_agent_turn(
    payload: AgentTurnRequest,
    session: AsyncSession = Depends(get_session),
):
    try:
        return await AgentService(session).handle_turn(payload)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/turn-jobs", response_model=AgentTurnJobResponse)
async def create_agent_turn_job(
    payload: AgentTurnRequest,
    request: Request,
):
    tracker: LiveJobTracker = request.app.state.live_jobs
    record = tracker.create_job(
        request_message=payload.message,
        session_id=payload.session_id,
    )

    async def run_job() -> None:
        async with async_session_maker() as session:
            service = AgentService(session)

            async def progress(
                phase: str, message: str, detail: dict | None = None
            ) -> None:
                tracker.add_event(
                    record.id, phase=phase, message=message, detail=detail
                )

            try:
                result = await service.handle_turn(payload, progress_callback=progress)
                tracker.complete(record.id, result=result.model_dump(mode="json"))
            except asyncio.CancelledError:
                # Job was cancelled by user
                pass
            except Exception as exc:
                tracker.fail(record.id, error=str(exc))

    task = asyncio.create_task(run_job())
    tracker.mark_running(record.id, task=task, message="Prophet started this turn.")
    return _job_response(tracker, record.id)


@router.get("/turn-jobs", response_model=AgentTurnJobListResponse)
async def list_agent_turn_jobs(
    request: Request,
    status: str | None = None,
    limit: int = 30,
):
    tracker: LiveJobTracker = request.app.state.live_jobs
    jobs = [
        _job_response(tracker, record.id)
        for record in tracker.list_jobs(
            status=status, job_kind="agent_turn", limit=limit
        )
    ]
    return AgentTurnJobListResponse(jobs=jobs)


@router.get("/turn-jobs/{job_id}", response_model=AgentTurnJobResponse)
async def get_agent_turn_job(job_id: UUID, request: Request):
    tracker: LiveJobTracker = request.app.state.live_jobs
    return _job_response(tracker, job_id)


@router.post("/turn-jobs/{job_id}/cancel")
async def cancel_agent_turn_job(job_id: UUID, request: Request):
    tracker: LiveJobTracker = request.app.state.live_jobs
    success = tracker.cancel_job(job_id)
    if not success:
        raise HTTPException(status_code=404, detail="job_not_found_or_not_cancellable")
    return {"ok": True, "detail": "Job cancellation signal sent."}


@router.post("/resolve", response_model=AgentResolveResponse)
async def resolve_agent_context(
    payload: AgentTurnRequest,
    session: AsyncSession = Depends(get_session),
):
    try:
        return await AgentService(session).resolve_context(
            message=payload.message,
            subject_id=payload.subject_id,
            subject_type=payload.subject_type,
            session_id=payload.session_id,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/history", response_model=AgentConversationHistoryResponse)
async def get_agent_history(
    session_id: UUID | None = None,
    subject_id: UUID | None = None,
    subject_type: str = "entity",
    include_artifacts: bool = False,
    session: AsyncSession = Depends(get_session),
):
    try:
        return await AgentService(session).conversation_history(
            session_id=session_id,
            subject_id=subject_id,
            subject_type=subject_type,
            include_artifacts=include_artifacts,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/conversations", response_model=AgentConversationListResponse)
async def list_agent_conversations(session: AsyncSession = Depends(get_session)):
    try:
        return await AgentService(session).list_conversations()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.patch(
    "/conversations/{session_id}", response_model=AgentConversationSummaryResponse
)
async def update_agent_conversation(
    session_id: UUID,
    payload: AgentConversationUpdateRequest,
    session: AsyncSession = Depends(get_session),
):
    try:
        return await AgentService(session).update_conversation(
            session_id, title=payload.title
        )
    except ValueError as exc:
        if str(exc) == "conversation_not_found":
            raise HTTPException(status_code=404, detail=str(exc))
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/actions", response_model=AgentActionLogResponse)
async def list_agent_actions(limit: int = 40):
    items = AgentActionLogService.recent(limit=max(1, min(limit, 200)))
    return AgentActionLogResponse(
        actions=[AgentActionLogEntryResponse.model_validate(item) for item in items]
    )
