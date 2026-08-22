import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from investos.db import get_session
from investos.services.pruning import PruningService

router = APIRouter()


class RestoreKnowledgeRequest(BaseModel):
    reason: str | None = None


@router.post("/restore/{node_type}/{node_id}")
async def restore_knowledge_node(
    node_type: str,
    node_id: uuid.UUID,
    payload: RestoreKnowledgeRequest | None = None,
    session: AsyncSession = Depends(get_session),
):
    """
    Restore a softly deprecated fact, claim, or event and write an audit event.
    """
    try:
        service = PruningService(session)
        result = await service.restore_knowledge_node(
            node_type,
            node_id,
            reason=None if payload is None else payload.reason,
            actor="user",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    if result.get("reason") == "not_found":
        raise HTTPException(status_code=404, detail="Knowledge node not found")
    return result


@router.post("/{subject_type}/{subject_id}")
async def prune_subject_knowledge(
    subject_type: str,
    subject_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
):
    """
    Manually trigger a pruning and contradiction resolution pass for a specific subject.
    """
    if subject_type not in ["entity", "theme"]:
        raise HTTPException(
            status_code=400, detail="subject_type must be entity or theme"
        )
    try:
        service = PruningService(session)
        result = await service.prune_stale_knowledge(subject_id, subject_type)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
