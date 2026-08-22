from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from investos.db import get_session
from investos.schemas.source import (
    FundamentalMetricCreate,
    FundamentalMetricResponse,
    InvestmentObjectBackfillCreate,
    InvestmentObjectBackfillResponse,
    MarketSetupOutcomeAssessmentCreate,
    MarketSetupOutcomeAssessmentResponse,
    MarketSetupSignalBackfillCreate,
    MarketSetupSignalBackfillResponse,
    MarketSetupSignalCreate,
    MarketSetupSignalResponse,
    MediaIngestionCapabilityResponse,
    OwnershipDisclosureCreate,
    SourceClaimAssessmentCreate,
    SourceClaimAssessmentResponse,
    SourceClaimAutoAssessmentCreate,
    SourceClaimAutoAssessmentResponse,
    SourceClaimBatchAssessmentCreate,
    SourceClaimBatchAssessmentResponse,
    SourceCreate,
    SourceEvidenceDetail,
    SourceEvidenceSummary,
    SourceFeedbackCreate,
    SourceFeedbackResponse,
    SourceResponse,
    SourceUpdate,
)
from investos.services.fundamentals import FundamentalMetricService
from investos.services.investment_object_backfill import InvestmentObjectBackfillService
from investos.services.market_setup import MarketSetupSignalService
from investos.services.ownership_signals import OwnershipSignalService
from investos.services.source import SourceService
from investos.services.youtube import YouTubeService

router = APIRouter(prefix="/sources", tags=["sources"])


@router.get("/", response_model=list[SourceResponse])
async def list_sources(session: AsyncSession = Depends(get_session)):
    return await SourceService(session).list_sources()


@router.post("/", response_model=SourceResponse)
async def create_source(
    payload: SourceCreate,
    session: AsyncSession = Depends(get_session),
):
    return await SourceService(session).create_source(payload)


@router.get("/recent-evidence", response_model=list[SourceEvidenceSummary])
async def list_recent_source_evidence(
    limit: int = 80,
    session: AsyncSession = Depends(get_session),
):
    return await SourceService(session).list_recent_evidence(limit=limit)


@router.get("/notes", response_model=list[SourceEvidenceSummary])
async def list_source_notes(
    limit: int = 80,
    session: AsyncSession = Depends(get_session),
):
    return await SourceService(session).list_notes(limit=limit)


@router.get("/youtube/capabilities", response_model=MediaIngestionCapabilityResponse)
async def get_youtube_capabilities():
    return YouTubeService.media_capabilities()


@router.get("/evidence/{evidence_id}", response_model=SourceEvidenceDetail)
async def get_source_evidence_detail(
    evidence_id: UUID,
    session: AsyncSession = Depends(get_session),
):
    detail = await SourceService(session).get_evidence_detail(evidence_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Evidence not found")
    return detail


@router.get("/feedback", response_model=list[SourceFeedbackResponse])
async def list_source_feedback(
    limit: int = 80,
    session: AsyncSession = Depends(get_session),
):
    return await SourceService(session).list_feedback(limit=limit)


@router.post("/feedback", response_model=SourceFeedbackResponse)
async def flag_source_evidence(
    payload: SourceFeedbackCreate,
    session: AsyncSession = Depends(get_session),
):
    try:
        result = await SourceService(session).update_evidence_feedback(
            evidence_id=payload.evidence_id,
            rating=payload.rating,
            note=payload.note,
            context=payload.context,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if result is None:
        raise HTTPException(status_code=404, detail="Evidence not found")
    return result


@router.delete("/feedback/{evidence_id}")
async def clear_source_feedback(
    evidence_id: UUID,
    session: AsyncSession = Depends(get_session),
):
    success = await SourceService(session).clear_evidence_feedback(evidence_id)
    if not success:
        raise HTTPException(status_code=404, detail="Evidence not found")
    return {"ok": True}


@router.post(
    "/claim-records/{claim_record_id}/assessment",
    response_model=SourceClaimAssessmentResponse,
)
async def assess_source_claim_record(
    claim_record_id: UUID,
    payload: SourceClaimAssessmentCreate,
    session: AsyncSession = Depends(get_session),
):
    try:
        result = await SourceService(session).update_claim_assessment(
            claim_record_id=claim_record_id,
            assessment=payload.assessment,
            notes=payload.notes,
            assessment_evidence=payload.assessment_evidence,
            horizon_days=payload.horizon_days,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if result is None:
        raise HTTPException(status_code=404, detail="Source claim record not found")
    return result


@router.post(
    "/claim-records/{claim_record_id}/auto-assessment",
    response_model=SourceClaimAutoAssessmentResponse,
)
async def auto_assess_source_claim_record(
    claim_record_id: UUID,
    payload: SourceClaimAutoAssessmentCreate,
    session: AsyncSession = Depends(get_session),
):
    result = await SourceService(session).propose_claim_assessment(
        claim_record_id=claim_record_id,
        apply=payload.apply,
        min_confidence=payload.min_confidence,
    )
    if result is None:
        raise HTTPException(status_code=404, detail="Source claim record not found")
    return result


@router.post(
    "/claim-records/auto-assess-due", response_model=SourceClaimBatchAssessmentResponse
)
async def auto_assess_due_source_claims(
    payload: SourceClaimBatchAssessmentCreate,
    session: AsyncSession = Depends(get_session),
):
    return await SourceService(session).assess_due_source_claims(
        limit=payload.limit,
        scan_limit=payload.scan_limit,
        apply=payload.apply,
        min_confidence=payload.min_confidence,
        retry_hours=payload.retry_hours,
        retry_share=payload.retry_share,
        research_missing_evidence=payload.research_missing_evidence,
        research_limit=payload.research_limit,
    )


@router.post("/ownership-disclosures", response_model=SourceEvidenceDetail)
async def ingest_ownership_disclosure(
    payload: OwnershipDisclosureCreate,
    session: AsyncSession = Depends(get_session),
):
    try:
        evidence = await OwnershipSignalService(session).ingest_disclosure(
            source_name=payload.source_name,
            source_type=payload.source_type,
            source_url=payload.source_url,
            source_description=payload.source_description,
            source_item_type=payload.source_item_type,
            title=payload.title,
            url=payload.url,
            external_id=payload.external_id,
            author=payload.author,
            metadata=payload.metadata,
            summary=payload.summary,
            event_time=payload.event_time,
            public_time=payload.public_time,
            eligible_action_time=payload.eligible_action_time,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    detail = await SourceService(session).get_evidence_detail(evidence.id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Evidence not found")
    return detail


@router.post("/market-setup-signals", response_model=MarketSetupSignalResponse)
async def create_market_setup_signal(
    payload: MarketSetupSignalCreate,
    session: AsyncSession = Depends(get_session),
):
    try:
        return await MarketSetupSignalService(session).create_signal(
            **payload.model_dump()
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post(
    "/market-setup-signals/backfill", response_model=MarketSetupSignalBackfillResponse
)
async def backfill_market_setup_signals(
    payload: MarketSetupSignalBackfillCreate,
    session: AsyncSession = Depends(get_session),
):
    return await MarketSetupSignalService(session).backfill_from_existing_evidence(
        apply=payload.apply,
        limit=payload.limit,
        min_confidence=payload.min_confidence,
        include_conversation_turns=payload.include_conversation_turns,
    )


@router.post(
    "/market-setup-signals/assess-due",
    response_model=MarketSetupOutcomeAssessmentResponse,
)
async def assess_due_market_setup_signals(
    payload: MarketSetupOutcomeAssessmentCreate,
    session: AsyncSession = Depends(get_session),
):
    return await MarketSetupSignalService(session).assess_due_signals(
        **payload.model_dump()
    )


@router.post(
    "/investment-objects/backfill",
    response_model=InvestmentObjectBackfillResponse,
)
async def backfill_investment_objects(
    payload: InvestmentObjectBackfillCreate,
    session: AsyncSession = Depends(get_session),
):
    return await InvestmentObjectBackfillService(session).run(**payload.model_dump())


@router.post("/fundamental-metrics", response_model=FundamentalMetricResponse)
async def create_fundamental_metric(
    payload: FundamentalMetricCreate,
    session: AsyncSession = Depends(get_session),
):
    try:
        return await FundamentalMetricService(session).create_metric(
            **payload.model_dump()
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.patch("/{source_id}", response_model=SourceResponse)
async def update_source(
    source_id: UUID,
    payload: SourceUpdate,
    session: AsyncSession = Depends(get_session),
):
    updated = await SourceService(session).update_source(source_id, payload)
    if not updated:
        raise HTTPException(status_code=404, detail="Source not found")
    return updated
