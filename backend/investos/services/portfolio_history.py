"""Dated holdings values reconstructed from the ledger and current price vintage."""

import asyncio
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4
from weakref import WeakValueDictionary

from sqlalchemy import case, cast, column, func, select, true
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession

from investos.core.dates import parse_iso_datetime
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
from investos.services.market_data import (
    YAHOO_CALENDARS,
    MarketDataService,
    reported_share_splits,
    share_basis_reconciliation,
)
from investos.services.portfolio_risk import digest
from investos.services.risk import RiskService
from investos.services.runtime_settings import RuntimeSettingsStore

EVENT_LIMIT = 250
HISTORY_RECEIPT_VERSION = 3


class PortfolioHistoryService:
    _revalidations = WeakValueDictionary()

    def __init__(self, session: AsyncSession):
        self.session = session

    async def history(
        self,
        window_days: int = 31,
        *,
        refresh: bool = False,
        revalidate: UUID | None = None,
    ) -> PortfolioHistoryResponse:
        if revalidate is not None:
            async with self._revalidations.setdefault(window_days, asyncio.Lock()):
                return await self._history(
                    window_days, refresh=False, revalidate=revalidate
                )
        return await self._history(window_days, refresh=refresh, revalidate=None)

    async def _history(self, window_days, *, refresh, revalidate):
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
        runtime = RuntimeSettingsStore.load().market_data
        market_data_enabled = runtime.enabled and runtime.provider == "yahoo_finance"
        if not refresh:
            receipt = AttributionResult.factor_contributions_json
            cached = (
                await self.session.execute(
                    select(
                        AttributionResult.id,
                        AttributionResult.computed_at,
                        receipt["response"].label("response"),
                        receipt["next_market_close"].astext.label("next_market_close"),
                    )
                    .where(
                        receipt["kind"].astext == "portfolio_history",
                        receipt["version"].as_integer() == HISTORY_RECEIPT_VERSION,
                        receipt["window_days"].as_integer() == window_days,
                        receipt["ledger_hash"].astext == ledger_hash,
                    )
                    .order_by(
                        AttributionResult.computed_at.desc(),
                        AttributionResult.id.desc(),
                    )
                    .limit(1)
                )
            ).first()
            if cached:
                response = PortfolioHistoryResponse.model_validate(cached.response)
                next_close = parse_iso_datetime(cached.next_market_close)
                if (
                    next_close is None
                    or not response.points
                    or response.points[-1].value is None
                ):
                    retry_at = cached.computed_at + timedelta(
                        seconds=max(1, runtime.refresh_interval_seconds)
                    )
                    next_close = min(next_close, retry_at) if next_close else retry_at
                due = market_data_enabled and next_close <= now
                if not due or revalidate != cached.id:
                    events, truncated = await self._events(rows, start, now)
                    return response.model_copy(
                        update={
                            "cache_hit": True,
                            "refresh_due": due,
                            "events": events,
                            "events_truncated": truncated,
                        }
                    )
        snapshots = {}
        if market_data_enabled:
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
        await self._load_retained_regular_closes(snapshots, start, now)
        points, reasons, calendar_version, next_close = await asyncio.to_thread(
            self._build_values,
            snapshots,
            securities,
            rows,
            current,
            full_replay.quantities,
            start,
            now,
        )
        events, truncated = await self._events(rows, start, now)
        response = PortfolioHistoryResponse(
            receipt_id=uuid4(),
            computed_at=datetime.now(UTC),
            window_days=window_days,
            points=points,
            events=events,
            events_truncated=truncated,
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
                    "version": HISTORY_RECEIPT_VERSION,
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
                    "next_market_close": next_close.isoformat() if next_close else None,
                    "response": response.model_dump(mode="json"),
                    "prices": snapshots,
                },
            )
        )
        await self.session.commit()
        return response

    async def _events(self, rows, start, now):
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
            await self.session.execute(
                select(RawEvidence.id, RawEvidence.ingest_time, RawEvidence.title)
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
            await self.session.execute(
                select(
                    ActiveWatcher.id,
                    ActiveWatcher.triggered_at,
                    ActiveWatcher.objective,
                    ActiveWatcher.trigger_detail,
                )
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
        truncated = (
            ledger_truncated
            or len(evidence) > EVENT_LIMIT
            or len(watches) > EVENT_LIMIT
        )
        return events, truncated

    async def _load_retained_regular_closes(self, snapshots, start, now):
        if not snapshots:
            return
        receipts = AttributionResult.factor_contributions_json
        prices = receipts["prices"]
        stored = func.jsonb_each(
            case((func.jsonb_typeof(prices) == "object", prices), else_=cast({}, JSONB))
        ).table_valued("key", column("value", JSONB))
        quote = stored.c.value["latest_regular_close"]
        identity_fields = (
            "provider",
            "reported_symbol",
            "reported_exchange",
            "exchange_timezone",
            "currency",
        )
        rows = await self.session.execute(
            select(
                AttributionResult.id,
                stored.c.key,
                quote,
                *(stored.c.value[key].astext for key in identity_fields),
            )
            .select_from(AttributionResult)
            .join(stored, true())
            .where(
                receipts["kind"].astext == "portfolio_history",
                AttributionResult.computed_at.between(start, now),
                stored.c.key.in_(list(snapshots)),
                func.jsonb_typeof(quote) == "object",
            )
            .order_by(AttributionResult.computed_at.desc(), AttributionResult.id.desc())
        )
        for receipt_id, ticker, close, *identity in rows:
            snapshot = snapshots[ticker]
            if identity[0] != "yahoo_finance" or tuple(identity) != tuple(
                snapshot.get(key) for key in identity_fields
            ):
                continue
            snapshot.setdefault("retained_regular_closes", []).append(
                {
                    **close,
                    "receipt_id": str(receipt_id),
                }
            )

    @staticmethod
    def _build_values(
        snapshots, securities, rows, current, reconciled_quantities, start, now
    ):
        prices = defaultdict(dict)
        retained_tickers = defaultdict(set)
        valuation_times = {}
        missing_prices_before_split = defaultdict(set)
        missing_shares_after_split = defaultdict(set)
        reported_splits = {}
        next_close = None
        security_ids = {security.ticker: key for key, security in securities.items()}
        import exchange_calendars

        for ticker, snapshot in snapshots.items():
            if (
                ticker not in security_ids
                or snapshot.get("reported_symbol") != ticker
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
            session = calendar.date_to_session(
                now.astimezone(calendar.tz).date(), direction="next"
            )
            close = calendar.session_close(session)
            if close <= now:
                close = calendar.session_close(calendar.next_session(session))
            close = close.to_pydatetime()
            next_close = min(next_close, close) if next_close else close
            provider_splits = reported_share_splits(snapshot)
            reported_splits[ticker] = provider_splits
            (
                _,
                missing_prices_before_split[ticker],
                missing_shares_after_split[ticker],
            ) = share_basis_reconciliation(
                rows, security_ids[ticker], calendar, provider_splits
            )
            for session_day in calendar.sessions_in_range(
                start.date().isoformat(), now.date().isoformat()
            ):
                close = calendar.session_close(session_day).to_pydatetime()
                if start <= close <= now:
                    day = session_day.date()
                    valuation_times[day] = max(valuation_times.get(day, close), close)
            bars = list(snapshot.get("daily_observations", []))
            closing_quote = snapshot.get("latest_regular_close")
            if closing_quote and not any(
                bar["timestamp"] == closing_quote["timestamp"] for bar in bars
            ):
                bars.append(closing_quote)
            for bar in bars:
                day = datetime.fromisoformat(bar["timestamp"]).date()
                if not calendar.is_session(day.isoformat()):
                    continue
                completed = (
                    datetime.fromisoformat(bar["completed_at"])
                    if bar.get("completion_observed_at") is not None
                    else calendar.session_close(day.isoformat()).to_pydatetime()
                )
                if start <= completed <= now:
                    prices[day][ticker] = (
                        Decimal(str(bar["close"])),
                        bar.get("close_basis") == "provider_regular_market_quote",
                    )
                    if day not in valuation_times:
                        valuation_times[day] = completed
            retained = snapshot.pop("retained_regular_closes", [])
            for quote in retained:
                if quote.get("close_basis") != "provider_regular_market_quote":
                    continue
                opens_at, closes_at, quoted_at, observed_at = (
                    parse_iso_datetime(quote.get(key))
                    for key in (
                        "timestamp",
                        "completed_at",
                        "quoted_at",
                        "completion_observed_at",
                    )
                )
                close = MarketDataService._finite_number(quote.get("close"))
                if not (
                    opens_at
                    and closes_at
                    and quoted_at
                    and observed_at
                    and close
                    and close > 0
                ):
                    continue
                day = opens_at.astimezone(calendar.tz).date()
                if (
                    ticker in prices.get(day, {})
                    or not start <= closes_at <= quoted_at <= observed_at <= now
                    or quoted_at.astimezone(calendar.tz).date() != day
                    or not calendar.is_session(day.isoformat())
                    or calendar.session_open(day.isoformat()) != opens_at
                    or calendar.session_close(day.isoformat()) != closes_at
                ):
                    continue
                prices[day][ticker] = (Decimal(str(close)), True)
                retained_tickers[day].add(ticker)
                snapshot.setdefault("retained_regular_closes", []).append(quote)

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
            disputed = []
            retained_used = []
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
                )
                if any(
                    day < split for split in missing_prices_before_split[ticker]
                ) or any(day >= split for split in missing_shares_after_split[ticker]):
                    missing.append(ticker)
                    disputed.append(ticker)
                elif not supported or mark is None:
                    missing.append(ticker)
                else:
                    price, regular_quote = mark
                    if ticker in retained_tickers.get(day, ()):
                        retained_used.append(ticker)
                    if not regular_quote:
                        for split, ratio in reported_splits.get(ticker, {}).items():
                            if day < split:
                                price *= ratio
                    value += quantity * price
            point_reasons = list(reasons)
            if replay.unsupported:
                point_reasons.append(
                    "An unsupported or incomplete transaction affects these holdings."
                )
            if missing:
                point_reasons.append(
                    "Some holdings lack matching prices or a verified historical share basis."
                )
            if disputed:
                point_reasons.append(
                    "Price and ledger split records need reconciliation for "
                    + ", ".join(sorted(disputed))
                    + "."
                )
            points.append(
                PortfolioHistoryPoint(
                    at=at,
                    value=None if point_reasons else float(value),
                    covered_positions=len(held) - len(missing),
                    total_positions=len(held),
                    unavailable_tickers=sorted(missing),
                    retained_close_tickers=sorted(retained_used),
                    reasons=point_reasons,
                )
            )
        PortfolioHistoryService._performance(points, rows)
        return points, reasons, exchange_calendars.__version__, next_close

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
