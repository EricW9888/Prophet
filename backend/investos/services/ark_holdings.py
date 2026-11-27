"""ARK Invest's daily trades, derived from its published daily holdings.

ARK (Cathie Wood) publishes each active ETF's full holdings every trading day.
Comparing one day's file with the previous day's gives the shares ARK added or
removed per company, a public and timely view of a closely followed
manager's moves. Each change is recorded as institutional-flow evidence
through the ownership lane, in plain words. The first run stores a baseline;
trades appear from the next published day.
"""

from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Awaitable, Callable
from urllib.parse import quote

from sqlalchemy.ext.asyncio import AsyncSession

from investos.config import settings
from investos.core.url_security import fetch_public_document
from investos.services.ownership_signals import OwnershipSignalService

CSV_BASE = "https://assets.ark-funds.com/fund-documents/funds-etf-csv/"
# ARK has renamed funds and keeps older file names published; the file with
# the newest date wins.
FUNDS = {
    "ARKK": ["ARK_INNOVATION_ETF_ARKK_HOLDINGS"],
    "ARKW": ["ARK_NEXT_GENERATION_INTERNET_ETF_ARKW_HOLDINGS"],
    "ARKQ": ["ARK_AUTONOMOUS_TECH._&_ROBOTICS_ETF_ARKQ_HOLDINGS"],
    "ARKG": ["ARK_GENOMIC_REVOLUTION_ETF_ARKG_HOLDINGS"],
    "ARKF": [
        "ARK_BLOCKCHAIN_&_FINTECH_INNOVATION_ETF_ARKF_HOLDINGS",
        "ARK_FINTECH_INNOVATION_ETF_ARKF_HOLDINGS",
    ],
    "ARKX": [
        "ARK_SPACE_&_DEFENSE_INNOVATION_ETF_ARKX_HOLDINGS",
        "ARK_SPACE_EXPLORATION_&_INNOVATION_ETF_ARKX_HOLDINGS",
    ],
}
SOURCE_NAME = "ARK Invest daily holdings"
# Ignore rounding-size changes (for example share-count corrections).
MIN_CHANGE_FRACTION = 0.001

Fetch = Callable[[str], Awaitable[str]]


@dataclass(frozen=True)
class Holding:
    company: str
    ticker: str
    shares: float
    market_value: float


@dataclass(frozen=True)
class FundDay:
    fund: str
    day: date
    holdings: dict[str, Holding]  # by ticker, or by company name when no ticker


@dataclass(frozen=True)
class Trade:
    fund: str
    day: date
    previous_day: date
    company: str
    ticker: str
    shares_before: float
    shares_after: float
    price: float | None

    @property
    def change(self) -> float:
        return self.shares_after - self.shares_before


def _number(text: str) -> float:
    cleaned = (text or "").replace(",", "").replace("$", "").replace("%", "").strip()
    return float(cleaned) if cleaned else 0.0


def parse_holdings(text: str, fund: str) -> FundDay | None:
    rows = [
        row
        for row in csv.DictReader(io.StringIO(text))
        if (row.get("fund") or "").strip() == fund
    ]
    if not rows:
        return None
    day = datetime.strptime(rows[0]["date"].strip(), "%m/%d/%Y").date()
    holdings = {}
    for row in rows:
        ticker = (row.get("ticker") or "").strip().upper()
        company = (row.get("company") or "").strip()
        key = ticker or company
        if not key:
            continue
        holdings[key] = Holding(
            company=company,
            ticker=ticker,
            shares=_number(row.get("shares", "")),
            market_value=_number(row.get("market value ($)", "")),
        )
    return FundDay(fund=fund, day=day, holdings=holdings)


def trades_between(previous: FundDay, current: FundDay) -> list[Trade]:
    """Positions ARK added to, trimmed, opened or closed between two days."""
    trades = []
    for key in sorted(set(previous.holdings) | set(current.holdings)):
        before = previous.holdings.get(key)
        after = current.holdings.get(key)
        shares_before = before.shares if before else 0.0
        shares_after = after.shares if after else 0.0
        if (
            abs(shares_after - shares_before)
            <= max(shares_before, shares_after) * MIN_CHANGE_FRACTION
        ):
            continue
        reference = after or before
        price = (
            after.market_value / after.shares
            if after and after.shares
            else (
                before.market_value / before.shares
                if before and before.shares
                else None
            )
        )
        trades.append(
            Trade(
                fund=current.fund,
                day=current.day,
                previous_day=previous.day,
                company=reference.company,
                ticker=reference.ticker,
                shares_before=shares_before,
                shares_after=shares_after,
                price=price,
            )
        )
    return trades


def describe(trade: Trade) -> tuple[str, str]:
    name = trade.ticker or trade.company
    shares = f"{abs(trade.change):,.0f} {name} shares"
    if trade.shares_before == 0:
        verb = f"opened a position: bought {shares}"
    elif trade.shares_after == 0:
        verb = f"closed its position: sold {shares}"
    elif trade.change > 0:
        verb = f"bought {shares}"
    else:
        verb = f"sold {shares}"
    title = f"ARK ({trade.fund}) {verb}"
    parts = [f"{title} between {trade.previous_day:%b %d} and {trade.day:%b %d, %Y}."]
    if trade.price:
        parts.append(
            f"Value about ${abs(trade.change) * trade.price:,.0f} at about ${trade.price:,.2f} a share."
        )
    parts.append(
        f"{trade.fund} now holds {trade.shares_after:,.0f} shares of {trade.company or name}."
    )
    parts.append("Derived by comparing ARK's published daily holdings files.")
    return title, " ".join(parts)


async def _fetch_text(url: str) -> str:
    document = await fetch_public_document(
        url, max_bytes=2_000_000, timeout_seconds=40, allow_binary_text=True
    )
    return document.text()


class ArkHoldingsService:
    def __init__(self, session: AsyncSession, fetch: Fetch | None = None):
        self.session = session
        self.fetch = fetch or _fetch_text

    @staticmethod
    def _snapshot_path(fund: str) -> Path:
        return Path(settings.STORAGE_DIR) / "_system" / "ark" / f"{fund}.json"

    def _load(self, fund: str) -> FundDay | None:
        try:
            raw = json.loads(self._snapshot_path(fund).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return FundDay(
            fund=fund,
            day=date.fromisoformat(raw["day"]),
            holdings={key: Holding(**value) for key, value in raw["holdings"].items()},
        )

    def _save(self, snapshot: FundDay) -> None:
        path = self._snapshot_path(snapshot.fund)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "day": snapshot.day.isoformat(),
                    "holdings": {
                        key: vars(value) for key, value in snapshot.holdings.items()
                    },
                }
            ),
            encoding="utf-8",
        )

    async def latest(self, fund: str) -> FundDay | None:
        best = None
        for name in FUNDS[fund]:
            try:
                parsed = parse_holdings(
                    await self.fetch(CSV_BASE + quote(name) + ".csv"), fund
                )
            except Exception:
                continue
            if parsed and (best is None or parsed.day > best.day):
                best = parsed
        return best

    async def refresh(self) -> dict[str, int]:
        counts = {"funds": 0, "trades": 0}
        signals = OwnershipSignalService(self.session)
        for fund in FUNDS:
            current = await self.latest(fund)
            if current is None:
                continue
            counts["funds"] += 1
            previous = self._load(fund)
            if previous is not None and current.day > previous.day:
                for trade in trades_between(previous, current):
                    title, summary = describe(trade)
                    await signals.ingest_disclosure(
                        source_name=SOURCE_NAME,
                        source_type="ownership_tracker",
                        source_url="https://www.ark-funds.com/",
                        source_description="Daily holdings of ARK Invest's active ETFs (Cathie Wood), compared day to day.",
                        source_item_type="institutional_flow",
                        title=title,
                        url=CSV_BASE + quote(FUNDS[fund][0]) + ".csv",
                        external_id=f"ark:{fund}:{trade.day.isoformat()}:{trade.ticker or trade.company}",
                        author="ARK Invest",
                        metadata={
                            "fund": fund,
                            "ticker": trade.ticker or None,
                            "issuer": trade.company,
                            "shares_before": trade.shares_before,
                            "shares_after": trade.shares_after,
                            "shares": abs(trade.change),
                            "transaction_type": "buy" if trade.change > 0 else "sell",
                            "price": trade.price,
                            "transaction_value": (
                                abs(trade.change) * trade.price if trade.price else None
                            ),
                            "transaction_date": trade.day.isoformat(),
                            "previous_holdings_date": trade.previous_day.isoformat(),
                        },
                        summary=summary,
                    )
                    counts["trades"] += 1
            if previous is None or current.day >= previous.day:
                self._save(current)
        return counts
