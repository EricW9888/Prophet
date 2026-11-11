"""Dated holdings values reconstructed from the ledger and current price vintage."""

import asyncio
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from investos.models.entity import Security
from investos.models.evidence import RawEvidence
from investos.models.portfolio import Position
from investos.models.quant import AttributionResult
from investos.models.watcher import ActiveWatcher
from investos.schemas.portfolio_history import (
    PortfolioHistoryEvent,
    PortfolioHistoryPoint,
    PortfolioHistoryResponse,
)
from investos.services.broker_equity import BrokerEquityService, LedgerReplay
from investos.services.market_data import MarketDataService
from investos.services.portfolio_risk import digest
from investos.services.risk import RiskService
from investos.services.runtime_settings import RuntimeSettingsStore

EVENT_LIMIT = 250
# Provider exchange codes are translated to the calendar library's exchange
# names. Hours, holidays and early closes come from the calendar itself.
YAHOO_CALENDARS = {
    "NYQ": "XNYS",
    "NMS": "XNAS",
    "NGM": "XNAS",
    "NCM": "XNAS",
    "NAS": "XNAS",
    "PCX": "ARCX",
    "ASE": "XASE",
    "BTS": "BATS",
    "PNK": "OOTC",
    "OQB": "OOTC",
    "OQX": "OOTC",
}


class PortfolioHistoryService:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def history(
        self, window_days: int = 31, *, refresh: bool = False
    ) -> PortfolioHistoryResponse:
        now = datetime.now(UTC)
        start = now - timedelta(days=window_days)
        rows = await BrokerEquityService(self.session).ledger(now)
        full_replay = LedgerReplay(rows)
        full_replay.advance(now)
        current = defaultdict(Decimal)
        for position in (
            await self.session.scalars(
                select(Position).where(Position.list_type.in_(("holding", "closed")))
            )
        ).all():
            current[position.security_id] += position.quantity
        securities = {
            security.id: security
            for security in (
                await self.session.scalars(
                    select(Security).where(
                        Security.id.in_(
                            {row.security_id for row in rows if row.security_id}
                        )
                    )
                )
            ).all()
        }
        ledger_hash = digest(
            {
                "ledger": full_replay.proof.hexdigest(),
                "current": sorted(
                    (str(key), str(value)) for key, value in current.items()
                ),
                "securities": sorted(
                    (
                        str(key),
                        security.ticker,
                        security.instrument_type,
                        str(security.multiplier),
                    )
                    for key, security in securities.items()
                ),
            }
        )
        if not refresh:
            cached = await self.session.scalar(
                select(AttributionResult)
                .where(
                    AttributionResult.factor_contributions_json["kind"].astext
                    == "portfolio_history",
                    AttributionResult.factor_contributions_json["version"].as_integer()
                    == 1,
                    AttributionResult.factor_contributions_json[
                        "window_days"
                    ].as_integer()
                    == window_days,
                    AttributionResult.factor_contributions_json["ledger_hash"].astext
                    == ledger_hash,
                )
                .order_by(
                    AttributionResult.computed_at.desc(), AttributionResult.id.desc()
                )
                .limit(1)
            )
            if cached:
                return PortfolioHistoryResponse.model_validate(
                    cached.factor_contributions_json["response"]
                ).model_copy(update={"cache_hit": True})
        runtime = RuntimeSettingsStore.load().market_data
        snapshots = {}
        if runtime.enabled and runtime.provider == "yahoo_finance":
            try:
                async with asyncio.timeout(60):
                    snapshots = await MarketDataService(
                        self.session
                    ).fetch_signal_snapshots(
                        [security.ticker for security in securities.values()],
                        range_value="1y" if window_days > 180 else "6mo",
                    )
            except TimeoutError:
                snapshots = {}
        points, reasons, calendar_version = await asyncio.to_thread(
            self._build_values,
            snapshots,
            securities,
            rows,
            current,
            full_replay.quantities,
            start,
            now,
        )
        events = [
            PortfolioHistoryEvent(
                id=f"{row.receipt_type}-{row.id}",
                at=row.at,
                kind="ledger",
                label=" ".join(
                    str(part)
                    for part in (
                        row.action.replace("_", " ").title(),
                        row.quantity if row.security_id else None,
                        row.ticker,
                    )
                    if part is not None
                ),
                detail=(
                    f"Cash change {row.cash:+,.2f} USD"
                    if row.cash is not None
                    else None
                ),
                href=f"/history/receipt/{row.receipt_type}/{row.id}",
            )
            for row in rows
            if start <= row.at <= now
        ][-EVENT_LIMIT:]
        ledger_truncated = sum(start <= row.at <= now for row in rows) > EVENT_LIMIT
        evidence = (
            await self.session.scalars(
                select(RawEvidence)
                .where(
                    RawEvidence.ingest_time >= start,
                    RawEvidence.ingest_time <= now,
                    RawEvidence.source_item_type.not_in(
                        (
                            "conversation_turn",
                            "email_order_confirmation",
                            "source_claim_followup",
                            "market_setup_outcome_followup",
                        )
                    ),
                )
                .order_by(RawEvidence.ingest_time.desc(), RawEvidence.id.desc())
                .limit(EVENT_LIMIT + 1)
            )
        ).all()
        events.extend(
            PortfolioHistoryEvent(
                id=f"evidence-{item.id}",
                at=item.ingest_time,
                kind="research",
                label=item.title or "Evidence received",
                detail="Recorded in Prophet at this time.",
                href=f"/sources/evidence/{item.id}",
            )
            for item in evidence[:EVENT_LIMIT]
        )
        watches = (
            await self.session.scalars(
                select(ActiveWatcher)
                .where(
                    ActiveWatcher.triggered_at >= start,
                    ActiveWatcher.triggered_at <= now,
                )
                .order_by(ActiveWatcher.triggered_at.desc(), ActiveWatcher.id.desc())
                .limit(EVENT_LIMIT + 1)
            )
        ).all()
        events.extend(
            PortfolioHistoryEvent(
                id=f"watch-{watch.id}",
                at=watch.triggered_at,
                kind="alert",
                label=watch.objective,
                detail=watch.trigger_detail,
                href=f"/api_proxy/watcher/{watch.id}",
            )
            for watch in watches[:EVENT_LIMIT]
        )
        events.sort(key=lambda event: (event.at, event.id))
        response = PortfolioHistoryResponse(
            receipt_id=uuid4(),
            computed_at=datetime.now(UTC),
            window_days=window_days,
            points=points,
            events=events,
            events_truncated=ledger_truncated
            or len(evidence) > EVENT_LIMIT
            or len(watches) > EVENT_LIMIT,
            reasons=reasons,
        )
        self.session.add(
            AttributionResult(
                id=response.receipt_id,
                portfolio_wide=True,
                period_start=start,
                period_end=response.computed_at,
                total_return=(
                    next(
                        (
                            point.return_pct
                            for point in reversed(points)
                            if point.return_pct is not None
                        ),
                        0,
                    )
                    or 0
                )
                / 100,
                factor_return=0,
                idiosyncratic_return=0,
                computed_at=response.computed_at,
                factor_contributions_json={
                    "kind": "portfolio_history",
                    "version": 1,
                    "window_days": window_days,
                    "ledger_hash": ledger_hash,
                    "ledger": [
                        {
                            key: None if value is None else str(value)
                            for key, value in row._mapping.items()
                        }
                        for row in rows
                    ],
                    "calendar_version": calendar_version,
                    "response": response.model_dump(mode="json"),
                    "prices": snapshots,
                },
            )
        )
        await self.session.commit()
        return response

    @staticmethod
    def _build_values(
        snapshots, securities, rows, current, reconciled_quantities, start, now
    ):
        prices = defaultdict(dict)
        valuation_times = {}
        import exchange_calendars

        for ticker, snapshot in snapshots.items():
            if (
                snapshot.get("reported_symbol") != ticker
                or snapshot.get("currency") != "USD"
                or snapshot.get("status") == "unavailable"
            ):
                continue
            calendar_name = YAHOO_CALENDARS.get(snapshot.get("reported_exchange"))
            if calendar_name is None:
                continue
            calendar = exchange_calendars.get_calendar(calendar_name)
            if str(calendar.tz) != snapshot.get("exchange_timezone"):
                continue
            for session_day in calendar.sessions_in_range(
                start.date().isoformat(), now.date().isoformat()
            ):
                close = calendar.session_close(session_day).to_pydatetime()
                if start <= close <= now:
                    day = session_day.date()
                    valuation_times[day] = max(valuation_times.get(day, close), close)
            for bar in snapshot.get("daily_observations", []):
                day = datetime.fromisoformat(bar["timestamp"]).date()
                if not calendar.is_session(day.isoformat()):
                    continue
                completed = (
                    datetime.fromisoformat(bar["completed_at"])
                    if bar.get("completion_observed_at") is not None
                    else calendar.session_close(day.isoformat()).to_pydatetime()
                )
                if start <= completed <= now:
                    prices[day][ticker] = (completed, Decimal(str(bar["close"])))
                    if day not in valuation_times:
                        valuation_times[day] = completed

        # Keep current positions as an independent check on ledger completeness.
        reasons = []
        if any(
            current[key] != reconciled_quantities[key]
            for key in current.keys() | reconciled_quantities.keys()
        ):
            reasons.append(
                "The recorded trades do not reconstruct the current holdings."
            )
        if not prices:
            reasons.append("Dated market prices are unavailable.")
        latest_split = {
            row.security_id: row.at
            for row in rows
            if row.action == "split" and row.security_id
        }
        ticker_counts = defaultdict(int)
        for security in securities.values():
            ticker_counts[security.ticker] += 1
        replay = LedgerReplay(rows)
        points = []
        first_trade = next((row.at for row in rows if row.action == "buy"), now)
        for day, at in sorted(valuation_times.items()):
            marks = prices.get(day, {})
            if at < first_trade:
                continue
            replay.advance(at)
            held = {key: value for key, value in replay.quantities.items() if value}
            missing = []
            value = Decimal(0)
            for key, quantity in held.items():
                security = securities.get(key)
                ticker = (
                    security.ticker if security else replay.tickers.get(key, str(key))
                )
                mark = marks.get(ticker)
                supported = (
                    security is not None
                    and quantity > 0
                    and security.instrument_type in {"common_stock", "adr", "etf"}
                    and security.multiplier in {None, 1}
                    and ticker_counts[ticker] == 1
                    and not (key in latest_split and at < latest_split[key])
                )
                if not supported or mark is None:
                    missing.append(ticker)
                else:
                    value += quantity * mark[1]
            point_reasons = list(reasons)
            if replay.unsupported:
                point_reasons.append(
                    "An unsupported or incomplete transaction affects these holdings."
                )
            if missing:
                point_reasons.append(
                    "Some holdings lack matching prices or a verified historical share basis."
                )
            points.append(
                PortfolioHistoryPoint(
                    at=at,
                    value=None if point_reasons else float(value),
                    covered_positions=len(held) - len(missing),
                    total_positions=len(held),
                    unavailable_tickers=sorted(missing),
                    reasons=point_reasons,
                )
            )
        PortfolioHistoryService._performance(points, rows)
        return points, reasons, exchange_calendars.__version__

    @staticmethod
    def _performance(points, rows):
        baseline = next((point for point in points if point.value is not None), None)
        if baseline is None:
            return
        for point in points:
            if point.value is None or point.at < baseline.at:
                continue
            flows = [
                SimpleNamespace(
                    action=row.action,
                    quantity=row.quantity,
                    price=row.cash if row.receipt_type == "cash" else row.price,
                    executed_at=row.at,
                )
                for row in rows
                if baseline.at < row.at <= point.at
                and (row.receipt_type == "transaction" or row.action == "dividend")
            ]
            values = RiskService._modified_dietz_values(
                start_quantity=1,
                end_quantity=1,
                start_price=baseline.value,
                end_price=point.value,
                transactions=flows,
                period_start=baseline.at,
                period_end=point.at,
            )
            point.gain = values["gain"]
            point.return_pct = values["return_pct"]
