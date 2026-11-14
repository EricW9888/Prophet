from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession

from investos.db import get_session
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
    ChatAttachmentResponse,
)
from investos.services.agent import AgentService
from investos.services.agent_action_log import AgentActionLogService
from investos.services.agent_turn_jobs import start_agent_turn_job
from investos.services.chat_attachments import ChatAttachmentService
from investos.services.chat_turn_state import ChatTurnState
from investos.services.live_jobs import LiveJobTracker

router = APIRouter(prefix="/agent", tags=["agent"])


@router.get("/attachments/capabilities")
async def attachment_capabilities():
    return ChatAttachmentService.capabilities()


@router.post("/attachments", response_model=ChatAttachmentResponse)
async def upload_chat_attachment(
    file: UploadFile = File(...), session: AsyncSession = Depends(get_session)
):
    service = ChatAttachmentService(session)
    try:
        return service.response(await service.upload(file))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/attachments/{attachment_id}/content")
async def chat_attachment_content(
    attachment_id: UUID, session: AsyncSession = Depends(get_session)
):
    service = ChatAttachmentService(session)
    try:
        row = await service.get(attachment_id)
        path = await service.storage.get_object_path(row.storage_path)
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(404, "Attachment not found.") from exc
    return FileResponse(
        path,
        media_type=row.content_type,
        filename=row.name,
        content_disposition_type=(
            "inline"
            if row.content_type.startswith("image/")
            or row.content_type == "application/pdf"
            else "attachment"
        ),
        headers={
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-store",
            "Content-Security-Policy": "sandbox; default-src 'none'",
        },
    )


def _job_response(tracker: LiveJobTracker, job_id: UUID) -> AgentTurnJobResponse:
    record = tracker.get(job_id)
    if record is None or record.job_kind != "agent_turn":
        raise HTTPException(status_code=404, detail="job_not_found")
    result = AgentTurnResponse.model_validate(record.result) if record.result else None
    return AgentTurnJobResponse(
        job_id=record.id,
        status=record.status,
        request_message=record.request_message,
        request_subject_id=record.request_context.get("subject_id"),
        request_subject_type=record.request_context.get("subject_type"),
        request_attachments=record.request_context.get("attachments", []),
        use_session_attachments=record.request_context.get(
            "use_session_attachments", True
        ),
        session_id=record.session_id,
        created_at=record.created_at,
        updated_at=record.updated_at,
        retry_at=record.retry_at,
        resumable=bool(record.request_context.get("resume_payload"))
        and ChatTurnState.can_resume(record.request_context),
        events=[
            {
                "phase": event.phase,
                "message": event.message,
                "created_at": event.created_at,
                "detail": event.detail,
            }
            for event in record.events
            if event.phase != "thinking"
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
    session: AsyncSession = Depends(get_session),
):
    tracker: LiveJobTracker = request.app.state.live_jobs
    attachments = ChatAttachmentService(session)
    original = payload
    payload = await AgentService(session).resolve_file_context(payload)
    try:
        files = (
            await attachments.resolve(payload.attachment_ids)
            if payload.attachment_ids
            else []
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    has_files = bool(payload.attachments or payload.attachment_ids)
    payload = payload.model_copy(
        update={
            "session_id": payload.session_id or uuid4(),
            "use_session_attachments": False,
            **({"auto_execute": False} if has_files else {}),
        }
    )
    record = tracker.create_job(
        request_message=payload.message,
        session_id=payload.session_id,
        request_context={
            "subject_id": str(payload.subject_id) if payload.subject_id else None,
            "subject_type": payload.subject_type,
            "attachments": [
                attachments.response(row).model_dump(mode="json") for row in files
            ],
            "use_session_attachments": original.use_session_attachments,
            "resume_payload": payload.model_dump(mode="json"),
            "turn_state": ChatTurnState.initial(),
        },
    )
    start_agent_turn_job(tracker, record, payload)
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
