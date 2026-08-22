import logging
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from investos.core.url_security import UnsafeUrlError, UrlFetchNetworkError
from investos.db import get_session
from investos.schemas.evidence import (
    RawEvidenceCreate,
    RawEvidenceResponse,
    UrlEvidenceCreate,
)
from investos.services.ingestion import IngestionService

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/ingestion", tags=["ingestion"])


def _unexpected_ingestion_error(exc: Exception) -> HTTPException:
    logger.exception("Evidence ingestion failed")
    return HTTPException(status_code=500, detail="Evidence ingestion failed.")


@router.post("/upload", response_model=RawEvidenceResponse)
async def upload_evidence(
    file: UploadFile = File(...),
    title: Optional[str] = Form(None),
    source_id: Optional[UUID] = Form(None),
    source_item_type: str = Form("manual_upload"),
    url: Optional[str] = Form(None),
    author: Optional[str] = Form(None),
    session: AsyncSession = Depends(get_session),
):
    svc = IngestionService(session)
    try:
        evidence = await svc.ingest_file(
            file=file,
            title=title,
            source_id=source_id,
            source_item_type=source_item_type,
            url=url,
            author=author,
        )
        return evidence
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise _unexpected_ingestion_error(exc) from exc


@router.post("/notes", response_model=RawEvidenceResponse)
async def upload_note(
    payload: RawEvidenceCreate,
    session: AsyncSession = Depends(get_session),
):
    svc = IngestionService(session)
    try:
        return await svc.ingest_text(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise _unexpected_ingestion_error(exc) from exc


@router.post("/url", response_model=RawEvidenceResponse)
async def ingest_url(
    payload: UrlEvidenceCreate,
    session: AsyncSession = Depends(get_session),
):
    svc = IngestionService(session)
    try:
        return await svc.ingest_url(
            url=payload.url,
            title=payload.title,
            source_id=payload.source_id,
            source_item_type=payload.source_item_type,
            author=payload.author,
            process_now=payload.process_now,
        )
    except UnsafeUrlError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except UrlFetchNetworkError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise _unexpected_ingestion_error(exc) from exc
