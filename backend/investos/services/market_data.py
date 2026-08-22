from __future__ import annotations

import asyncio
import math
import statistics
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from investos.models.entity import Security
from investos.models.portfolio import Position
from investos.services.runtime_settings import RuntimeSettingsStore


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

        updated = 0
        total_market_value = Decimal("0")
        updates: list[Position] = []
        for position in rows:
            security = position.security
            if security is None or not security.ticker:
                continue
            quote = quotes.get(security.ticker.upper())
            if quote is None:
                continue
            price = _to_decimal(quote.get("price"))
            quantity = _to_decimal(position.quantity)
            avg_cost = _to_decimal(position.avg_cost_basis)

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

        if total_market_value > 0:
            for position in updates:
                position.weight_pct = float(
                    (_to_decimal(position.market_value) / total_market_value)
                    * Decimal("100")
                )

        await self.session.commit()
        return {"updated": updated, "detail": "ok"}

    async def get_live_price(self, ticker: str) -> dict[str, Any]:
        quotes = await self.fetch_quotes([ticker])
        return quotes.get(ticker.upper()) or {}

    async def fetch_quotes(self, tickers: list[str]) -> dict[str, dict[str, Any]]:
        quotes: dict[str, dict[str, Any]] = {}
        async with httpx.AsyncClient(timeout=15.0) as client:
            for ticker in tickers:
                response = await client.get(
                    self.CHART_URL_TEMPLATE.format(ticker=ticker),
                    params={"interval": "1d", "range": "1d", "includePrePost": "true"},
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
                elif market_state.startswith("POST") and post_market_price is not None:
                    price = post_market_price
                    price_session = "post_market"
                elif market_state in {"REGULAR", "OPEN"}:
                    price = regular_market_price
                    price_session = "regular"
                else:
                    price = regular_market_price
                    price_session = "historical_close"
                if price is None:
                    closes = (
                        ((chart.get("indicators") or {}).get("quote") or [{}])[0]
                    ).get("close") or []
                    closes = [item for item in closes if item is not None]
                    if closes:
                        price = closes[-1]
                        price_session = "historical_close"
                if price is not None:
                    quotes[ticker.upper()] = {
                        "price": float(price),
                        "session": price_session,
                        "market_state": market_state or None,
                        "quote_time": (
                            datetime.fromtimestamp(
                                float(meta["regularMarketTime"]), tz=UTC
                            ).isoformat()
                            if meta.get("regularMarketTime")
                            else None
                        ),
                        "name": meta.get("longName") or meta.get("shortName"),
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

        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(
                self.CHART_URL_TEMPLATE.format(ticker=ticker),
                params=params,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            response.raise_for_status()
            payload = response.json()

        result = (payload.get("chart") or {}).get("result") or []
        if not result:
            return {"current_price": None, "series": []}

        chart = result[0]
        meta = chart.get("meta") or {}
        timestamps = chart.get("timestamp") or []
        closes = (((chart.get("indicators") or {}).get("quote") or [{}])[0]).get(
            "close"
        ) or []
        series = [
            (datetime.fromtimestamp(timestamp, tz=UTC), float(close))
            for timestamp, close in zip(timestamps, closes)
            if close is not None
        ]
        current_price = meta.get("regularMarketPrice")
        if current_price is None and series:
            current_price = series[-1][1]
        return {
            "current_price": None if current_price is None else float(current_price),
            "series": series,
        }

    async def fetch_signal_snapshot(
        self,
        ticker: str,
        *,
        range_value: str = "6mo",
    ) -> dict[str, object]:
        """Return a point-in-time tape snapshot without interpreting the investment case."""
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(
                self.CHART_URL_TEMPLATE.format(ticker=ticker),
                params={"interval": "1d", "range": range_value},
                headers={"User-Agent": "Mozilla/5.0"},
            )
            response.raise_for_status()
            payload = response.json()

        result = (payload.get("chart") or {}).get("result") or []
        if not result:
            return self.build_signal_snapshot(ticker=ticker, observations=[])
        chart = result[0]
        timestamps = chart.get("timestamp") or []
        quote = ((chart.get("indicators") or {}).get("quote") or [{}])[0]
        closes = quote.get("close") or []
        volumes = quote.get("volume") or []
        observations = [
            {
                "timestamp": datetime.fromtimestamp(timestamp, tz=UTC),
                "close": float(close),
                "volume": (
                    None
                    if index >= len(volumes) or volumes[index] is None
                    else float(volumes[index])
                ),
            }
            for index, (timestamp, close) in enumerate(zip(timestamps, closes))
            if close is not None
        ]
        return self.build_signal_snapshot(ticker=ticker, observations=observations)

    async def fetch_signal_snapshots(
        self,
        tickers: list[str],
        *,
        range_value: str = "6mo",
        concurrency: int = 4,
    ) -> dict[str, dict[str, object]]:
        semaphore = asyncio.Semaphore(max(1, min(int(concurrency or 4), 8)))

        async def fetch_one(ticker: str) -> tuple[str, dict[str, object] | None]:
            async with semaphore:
                try:
                    return ticker, await self.fetch_signal_snapshot(
                        ticker, range_value=range_value
                    )
                except Exception:
                    return ticker, None

        normalized = sorted(
            {
                str(ticker or "").strip().upper()
                for ticker in tickers
                if str(ticker or "").strip()
            }
        )
        rows = await asyncio.gather(*(fetch_one(ticker) for ticker in normalized))
        return {
            ticker: snapshot
            for ticker, snapshot in rows
            if snapshot and snapshot.get("as_of")
        }

    @staticmethod
    def build_signal_snapshot(
        *,
        ticker: str,
        observations: list[dict[str, object]],
    ) -> dict[str, object]:
        """Compute generic observable tape features; leave interpretation to the analyst."""
        clean = [
            item
            for item in observations
            if isinstance(item.get("timestamp"), datetime)
            and MarketDataService._finite_number(item.get("close")) is not None
        ]
        clean.sort(key=lambda item: item["timestamp"])
        if not clean:
            return {
                "ticker": ticker.upper(),
                "as_of": None,
                "signal_ref": None,
                "observations": 0,
            }

        closes = [float(item["close"]) for item in clean]
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
            "current_price": round(current, 4),
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
        }

    @staticmethod
    def _finite_number(value: object) -> float | None:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if math.isfinite(number) else None
