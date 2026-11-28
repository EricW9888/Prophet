"""Notable insider buying anywhere in the US market, as a discovery signal for Ideas.

The portfolio is a reference, not a boundary. Insiders rarely buy their own
company's stock in the open market unless they expect it to do well, and
several insiders buying within a few weeks is a stronger sign. Once a day this
reads the SEC's daily index of all filings, keeps open-market purchases (Form 4
code P) worth at least ``min_value``, records each as insider evidence in the
same source as the followed-company lane (the two share external ids, so a
trade is never recorded twice), and puts the company in the Ideas universe
with a raised priority and a plain reason, so a scan researches it soon.

Sales are not discovery signals: executives sell for many reasons. Sales are
recorded for followed companies by ``insider_filings``.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from investos.models.evidence import RawEvidence
from investos.models.source import Source
from investos.services.business_map import BusinessMapService, Fetch
from investos.services.insider_filings import (
    SOURCE_NAME,
    InsiderTransaction,
    _first,
    combine,
    describe,
    parse_form4,
)
from investos.services.opportunity import OpportunityDiscoveryService
from investos.services.ownership_signals import OwnershipSignalService

QUARTER_LISTING = (
    "https://www.sec.gov/Archives/edgar/daily-index/{year}/QTR{quarter}/index.json"
)
DAILY_INDEX = (
    "https://www.sec.gov/Archives/edgar/daily-index/{year}/QTR{quarter}/{name}"
)
ARCHIVES = "https://www.sec.gov/Archives/"
STATE_FILE = "market_insider_buying.json"
# A filing that cannot be fetched is retried on later runs, then given up
# and counted, so one broken filing never holds back the days after it.
MAX_FILING_ATTEMPTS = 5
FIRST_RUN_LOOKBACK = timedelta(days=7)
MAX_DAYS_PER_RUN = 5
CLUSTER_WINDOW = timedelta(days=30)
_OWNERSHIP = re.compile(r"<ownershipDocument>.*?</ownershipDocument>", re.S)
_DAILY_NAME = re.compile(r"^form\.(\d{8})\.idx$")


@dataclass
class IssuerBuying:
    ticker: str
    name: str
    buyers: set[str]
    total_value: float
    first_day: date
    last_day: date


def form4_filings(index: str) -> list[tuple[str, str]]:
    """(accession, archive path) for each distinct Form 4 in a daily form index.

    A filing appears once per party (each reporting owner and the issuer), so
    rows are collapsed by accession number.
    """
    seen: dict[str, str] = {}
    for line in index.splitlines():
        if not line.startswith("4 "):
            continue
        path = line.split()[-1]
        accession = path.rsplit("/", 1)[-1].removesuffix(".txt")
        seen.setdefault(accession, path)
    return list(seen.items())


def is_notable_buy(transaction: InsiderTransaction, min_value: float) -> bool:
    return transaction.code == "P" and (transaction.value or 0.0) >= min_value


def quarter_of(day: date) -> int:
    return (day.month - 1) // 3 + 1


def priority_for(buying: IssuerBuying) -> float:
    """More buyers and more money rank higher; never above 0.95."""
    buyers_bonus = 0.12 * min(len(buying.buyers) - 1, 3)
    value_bonus = 0.05 * max(0.0, math.log10(max(buying.total_value, 1.0)) - 5)
    return round(min(0.95, 0.6 + buyers_bonus + value_bonus), 3)


def money(value: float) -> str:
    if value >= 1e6:
        return f"${value / 1e6:,.1f}M"
    return f"${value / 1e3:,.0f}K"


def reason_for(buying: IssuerBuying) -> str:
    people = (
        "1 insider" if len(buying.buyers) == 1 else f"{len(buying.buyers)} insiders"
    )
    span = (
        f"{buying.first_day:%b %d}"
        if buying.first_day == buying.last_day
        else f"{buying.first_day:%b %d} to {buying.last_day:%b %d}"
    )
    return (
        f"Insider buying: {people} bought {money(buying.total_value)} "
        f"in the open market ({span})"
    )


class MarketInsiderBuyingService:
    def __init__(
        self,
        session: AsyncSession,
        fetch: Fetch | None = None,
        *,
        min_value: float = 100_000,
    ):
        self.session = session
        self.maps = BusinessMapService(session, fetch=fetch)
        self.min_value = min_value

    async def published_days(self, after: date, today: date) -> list[date]:
        """Days with a published daily index, after ``after`` and before ``today``.

        The SEC lists each quarter's files, so holidays and days not yet
        published are never guessed.
        """
        quarters = sorted(
            {(day.year, quarter_of(day)) for day in (after + timedelta(days=1), today)}
        )
        days: list[date] = []
        for year, quarter in quarters:
            listing = json.loads(
                await self.maps._get(QUARTER_LISTING.format(year=year, quarter=quarter))
            )
            for item in listing.get("directory", {}).get("item", []):
                match = _DAILY_NAME.match(str(item.get("name", "")))
                if not match:
                    continue
                day = datetime.strptime(match.group(1), "%Y%m%d").date()
                if after < day < today:
                    days.append(day)
        return sorted(set(days))

    async def refresh(self, *, today: date | None = None) -> dict[str, int]:
        today = today or datetime.now(UTC).date()
        state = self.maps._read_state(STATE_FILE)
        after = (
            date.fromisoformat(state["last_day"])
            if state.get("last_day")
            else today - FIRST_RUN_LOOKBACK
        )
        days = (await self.published_days(after, today))[:MAX_DAYS_PER_RUN]
        counts = {
            "days": 0,
            "filings": 0,
            "buys": 0,
            "companies": 0,
            "unread": 0,
            "given_up": 0,
        }
        bought: set[str] = set()
        signals = OwnershipSignalService(self.session)
        retry: dict[str, dict] = dict(state.get("retry") or {})

        async def read(accession: str, path: str, day: date) -> None:
            try:
                full = await self.maps._get(ARCHIVES + path)
            except Exception:
                attempts = int(retry.get(accession, {}).get("attempts", 0)) + 1
                if attempts >= MAX_FILING_ATTEMPTS:
                    retry.pop(accession, None)
                    counts["given_up"] += 1
                else:
                    retry[accession] = {
                        "path": path,
                        "day": day.isoformat(),
                        "attempts": attempts,
                    }
                return
            retry.pop(accession, None)
            await self._read_filing(full, accession, path, day, signals, counts, bought)

        # Filings that failed on an earlier run come first.
        for accession, entry in list(retry.items()):
            await read(accession, entry["path"], date.fromisoformat(entry["day"]))
        for day in days:
            index = await self.maps._get(
                DAILY_INDEX.format(
                    year=day.year,
                    quarter=quarter_of(day),
                    name=f"form.{day:%Y%m%d}.idx",
                )
            )
            for accession, path in form4_filings(index):
                await read(accession, path, day)
            counts["days"] += 1
            state["last_day"] = day.isoformat()
            state["retry"] = retry
            self.maps._write_state(STATE_FILE, state)
        if not days:
            state["retry"] = retry
            self.maps._write_state(STATE_FILE, state)
        counts["unread"] = len(retry)
        for ticker in sorted(bought):
            buying = await self.recent_buying(ticker, today)
            if buying and await self._add_to_ideas(buying):
                counts["companies"] += 1
        return counts

    async def _read_filing(
        self,
        full: str,
        accession: str,
        path: str,
        day: date,
        signals: OwnershipSignalService,
        counts: dict[str, int],
        bought: set[str],
    ) -> None:
        counts["filings"] += 1
        document = _OWNERSHIP.search(full)
        if not document:
            return
        xml = document.group(0)
        ticker = (_first(xml, "issuerTradingSymbol") or "").upper().strip()
        if not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", ticker):
            return
        issuer = _first(xml, "issuerName") or ticker
        for transaction in combine(parse_form4(xml)):
            if not is_notable_buy(transaction, self.min_value):
                continue
            title, summary = describe(transaction, ticker, day)
            await signals.ingest_disclosure(
                source_name=SOURCE_NAME,
                source_type="filing",
                source_url="https://www.sec.gov/",
                source_description="Insider trades reported to the SEC on Form 4 by officers, directors and 10% owners.",
                source_item_type="insider_disclosure",
                title=title,
                url=ARCHIVES + path,
                external_id=f"{accession}:{transaction.code}",
                author=transaction.owner,
                metadata={
                    "ticker": ticker,
                    "issuer": issuer,
                    "owner": transaction.owner,
                    "role": transaction.role,
                    "transaction_code": "P",
                    "transaction_type": "bought",
                    "shares": transaction.shares,
                    "price": transaction.price,
                    "transaction_value": transaction.value,
                    "acquired": True,
                    "shares_owned_after": transaction.owned_after,
                    "under_10b5_1_plan": transaction.under_plan,
                    "transaction_date": transaction.transaction_date.isoformat(),
                    "last_transaction_date": (
                        transaction.last_date.isoformat()
                        if transaction.last_date
                        else None
                    ),
                    "trades": transaction.trades,
                    "filing_date": day.isoformat(),
                    "accession": accession,
                    "discovery_feed": True,
                },
                summary=summary,
            )
            counts["buys"] += 1
            bought.add(ticker)

    async def recent_buying(self, ticker: str, today: date) -> IssuerBuying | None:
        """Open-market buying in ``ticker`` over the last 30 days, from stored evidence.

        Read from what is recorded rather than one run, so insiders who buy
        on different days count together.
        """
        since = datetime.combine(today - CLUSTER_WINDOW, datetime.min.time(), UTC)
        rows = (
            await self.session.scalars(
                select(RawEvidence)
                .join(Source, RawEvidence.source_id == Source.id)
                .where(
                    Source.name == SOURCE_NAME,
                    RawEvidence.metadata_json["ticker"].astext == ticker,
                    RawEvidence.metadata_json["transaction_code"].astext == "P",
                    RawEvidence.event_time >= since,
                )
            )
        ).all()
        buying: IssuerBuying | None = None
        for row in rows:
            metadata: dict[str, Any] = row.metadata_json or {}
            value = float(metadata.get("transaction_value") or 0.0)
            if value < self.min_value or row.event_time is None:
                continue
            day = row.event_time.date()
            if buying is None:
                buying = IssuerBuying(
                    ticker, str(metadata.get("issuer") or ticker), set(), 0.0, day, day
                )
            buying.buyers.add(str(metadata.get("owner") or "Unknown insider"))
            buying.total_value += value
            buying.first_day = min(buying.first_day, day)
            buying.last_day = max(buying.last_day, day)
        return buying

    async def _add_to_ideas(self, buying: IssuerBuying) -> bool:
        member = await OpportunityDiscoveryService(self.session).note_universe_signal(
            ticker=buying.ticker,
            entity_name=buying.name,
            source_type="insider_buying",
            source_id=buying.ticker,
            label=reason_for(buying),
            priority=priority_for(buying),
            metadata={
                "buyers": sorted(buying.buyers),
                "total_value": buying.total_value,
                "first_day": buying.first_day.isoformat(),
                "last_day": buying.last_day.isoformat(),
            },
        )
        return member is not None
