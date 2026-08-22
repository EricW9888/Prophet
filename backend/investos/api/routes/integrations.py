import asyncio

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from investos.db import async_session_maker, get_session
from investos.schemas.integrations import (
    IntegrationSettingsResponse,
    IntegrationSettingsUpdate,
    PlaidPublicTokenExchangeRequest,
    ResearchUsageSnapshot,
)
from investos.services.brokerage import BrokerageService, PlaidServiceError
from investos.services.research import ResearchService
from investos.services.runtime_settings import RuntimeSettingsStore

router = APIRouter(prefix="/integrations", tags=["integrations"])


@router.get("/settings", response_model=IntegrationSettingsResponse)
async def get_integration_settings(probe: bool = False):
    return await RuntimeSettingsStore.get_public_settings(probe=probe)


@router.put("/settings", response_model=IntegrationSettingsResponse)
async def update_integration_settings(
    payload: IntegrationSettingsUpdate,
    request: Request,
):
    try:
        updated = await RuntimeSettingsStore.update(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    coordinator = getattr(request.app.state, "automation", None)
    if coordinator:
        coordinator.sync_runtime_jobs()
    return updated


@router.get("/research/usage", response_model=ResearchUsageSnapshot)
async def get_research_usage():
    return await ResearchService.current_usage_snapshot()


@router.post("/plaid/link-token")
async def create_plaid_link_token():
    try:
        token = await asyncio.to_thread(BrokerageService.create_link_token)
        return {"link_token": token}
    except PlaidServiceError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/plaid/exchange")
async def exchange_plaid_public_token(payload: PlaidPublicTokenExchangeRequest):
    try:
        return await asyncio.to_thread(
            BrokerageService.exchange_public_token,
            payload.public_token,
        )
    except PlaidServiceError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/plaid/reconcile")
async def reconcile_plaid_holdings(session: AsyncSession = Depends(get_session)):
    try:
        snapshot = await asyncio.to_thread(BrokerageService.fetch_holdings_snapshot)
        result = await PortfolioService(session).reconcile_positions(
            snapshot["holdings"],
            broker_cash=snapshot.get("cash"),
            create_review_items=True,
        )
        return {**result, "snapshot": snapshot}
    except PlaidServiceError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


from investos.schemas.integrations import (
    GmailIntegrationTestRequest,
    GmailIntegrationTestResponse,
)
from investos.services.mailbox import GmailMailboxService
from investos.services.portfolio import PortfolioService


@router.post("/gmail/test", response_model=GmailIntegrationTestResponse)
async def test_gmail_connection(
    payload: GmailIntegrationTestRequest,
    session: AsyncSession = Depends(get_session),
):
    try:
        service = GmailMailboxService(session)
        return await service.test_connection(payload)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/gmail/sync")
async def sync_gmail(session: AsyncSession = Depends(get_session)):
    try:
        service = GmailMailboxService(session)
        result = await service.sync_recent_messages()
        return result
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


async def run_gmail_backfill_background():
    """
    Standalone background task that manages its own session.
    """
    async with async_session_maker() as session:
        try:
            service = GmailMailboxService(session)
            result = await service.backfill_scoped_label()

            import logging

            logger = logging.getLogger(__name__)
            logger.info(
                f"Gmail backfill scan complete: {result['processed_messages']} processed, {result['transactions_created']} created."
            )

            # Rebuild portfolio to integrate historical trades found in Gmail
            # which may sit chronologically between older CSV imports and now.
            portfolio = PortfolioService(session)
            await portfolio.recalculate_all_positions()
            logger.info("Portfolio rebuild complete after Gmail backfill.")
        except Exception:
            import logging

            logging.getLogger(__name__).exception(
                "Gmail backfill background task failed"
            )


@router.post("/gmail/backfill")
async def backfill_gmail(
    background_tasks: BackgroundTasks,
):
    try:
        background_tasks.add_task(run_gmail_backfill_background)
        return {
            "ok": True,
            "status": "started",
            "detail": "Deep backfill scan initiated. Your portfolio will rebuild automatically as transactions are discovered in the background.",
        }
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/gmail/backfill/logs")
async def stream_backfill_logs():
    """
    SSE endpoint to stream the backfill status log file to the frontend.
    """
    import asyncio

    from fastapi.responses import StreamingResponse

    from investos.services.mailbox import REPO_ROOT

    log_path = REPO_ROOT / "data" / "backfill_status.log"

    async def log_generator():
        if not log_path.exists():
            yield "data: [SYSTEM] Log file not found. Backfill may not have started yet.\n\n"
            return

        with open(log_path, "r") as f:
            # Start from the end of the file or recent history
            f.seek(0, 2)
            while True:
                line = f.readline()
                if not line:
                    await asyncio.sleep(0.5)
                    continue
                yield f"data: {line.strip()}\n\n"

    return StreamingResponse(log_generator(), media_type="text/event-stream")
