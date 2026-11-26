from __future__ import annotations

import asyncio
import math
import statistics
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from investos.core.valuation import number
from investos.models.entity import Security
from investos.models.market_calendar import MarketSessionObservation
from investos.models.portfolio import Position
from investos.services.runtime_settings import RuntimeSettingsStore

# Provider exchange codes use the same calendar for quotes and holdings history.
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


def share_basis_reconciliation(rows, security_id, calendar, reported):
    """Match ratios on the exchange session to which each ledger action applies."""
    from collections import defaultdict

    from investos.services.broker_equity import LedgerReplay

    timezone = ZoneInfo(str(calendar.tz))
    booked = defaultdict(lambda: Decimal(1))
    for row in rows:
        if row.action != "split" or row.security_id != security_id:
            continue
        day = row.at.astimezone(timezone).date()
        if day < calendar.first_session.date() or day > calendar.last_session.date():
            continue
        session = calendar.date_to_session(day, direction="next")
        if row.at > calendar.session_close(session):
            session = calendar.next_session(session)
        booked[session.date()] *= row.quantity

    def agrees(day):
        return (
            day in booked
            and day in reported
            and float(booked[day]) == float(reported[day])
        )

    missing_prices = {day for day in booked if not agrees(day)}
    missing_shares = set()
    prior = LedgerReplay(rows)
    for day in sorted(reported):
        if not agrees(day) and calendar.is_session(day):
            prior.advance(
                calendar.session_open(day).to_pydatetime() - timedelta(microseconds=1)
            )
            if prior.quantities[security_id]:
                missing_shares.add(day)
    return booked, missing_prices, missing_shares


def reported_share_splits(snapshot):
    return {
        date.fromisoformat(day): Decimal(ratio)
        for day, ratio in snapshot.get("share_basis_splits", {}).items()
    }


def _to_decimal(value: float | int | Decimal | None) -> Decimal:
    if value is None:
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


class MarketDataService:
    CHART_URL_TEMPLATE = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"

    def __init__(self, session: AsyncSession):
        self.session = session

    @staticmethod
    @asynccontextmanager
    async def _http_client():
        # HTTPX loads TLS configuration synchronously at construction. Keep
        # that work off the API loop and share the pool across each batch.
        client = await asyncio.to_thread(httpx.AsyncClient, timeout=15.0)
        async with client:
            yield client

    async def refresh_live_prices(self) -> dict[str, object]:
        runtime = RuntimeSettingsStore.load().market_data
        if not runtime.enabled:
            return {"updated": 0, "detail": "market_data_disabled"}
        if runtime.provider != "yahoo_finance":
            return {"updated": 0, "detail": f"unsupported_provider={runtime.provider}"}

        rows = list(
            (
                await self.session.execute(
                    select(Position)
                    .where(Position.list_type == "holding", Position.quantity > 0)
                    .options(
                        selectinload(Position.security).selectinload(Security.entity)
                    )
                )
            )
            .scalars()
            .all()
        )
        if not rows:
            return {"updated": 0, "detail": "no_positions"}

        tickers = sorted(
            {
                position.security.ticker.upper()
                for position in rows
                if position.security and position.security.ticker
            }
        )
        quotes = await self.fetch_quotes(tickers)
        if not quotes:
            return {"updated": 0, "detail": "no_quotes_returned"}

        ledger = ()
        observed_at = datetime.now(UTC)
        if any(quote.get("reported_exchange") for quote in quotes.values()):
            from investos.services.broker_equity import BrokerEquityService

            ledger = await BrokerEquityService(self.session).ledger(observed_at)
        updated = 0
        total_market_value = Decimal("0")
        updates: list[Position] = []
        for position in rows:
            security = position.security
            if security is None or not security.ticker:
                continue
            quote = quotes.get(security.ticker.upper())
            if (
                quote is None
                or quote.get("currency") != "USD"
                or quote.get("reported_symbol") != security.ticker.upper()
                or self._finite_number(quote.get("price")) is None
                or float(quote["price"]) < 0
            ):
                continue
            price = _to_decimal(quote.get("price"))
            quantity = _to_decimal(position.quantity)
            avg_cost = _to_decimal(position.avg_cost_basis)

            prior_receipt = getattr(position, "quote_receipt_json", None) or {}
            splits = {
                **prior_receipt.get("share_basis_splits", {}),
                **quote.get("share_basis_splits", {}),
            }
            issue = self.quote_share_basis_issue(quote, splits, ledger, security.id)
            position.quote_receipt_json = {
                "version": 1,
                "security_id": str(security.id),
                "ticker": security.ticker.upper(),
                "instrument_type": security.instrument_type,
                "quantity": str(quantity),
                "direction": position.direction,
                "price": str(price),
                "quoted_at": quote.get("quote_time"),
                "observed_at": observed_at.isoformat(),
                "currency": quote["currency"],
                "provider": "yahoo_finance",
                "session": quote.get("session"),
                "previous_close": quote.get("previous_close"),
                "previous_close_on": quote.get("previous_close_on"),
                "share_basis_splits": splits,
                "share_basis_issue": issue,
            }
            if issue:
                continue
            position.current_price = price
            position.market_value = quantity * price
            position.unrealized_pnl = quantity * (price - avg_cost)
            entity = getattr(security, "entity", None)
            quote_name = str(quote.get("name") or "").strip()
            if (
                entity is not None
                and quote_name
                and entity.name.strip().upper() == security.ticker.upper()
            ):
                entity.name = quote_name
            total_market_value += _to_decimal(position.market_value)
            updates.append(position)
            updated += 1

        # Failed quotes retain their last valuation. Normalize all retained
        # values together; updated-only weights plus old weights can exceed 100%.
        total_market_value = sum(
            (_to_decimal(position.market_value) for position in rows), Decimal("0")
        )
        if total_market_value > 0:
            for position in rows:
                position.weight_pct = float(
                    (_to_decimal(position.market_value) / total_market_value)
                    * Decimal("100")
                )

        await self.session.commit()
        unavailable = [
            p.security.ticker for p in rows if p not in updates and p.security
        ]
        return {
            "updated": updated,
            "detail": "partial" if unavailable else "ok",
            "unavailable_tickers": unavailable,
            "retained_stale_valuations": len(unavailable),
        }

    async def get_live_price(self, ticker: str) -> dict[str, Any]:
        quotes = await self.fetch_quotes([ticker])
        return quotes.get(ticker.upper()) or {}

    @staticmethod
    def quote_share_basis_issue(quote, splits, ledger, security_id):
        exchange = YAHOO_CALENDARS.get(quote.get("reported_exchange"))
        if not exchange or not quote.get("quote_time"):
            return (
                "The quote cannot be matched to the split's trading session."
                if splits
                else None
            )
        if not splits and not any(
            row.security_id == security_id and row.action == "split" for row in ledger
        ):
            return None
        if splits and not any(
            row.security_id == security_id and row.action == "buy" for row in ledger
        ):
            return "The ledger has no acquisition record to verify the split's share basis."

        import exchange_calendars

        calendar = exchange_calendars.get_calendar(exchange)
        if str(calendar.tz) != quote.get("exchange_timezone"):
            return "The quote's exchange time zone cannot verify its share basis."
        day = (
            datetime.fromisoformat(quote["quote_time"])
            .astimezone(ZoneInfo(str(calendar.tz)))
            .date()
        )
        booked, _, missing_shares = share_basis_reconciliation(
            ledger,
            security_id,
            calendar,
            reported_share_splits({"share_basis_splits": splits}),
        )
        if any(split <= day for split in missing_shares):
            return "A reported stock split needs a matching share adjustment in the ledger."
        if any(split > day for split in booked):
            return "The retained quote predates a recorded stock split."
        return None

    async def fetch_quotes(self, tickers: list[str]) -> dict[str, dict[str, Any]]:
        semaphore = asyncio.Semaphore(4)
        normalized = sorted({str(t).strip().upper() for t in tickers if str(t).strip()})
        if not normalized:
            return {}

        async def fetch_one(ticker: str) -> dict[str, dict[str, Any]]:
            async with semaphore:
                try:
                    return await self._fetch_quotes([ticker], client=client)
                except Exception:
                    return {}

        async with self._http_client() as client:
            rows = await asyncio.gather(*(fetch_one(ticker) for ticker in normalized))
        return {ticker: quote for row in rows for ticker, quote in row.items()}

    async def _fetch_quotes(
        self, tickers: list[str], *, client: httpx.AsyncClient
    ) -> dict[str, dict[str, Any]]:
        quotes: dict[str, dict[str, Any]] = {}
        for ticker in tickers:
            response = await client.get(
                self.CHART_URL_TEMPLATE.format(ticker=ticker),
                params={
                    "interval": "1d",
                    "range": "1mo",
                    "includePrePost": "true",
                    "events": "splits",
                },
                headers={"User-Agent": "Mozilla/5.0"},
            )
            response.raise_for_status()
            payload = response.json()
            result = (payload.get("chart") or {}).get("result") or []
            if not result:
                continue
            chart = result[0]
            meta = chart.get("meta") or {}
            market_state = str(meta.get("marketState") or "").strip().upper()
            pre_market_price = meta.get("preMarketPrice")
            post_market_price = meta.get("postMarketPrice")
            regular_market_price = meta.get("regularMarketPrice")
            if market_state.startswith("PRE") and pre_market_price is not None:
                price = pre_market_price
                price_session = "pre_market"
                quote_epoch = meta.get("preMarketTime")
            elif market_state.startswith("POST") and post_market_price is not None:
                price = post_market_price
                price_session = "post_market"
                quote_epoch = meta.get("postMarketTime")
            elif market_state in {"REGULAR", "OPEN"}:
                price = regular_market_price
                price_session = "regular"
                quote_epoch = meta.get("regularMarketTime")
            else:
                price = regular_market_price
                price_session = "historical_close"
                quote_epoch = meta.get("regularMarketTime")
            if price is None:
                closes = (
                    ((chart.get("indicators") or {}).get("quote") or [{}])[0]
                ).get("close") or []
                closes = [item for item in closes if item is not None]
                if closes:
                    price = closes[-1]
                    price_session = "historical_close"
                    quote_epoch = None
            if price is not None:
                exchange_timezone = meta.get("exchangeTimezoneName")
                try:
                    timezone = ZoneInfo(exchange_timezone or "UTC")
                except ZoneInfoNotFoundError:
                    timezone = ZoneInfo("UTC")
                previous_close, previous_close_on = self._previous_close(
                    chart, timezone, price_session
                )
                quotes[ticker.upper()] = {
                    "price": float(price),
                    "previous_close": previous_close,
                    "previous_close_on": previous_close_on,
                    "session": price_session,
                    "market_state": market_state or None,
                    "quote_time": (
                        datetime.fromtimestamp(float(quote_epoch), tz=UTC).isoformat()
                        if quote_epoch
                        else None
                    ),
                    "name": meta.get("longName") or meta.get("shortName"),
                    "currency": meta.get("currency"),
                    "reported_symbol": meta.get("symbol"),
                    "reported_exchange": meta.get("exchangeName"),
                    "exchange_timezone": exchange_timezone,
                    "share_basis_splits": self._reported_splits(
                        chart, timezone, datetime.now(UTC)
                    ),
                }
        return quotes

    async def fetch_chart_series(
        self,
        ticker: str,
        *,
        range_value: str = "3mo",
        period_start: datetime | None = None,
        period_end: datetime | None = None,
    ) -> dict[str, object]:
        params: dict[str, object] = {"interval": "1d"}
        if period_start is not None and period_end is not None:
            params["period1"] = int(period_start.timestamp())
            params["period2"] = int(period_end.timestamp())
        else:
            params["range"] = range_value

        async with self._http_client() as client:
            response = await client.get(
                self.CHART_URL_TEMPLATE.format(ticker=ticker),
                params=params,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            response.raise_for_status()
            payload = response.json()

        result = (payload.get("chart") or {}).get("result") or []
        if not result:
            return {"current_price": None, "series": [], "adjusted_series": []}

        chart = result[0]
        meta = chart.get("meta") or {}
        timestamps = chart.get("timestamp") or []
        closes = (((chart.get("indicators") or {}).get("quote") or [{}])[0]).get(
            "close"
        ) or []
        adjusted_closes = (
            ((chart.get("indicators") or {}).get("adjclose") or [{}])[0]
        ).get("adjclose") or []
        series = [
            (datetime.fromtimestamp(timestamp, tz=UTC), float(close))
            for timestamp, close in zip(timestamps, closes)
            if close is not None
        ]
        adjusted_series = [
            (datetime.fromtimestamp(timestamp, tz=UTC), float(close))
            for timestamp, close in zip(timestamps, adjusted_closes)
            if close is not None
        ]
        current_price = meta.get("regularMarketPrice")
        if current_price is None and series:
            current_price = series[-1][1]
        return {
            "current_price": None if current_price is None else float(current_price),
            "series": series,
            "adjusted_series": adjusted_series,
        }

    async def fetch_signal_snapshot(
        self,
        ticker: str,
        *,
        range_value: str = "6mo",
        cutoff: datetime | None = None,
    ) -> dict[str, object]:
        ticker = ticker.strip().upper()
        snapshots = await self.fetch_signal_snapshots(
            [ticker], range_value=range_value, concurrency=1, cutoff=cutoff
        )
        return snapshots.get(ticker, {})

    async def _fetch_signal_snapshot(
        self,
        ticker: str,
        *,
        range_value: str,
        cutoff: datetime | None,
        known_sessions: list[dict],
        client: httpx.AsyncClient,
    ) -> dict[str, object]:
        """Capture completed daily bars, with acquisition and completion clocks.

        Yahoo's current historical revision cannot establish what was retrievable
        at a past cutoff. Historical evaluations must use a previously frozen packet.
        """
        if cutoff is not None and cutoff < datetime.now(UTC):
            return {
                **self.build_signal_snapshot(ticker=ticker, observations=[]),
                "status": "historical_capture_required",
            }
        response = await client.get(
            self.CHART_URL_TEMPLATE.format(ticker=ticker),
            params={
                "interval": "1d",
                "range": range_value,
                "includeAdjustedClose": "true",
                "events": "splits",
            },
            headers={"User-Agent": "Mozilla/5.0"},
        )
        response.raise_for_status()
        payload = response.json()
        retrieved_at = datetime.now(UTC)

        result = (payload.get("chart") or {}).get("result") or []
        if not result:
            return self.build_signal_snapshot(ticker=ticker, observations=[])
        chart = result[0]
        timestamps = chart.get("timestamp") or []
        quote = ((chart.get("indicators") or {}).get("quote") or [{}])[0]
        closes = quote.get("close") or []
        opens = quote.get("open") or []
        volumes = quote.get("volume") or []
        adjusted = ((chart.get("indicators") or {}).get("adjclose") or [{}])[0].get(
            "adjclose"
        ) or []
        meta = chart.get("meta") or {}
        try:
            timezone = ZoneInfo(meta.get("exchangeTimezoneName") or "UTC")
        except ZoneInfoNotFoundError:
            timezone = ZoneInfo("UTC")
        regular = (meta.get("currentTradingPeriod") or {}).get("regular") or {}
        session_start = regular.get("start")
        session_end = regular.get("end")
        identity = (
            "yahoo_finance",
            meta.get("symbol"),
            meta.get("exchangeName") or "",
            str(timezone),
        )
        remembered = {}
        for period in known_sessions:
            if (
                tuple(
                    period[key]
                    for key in ("provider", "symbol", "exchange", "timezone")
                )
                == identity
                and meta.get("exchangeTimezoneName") == str(timezone)
                and period["observed_at"] <= retrieved_at
            ):
                day = period["opens_at"].astimezone(timezone).date()
                remembered.setdefault(day, period)

        def session_period(timestamp: int) -> dict | None:
            day = datetime.fromtimestamp(timestamp, timezone).date()
            if (
                session_start
                and session_end
                and session_end > session_start
                and datetime.fromtimestamp(session_start, timezone).date() == day
            ):
                return {
                    "opens_at": datetime.fromtimestamp(session_start, UTC),
                    "closes_at": datetime.fromtimestamp(session_end, UTC),
                    "observed_at": retrieved_at,
                }
            return remembered.get(day)

        def regular_session(timestamp: int) -> bool:
            # A same-date daily label alone is not a verified opening time.
            return bool(session_start and session_end and timestamp == session_start)

        def completed_at(timestamp: int) -> datetime:
            day = datetime.fromtimestamp(timestamp, timezone).date()
            period = session_period(timestamp)
            if period:
                return period["closes_at"]
            # Older bars have no verified historical exchange schedule. Wait
            # until the next local date rather than inventing a session close.
            return datetime.combine(
                day + timedelta(days=1), time.min, timezone
            ).astimezone(UTC)

        observations = [
            {
                "timestamp": datetime.fromtimestamp(timestamp, tz=UTC),
                "close": float(close),
                "open": (
                    self._finite_number(opens[index]) if index < len(opens) else None
                ),
                "session_open_at": (
                    datetime.fromtimestamp(session_start, UTC)
                    if regular_session(timestamp)
                    else None
                ),
                "adjusted_close": (
                    self._finite_number(adjusted[index])
                    if index < len(adjusted)
                    else None
                ),
                "completed_at": completed_at(timestamp),
                "completion_observed_at": (
                    session_period(timestamp)["observed_at"]
                    if session_period(timestamp)
                    else None
                ),
                "available_at": retrieved_at,
                "volume": (
                    None
                    if index >= len(volumes) or volumes[index] is None
                    else self._finite_number(volumes[index])
                ),
            }
            for index, (timestamp, close) in enumerate(zip(timestamps, closes))
            if close is not None
        ]
        snapshot = self.build_signal_snapshot(
            ticker=ticker,
            observations=observations,
            cutoff=min(cutoff, retrieved_at) if cutoff else retrieved_at,
        )
        snapshot.update(
            {
                "retrieved_at": retrieved_at.isoformat(),
                "provider": "yahoo_finance",
                "reported_symbol": meta.get("symbol"),
                "reported_exchange": meta.get("exchangeName"),
                "exchange_timezone": str(timezone),
                "as_of_kind": "daily_bar_label",
                "currency": meta.get("currency"),
                "price_basis": "provider_adjusted_current_revision",
                "share_basis_splits": self._reported_splits(
                    chart, timezone, retrieved_at
                ),
                "historical_availability_verified": False,
            }
        )
        # The regular-session quote can carry the final print while the matching
        # daily candle is still empty. Retain it separately from daily signals.
        price = self._finite_number(meta.get("regularMarketPrice"))
        quote_time = self._finite_number(meta.get("regularMarketTime"))
        if (
            session_start
            and session_end
            and session_start < session_end
            and price is not None
            and price > 0
            and quote_time is not None
            and session_end <= quote_time <= retrieved_at.timestamp()
            and datetime.fromtimestamp(quote_time, timezone).date()
            == datetime.fromtimestamp(session_start, timezone).date()
        ):
            snapshot["latest_regular_close"] = {
                "timestamp": datetime.fromtimestamp(session_start, UTC).isoformat(),
                "completed_at": datetime.fromtimestamp(session_end, UTC).isoformat(),
                "quoted_at": datetime.fromtimestamp(quote_time, UTC).isoformat(),
                "completion_observed_at": retrieved_at.isoformat(),
                "close": price,
                "close_basis": "provider_regular_market_quote",
            }
        if (
            meta.get("symbol") == ticker
            and meta.get("exchangeTimezoneName") == str(timezone)
            and session_start in timestamps
            and session_end
            and session_end > session_start
            and session_end <= retrieved_at.timestamp()
            and any(
                bar["timestamp"].timestamp() == session_start
                and self._finite_number(bar["close"]) is not None
                and bar["close"] > 0
                for bar in observations
            )
        ):
            snapshot["observed_session"] = {
                "id": str(uuid4()),
                "provider": identity[0],
                "symbol": identity[1],
                "exchange": identity[2],
                "timezone": identity[3],
                "opens_at": datetime.fromtimestamp(session_start, UTC).isoformat(),
                "closes_at": datetime.fromtimestamp(session_end, UTC).isoformat(),
                "observed_at": retrieved_at.isoformat(),
            }
        if remembered:
            expected = max(remembered)
            latest = (
                datetime.fromisoformat(snapshot["as_of"]).astimezone(timezone).date()
                if snapshot.get("as_of")
                else None
            )
            if latest is None or latest < expected:
                snapshot["status"] = "missing_verified_session"
                snapshot["expected_session_date"] = expected.isoformat()
        return snapshot

    @staticmethod
    def _previous_close(chart, timezone, price_session):
        """The regular-session close that today's change is measured from.

        Before the open the latest regular close is the reference. During and
        after the session, and while the market is closed, it is the close of
        the last daily bar before the session the price belongs to.
        """
        meta = chart.get("meta") or {}
        try:
            session_day = datetime.fromtimestamp(
                float(meta["regularMarketTime"]), timezone
            ).date()
        except (KeyError, OSError, OverflowError, TypeError, ValueError):
            return None, None
        if price_session == "pre_market":
            close = number(meta.get("regularMarketPrice"))
            if close is None or close <= 0:
                return None, None
            return float(close), session_day.isoformat()
        stamps = chart.get("timestamp") or []
        closes = (((chart.get("indicators") or {}).get("quote") or [{}])[0]).get(
            "close"
        ) or []
        previous = None
        for stamp, close in zip(stamps, closes):
            close = number(close)
            if close is None or close <= 0:
                continue
            try:
                day = datetime.fromtimestamp(float(stamp), timezone).date()
            except (OSError, OverflowError, TypeError, ValueError):
                continue
            if day < session_day and (previous is None or day >= previous[1]):
                previous = (float(close), day)
        if previous is None:
            return None, None
        return previous[0], previous[1].isoformat()

    @staticmethod
    def _reported_splits(chart, timezone, observed_at):
        splits = {}
        for event in ((chart.get("events") or {}).get("splits") or {}).values():
            numerator = number(event.get("numerator"))
            denominator = number(event.get("denominator"))
            if (
                numerator is None
                or denominator is None
                or min(numerator, denominator) <= 0
            ):
                continue
            try:
                at = datetime.fromtimestamp(event["date"], timezone)
            except (KeyError, OSError, OverflowError, TypeError, ValueError):
                continue
            if at <= observed_at:
                splits[at.date().isoformat()] = str(numerator / denominator)
        return splits

    async def fetch_signal_snapshots(
        self,
        tickers: list[str],
        *,
        range_value: str = "6mo",
        concurrency: int = 4,
        cutoff: datetime | None = None,
    ) -> dict[str, dict[str, object]]:
        semaphore = asyncio.Semaphore(max(1, min(int(concurrency or 4), 8)))
        now = datetime.now(UTC)
        normalized = sorted(
            {
                str(ticker or "").strip().upper()
                for ticker in tickers
                if str(ticker or "").strip()
            }
        )
        if not normalized:
            return {}
        if cutoff is not None and cutoff < now:
            return {
                ticker: {
                    **self.build_signal_snapshot(ticker=ticker, observations=[]),
                    "status": "historical_capture_required",
                }
                for ticker in normalized
            }
        known_sessions = []
        if self.session is not None and normalized:
            # Read the latest observed session per provider identity, including
            # across weekends, without loading every polling observation.
            periods = (
                (
                    await self.session.execute(
                        select(MarketSessionObservation)
                        .where(
                            MarketSessionObservation.provider == "yahoo_finance",
                            MarketSessionObservation.symbol.in_(normalized),
                            MarketSessionObservation.observed_at <= now,
                        )
                        .distinct(
                            MarketSessionObservation.symbol,
                            MarketSessionObservation.exchange,
                            MarketSessionObservation.timezone,
                        )
                        .order_by(
                            MarketSessionObservation.symbol,
                            MarketSessionObservation.exchange,
                            MarketSessionObservation.timezone,
                            MarketSessionObservation.opens_at.desc(),
                            MarketSessionObservation.observed_at.desc(),
                            MarketSessionObservation.id.desc(),
                        )
                    )
                )
                .scalars()
                .all()
            )
            known_sessions = [
                {
                    key: getattr(period, key)
                    for key in (
                        "provider",
                        "symbol",
                        "exchange",
                        "timezone",
                        "opens_at",
                        "closes_at",
                        "observed_at",
                    )
                }
                for period in periods
            ]

        async def fetch_one(ticker: str) -> tuple[str, dict[str, object] | None]:
            async with semaphore:
                try:
                    return ticker, await self._fetch_signal_snapshot(
                        ticker,
                        range_value=range_value,
                        cutoff=cutoff,
                        known_sessions=known_sessions,
                        client=client,
                    )
                except Exception:
                    return ticker, {
                        "ticker": ticker,
                        "as_of": None,
                        "status": "unavailable",
                    }

        async with self._http_client() as client:
            rows = await asyncio.gather(*(fetch_one(ticker) for ticker in normalized))
        schedules = [
            {
                **snapshot["observed_session"],
                "id": UUID(snapshot["observed_session"]["id"]),
                **{
                    key: datetime.fromisoformat(snapshot["observed_session"][key])
                    for key in ("opens_at", "closes_at", "observed_at")
                },
            }
            for _, snapshot in rows
            if snapshot and snapshot.get("observed_session")
        ]
        if self.session is not None and schedules:
            # Network tasks finish before using the shared database session.
            await self.session.execute(insert(MarketSessionObservation), schedules)
        return {ticker: snapshot for ticker, snapshot in rows if snapshot}

    @staticmethod
    def build_signal_snapshot(
        *,
        ticker: str,
        observations: list[dict[str, object]],
        cutoff: datetime | None = None,
    ) -> dict[str, object]:
        """Compute generic observable tape features; leave interpretation to the analyst."""
        clean = [
            item
            for item in observations
            if isinstance(item.get("timestamp"), datetime)
            and MarketDataService._finite_number(item.get("close")) is not None
            and float(item["close"]) > 0
            and (
                cutoff is None
                or all(
                    isinstance(item.get(clock), datetime)
                    and item[clock].tzinfo is not None
                    and item[clock] <= cutoff
                    for clock in ("timestamp", "completed_at", "available_at")
                )
            )
            and (
                cutoff is None
                or item.get("completion_observed_at") is None
                or (
                    isinstance(item["completion_observed_at"], datetime)
                    and item["completion_observed_at"] <= cutoff
                )
            )
        ]
        clean.sort(key=lambda item: item["timestamp"])
        if not clean:
            return {
                "ticker": ticker.upper(),
                "as_of": None,
                "signal_ref": None,
                "observations": 0,
            }

        raw_closes = [float(item["close"]) for item in clean]
        adjusted_complete = all(
            MarketDataService._finite_number(item.get("adjusted_close")) is not None
            and float(item["adjusted_close"]) > 0
            for item in clean
        )
        closes = (
            [float(item["adjusted_close"]) for item in clean]
            if adjusted_complete
            else raw_closes
        )
        current = closes[-1]
        as_of = clean[-1]["timestamp"]
        daily_returns = [
            (closes[index] / closes[index - 1]) - 1.0
            for index in range(1, len(closes))
            if closes[index - 1] > 0
        ]
        recent_volumes = [
            float(item["volume"])
            for item in clean[-21:-1]
            if MarketDataService._finite_number(item.get("volume")) is not None
            and float(item["volume"]) >= 0
        ]
        current_volume = MarketDataService._finite_number(clean[-1].get("volume"))
        average_volume = (
            sum(recent_volumes) / len(recent_volumes) if recent_volumes else None
        )

        def period_return(lookback: int) -> float | None:
            if len(closes) <= lookback or closes[-lookback - 1] <= 0:
                return None
            return round(((current / closes[-lookback - 1]) - 1.0) * 100.0, 3)

        def moving_average(window: int) -> float | None:
            if len(closes) < window:
                return None
            return round(sum(closes[-window:]) / window, 4)

        high = max(closes)
        volatility = None
        if len(daily_returns) >= 5:
            volatility = round(
                statistics.stdev(daily_returns[-20:]) * math.sqrt(252) * 100.0, 3
            )
        volume_ratio = None
        if current_volume is not None and average_volume and average_volume > 0:
            volume_ratio = round(current_volume / average_volume, 3)

        return {
            "ticker": ticker.upper(),
            "as_of": as_of.isoformat(),
            "signal_ref": f"market:{ticker.upper()}:{as_of.date().isoformat()}",
            "observations": len(clean),
            "current_price": round(raw_closes[-1], 4),
            "return_price_basis": (
                "provider_adjusted" if adjusted_complete else "unadjusted"
            ),
            "completed_at": clean[-1].get("completed_at", as_of).isoformat(),
            "available_at": clean[-1].get("available_at", as_of).isoformat(),
            "return_1d_pct": period_return(1),
            "return_5d_pct": period_return(5),
            "return_20d_pct": period_return(20),
            "return_60d_pct": period_return(60),
            "return_120d_pct": period_return(120),
            "moving_average_20d": moving_average(20),
            "moving_average_50d": moving_average(50),
            "drawdown_from_period_high_pct": (
                round(((current / high) - 1.0) * 100.0, 3) if high > 0 else None
            ),
            "annualized_volatility_20d_pct": volatility,
            "latest_volume_vs_prior_20d": volume_ratio,
            "volume_comparison_observations": len(recent_volumes),
            "volume_window": "latest_completed_session_vs_prior_up_to_20_sessions",
            "daily_observations": [
                {
                    key: value.isoformat() if isinstance(value, datetime) else value
                    for key, value in item.items()
                }
                for item in clean
            ],
        }

    @staticmethod
    def _finite_number(value: object) -> float | None:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if math.isfinite(number) else None
