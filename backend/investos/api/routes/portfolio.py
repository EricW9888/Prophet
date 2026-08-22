import time
from uuid import UUID

from fastapi import APIRouter, Body, Depends, File, HTTPException, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from investos.config import settings
from investos.core.uploads import read_upload_limited
from investos.db import get_session
from investos.schemas.portfolio import (
    PortfolioImportResponse,
    PortfolioOverviewResponse,
    PortfolioSimpleImportRequest,
    PositionResponse,
    ReconcileRequest,
    ReconcileResponse,
    ReconcileTextRequest,
    ResearchObjectCreate,
    ResearchObjectResponse,
    TransactionCorrectionRequest,
    TransactionCorrectionResponse,
    TransactionCreate,
    TransactionCreateByTicker,
    TransactionResponse,
)
from investos.services.market_data import MarketDataService
from investos.services.portfolio import PortfolioService

router = APIRouter(prefix="/portfolio", tags=["portfolio"])


@router.get("/positions", response_model=list[PositionResponse])
async def list_positions(
    list_type: str = "holding", session: AsyncSession = Depends(get_session)
):
    svc = PortfolioService(session)
    return await svc.list_positions(list_type=list_type)


_last_overview_refresh: dict[str, float] = {"at": 0.0}
_OVERVIEW_REFRESH_THROTTLE_SECONDS = 30.0


@router.get("/overview", response_model=PortfolioOverviewResponse)
async def get_portfolio_overview(
    refresh: bool = True,
    session: AsyncSession = Depends(get_session),
):
    """Return the portfolio overview.

    By default this refreshes live market prices on load (throttled so repeated
    loads don't hammer the data provider) so the page reflects current quotes
    rather than the last background snapshot. Pass ``refresh=false`` to skip.
    """
    if refresh:
        now = time.monotonic()
        if now - _last_overview_refresh["at"] >= _OVERVIEW_REFRESH_THROTTLE_SECONDS:
            _last_overview_refresh["at"] = now
            try:
                await MarketDataService(session).refresh_live_prices()
            except Exception:
                # Never let a transient quote-provider error block the overview.
                pass
    svc = PortfolioService(session)
    return await svc.overview()


@router.post("/reconcile", response_model=ReconcileResponse)
async def reconcile_positions(
    payload: ReconcileRequest,
    session: AsyncSession = Depends(get_session),
):
    """Compare the reconstructed book to an authoritative broker snapshot.

    Discrepancies are returned and (by default) queued for human review,
    rather than silently overwriting the evidence-built book.
    """
    svc = PortfolioService(session)
    snapshot = [{"ticker": h.ticker, "quantity": h.quantity} for h in payload.holdings]
    return await svc.reconcile_positions(
        snapshot,
        broker_cash=payload.cash,
        create_review_items=payload.create_review_items,
    )


@router.post("/reconcile/text", response_model=ReconcileResponse)
async def reconcile_positions_from_text(
    payload: ReconcileTextRequest,
    session: AsyncSession = Depends(get_session),
):
    """Reconcile against a pasted/CSV holdings snapshot (e.g. a Robinhood
    statement export). The snapshot is parsed deterministically — no LLM."""
    svc = PortfolioService(session)
    parsed = svc.parse_holdings_snapshot(payload.text)
    if not parsed["holdings"] and parsed["cash"] is None:
        raise HTTPException(
            status_code=422,
            detail="Could not parse any holdings. Use lines like 'EXMPL 10' or a CSV with Symbol/Quantity columns.",
        )
    return await svc.reconcile_positions(
        parsed["holdings"],
        broker_cash=parsed["cash"],
        create_review_items=payload.create_review_items,
    )


@router.get("/positions/{position_id}", response_model=PositionResponse)
async def get_position(position_id: UUID, session: AsyncSession = Depends(get_session)):
    svc = PortfolioService(session)
    pos = await svc.get_position(position_id)
    if not pos:
        raise HTTPException(status_code=404, detail="Position not found")
    return pos


@router.post(
    "/positions/{position_id}/transactions", response_model=TransactionResponse
)
async def add_transaction(
    position_id: UUID,
    txn_data: TransactionCreate,
    session: AsyncSession = Depends(get_session),
):
    svc = PortfolioService(session)
    try:
        txn = await svc.add_transaction(position_id, txn_data)
        return txn
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/transactions/by-ticker", response_model=TransactionResponse)
async def add_transaction_by_ticker(
    txn_data: TransactionCreateByTicker,
    session: AsyncSession = Depends(get_session),
):
    svc = PortfolioService(session)
    try:
        txn = await svc.add_transaction_by_ticker(
            ticker=txn_data.ticker,
            txn_data=TransactionCreate(
                action=txn_data.action,
                quantity=txn_data.quantity,
                price=txn_data.price,
                executed_at=txn_data.executed_at,
                notes=txn_data.notes,
                lot_type=txn_data.lot_type,
                provenance_json=txn_data.provenance_json,
            ),
            list_type=txn_data.list_type,
            direction=txn_data.direction,
        )
        return txn
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post(
    "/transactions/{transaction_id}/correct", response_model=TransactionResponse
)
async def correct_transaction(
    transaction_id: UUID,
    payload: TransactionCorrectionRequest,
    session: AsyncSession = Depends(get_session),
):
    """Supersede a parsed/manual transaction with a corrected ledger row.

    The original row is retained as corrected evidence and excluded from replay;
    the replacement row becomes the active transaction.
    """
    svc = PortfolioService(session)
    try:
        return await svc.correct_transaction(transaction_id, payload)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get(
    "/transactions/corrections", response_model=list[TransactionCorrectionResponse]
)
async def list_transaction_corrections(
    limit: int = 50,
    session: AsyncSession = Depends(get_session),
):
    """Read-only ledger of manual transaction supersessions."""
    return await PortfolioService(session).transaction_corrections(limit=limit)


@router.post("/research-objects", response_model=ResearchObjectResponse)
async def create_research_object(
    payload: ResearchObjectCreate,
    session: AsyncSession = Depends(get_session),
):
    svc = PortfolioService(session)
    try:
        return await svc.create_research_object(payload)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/import/csv", response_model=PortfolioImportResponse)
async def import_transactions_csv(
    file: UploadFile = File(...),
    session: AsyncSession = Depends(get_session),
):
    try:
        content = (
            await read_upload_limited(
                file,
                max_bytes=settings.PORTFOLIO_IMPORT_MAX_MB * 1024 * 1024,
            )
        ).decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="CSV must be UTF-8 encoded text.")
    except ValueError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc

    svc = PortfolioService(session)
    try:
        return await svc.import_transactions_csv(content)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/import/text", response_model=PortfolioImportResponse)
async def import_transactions_text(
    payload: str = Body(..., embed=False),
    session: AsyncSession = Depends(get_session),
):
    max_characters = settings.PORTFOLIO_IMPORT_MAX_MB * 1024 * 1024
    if len(payload) > max_characters:
        raise HTTPException(
            status_code=413,
            detail="The portfolio import exceeds the configured size limit.",
        )
    svc = PortfolioService(session)
    try:
        return await svc.import_transactions_csv(payload)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/import/simple", response_model=PortfolioImportResponse)
async def import_simple_portfolio_text(
    payload: PortfolioSimpleImportRequest,
    session: AsyncSession = Depends(get_session),
):
    svc = PortfolioService(session)
    try:
        return await svc.import_simple_text(payload)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
