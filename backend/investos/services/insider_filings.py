"""Insider trades (SEC Form 4) for the companies the owner holds or watches.

Officers, directors and 10% owners must report trades within two business
days. Prophet's SEC feeds excluded these filings, so the ownership lane
(``ownership_signals``) had nothing to read. This reads each followed
company's recent Form 4 filings from EDGAR and records every reported
transaction as insider-disclosure evidence, in plain words.
"""

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from investos.models.entity import Entity, Security
from investos.models.portfolio import Position
from investos.services.business_map import (
    ARCHIVE_URL,
    SUBMISSIONS_URL,
    BusinessMapService,
    Fetch,
)
from investos.services.ownership_signals import OwnershipSignalService

FIRST_LOOKBACK = timedelta(days=90)
SOURCE_NAME = "SEC EDGAR insider filings (Form 4)"
# Form 4 transaction codes, in plain words.
TRANSACTION_TYPES = {
    "P": "bought {shares}",
    "S": "sold {shares}",
    "A": "received {shares} as a grant or award",
    "M": "exercised options for {shares}",
    "X": "exercised options for {shares}",
    "C": "converted securities into {shares}",
    "F": "had {shares} withheld for taxes",
    "G": "gave {shares} as a gift",
    "D": "returned {shares} to the company",
    "J": "reported another transaction in {shares}",
}


@dataclass(frozen=True)
class InsiderTransaction:
    owner: str
    role: str
    transaction_date: date
    code: str
    shares: float
    price: float | None
    acquired: bool
    owned_after: float | None
    under_plan: bool
    last_date: date | None = None
    trades: int = 1

    @property
    def value(self) -> float | None:
        return self.shares * self.price if self.price else None


def _values(block: str, tag: str) -> list[str]:
    return [
        html.unescape(value).strip()
        for value in re.findall(
            rf"<{tag}>\s*(?:<value>)?\s*([^<]*?)\s*(?:</value>)?\s*</{tag}>",
            block,
            re.S,
        )
    ]


def _first(block: str, tag: str) -> str | None:
    found = _values(block, tag)
    return found[0] if found and found[0] else None


def _float(text: str | None) -> float | None:
    try:
        return float(text) if text not in (None, "") else None
    except ValueError:
        return None


def parse_form4(xml: str) -> list[InsiderTransaction]:
    """Non-derivative (common stock) transactions reported in one Form 4."""
    owner = _first(xml, "rptOwnerName") or "An insider"
    roles = []
    if _first(xml, "isDirector") in {"1", "true"}:
        roles.append("director")
    if _first(xml, "isOfficer") in {"1", "true"}:
        roles.append(_first(xml, "officerTitle") or "officer")
    if _first(xml, "isTenPercentOwner") in {"1", "true"}:
        roles.append("10% owner")
    role = ", ".join(roles) or "insider"
    under_plan = _first(xml, "aff10b5One") in {"1", "true"}
    transactions = []
    for block in re.findall(
        r"<nonDerivativeTransaction>(.*?)</nonDerivativeTransaction>", xml, re.S
    ):
        when = _first(block, "transactionDate")
        code = _first(block, "transactionCode")
        shares = _float(_first(block, "transactionShares"))
        if not when or not code or shares is None:
            continue
        transactions.append(
            InsiderTransaction(
                owner=owner,
                role=role,
                transaction_date=date.fromisoformat(when[:10]),
                code=code,
                shares=shares,
                price=_float(_first(block, "transactionPricePerShare")),
                acquired=_first(block, "transactionAcquiredDisposedCode") == "A",
                owned_after=_float(_first(block, "sharesOwnedFollowingTransaction")),
                under_plan=under_plan,
            )
        )
    return transactions


def combine(transactions: list[InsiderTransaction]) -> list[InsiderTransaction]:
    """One trade per transaction code in a filing.

    A Form 4 often lists one decision as many rows: a sale filled at a dozen
    prices over two days. Rows with the same code are one trade, with total
    shares, the average price, and the span of dates.
    """
    groups: dict[str, list[InsiderTransaction]] = {}
    for transaction in transactions:
        groups.setdefault(transaction.code, []).append(transaction)
    combined = []
    for rows in groups.values():
        first = min(row.transaction_date for row in rows)
        last = max(row.transaction_date for row in rows)
        priced = [row for row in rows if row.price]
        priced_shares = sum(row.shares for row in priced)
        latest = max(rows, key=lambda row: row.transaction_date)
        combined.append(
            InsiderTransaction(
                owner=rows[0].owner,
                role=rows[0].role,
                transaction_date=first,
                code=rows[0].code,
                shares=sum(row.shares for row in rows),
                price=(
                    sum(row.shares * row.price for row in priced) / priced_shares
                    if priced_shares
                    else None
                ),
                acquired=rows[0].acquired,
                owned_after=latest.owned_after,
                under_plan=any(row.under_plan for row in rows),
                last_date=last if last != first else None,
                trades=len(rows),
            )
        )
    return combined


def describe(
    transaction: InsiderTransaction, ticker: str, filed: date
) -> tuple[str, str]:
    """Plain-language title and summary for one trade."""
    template = TRANSACTION_TYPES.get(
        transaction.code, "reported a transaction in {shares}"
    )
    shares = f"{transaction.shares:,.0f} {ticker} shares"
    price = (
        ""
        if not transaction.price
        else (
            f" at an average ${transaction.price:,.2f}"
            if transaction.trades > 1
            else f" at ${transaction.price:,.2f}"
        )
    )
    title = f"{transaction.owner} ({transaction.role}) {template.format(shares=shares)}{price}"
    when = (
        f"between {transaction.transaction_date:%b %d} and {transaction.last_date:%b %d, %Y}"
        if transaction.last_date
        else f"on {transaction.transaction_date:%b %d, %Y}"
    )
    parts = [f"{title} {when}, reported {filed:%b %d, %Y}."]
    if transaction.trades > 1:
        parts.append(f"Filed as {transaction.trades} separate transactions.")
    if transaction.value:
        parts.append(f"Value about ${transaction.value:,.0f}.")
    if transaction.owned_after is not None:
        parts.append(f"Holds {transaction.owned_after:,.0f} shares afterwards.")
    if transaction.under_plan:
        parts.append("Made under a pre-arranged trading plan (Rule 10b5-1).")
    return title, " ".join(parts)


class InsiderFilingService:
    def __init__(self, session: AsyncSession, fetch: Fetch | None = None):
        self.session = session
        self.maps = BusinessMapService(session, fetch=fetch)

    async def followed(self) -> list[tuple[Entity, str]]:
        """Companies held or watched, largest positions first."""
        rows = (
            await self.session.execute(
                select(Entity, Security.ticker)
                .join(Security, Security.entity_id == Entity.id)
                .join(Position, Position.security_id == Security.id)
                .where(Position.list_type.in_(["holding", "watchlist", "considering"]))
                .order_by(desc(Position.market_value))
            )
        ).all()
        seen, ordered = set(), []
        for entity, ticker in rows:
            if ticker and entity.id not in seen:
                seen.add(entity.id)
                ordered.append((entity, ticker))
        return ordered

    async def refresh(self, *, now: datetime | None = None) -> dict[str, int]:
        now = now or datetime.now(UTC)
        state = self.maps._read_state("insider_filing_checks.json")
        ciks = await self.maps.ticker_ciks()
        counts = {"companies": 0, "filings": 0, "transactions": 0}
        signals = OwnershipSignalService(self.session)
        for entity, ticker in await self.followed():
            cik = ciks.get(ticker.upper())
            if cik is None:
                continue
            counts["companies"] += 1
            since = (
                state.get(str(entity.id)) or (now - FIRST_LOOKBACK).date().isoformat()
            )
            submissions = json.loads(
                await self.maps._get(SUBMISSIONS_URL.format(cik=cik))
            )
            recent: dict[str, Any] = submissions.get("filings", {}).get("recent", {})
            newest = since
            for index, form in enumerate(recent.get("form", [])):
                filed = recent["filingDate"][index]
                if form != "4" or filed <= since:
                    continue
                newest = max(newest, filed)
                accession = recent["accessionNumber"][index]
                folder = ARCHIVE_URL.format(
                    cik=cik, accession=accession.replace("-", "")
                )
                document = recent["primaryDocument"][index].split("/")[-1]
                url = f"{folder}/{document}"
                transactions = parse_form4(await self.maps._get(url))
                counts["filings"] += 1
                for transaction in combine(transactions):
                    title, summary = describe(
                        transaction, ticker, date.fromisoformat(filed)
                    )
                    await signals.ingest_disclosure(
                        source_name=SOURCE_NAME,
                        source_type="filing",
                        source_url="https://www.sec.gov/",
                        source_description="Insider trades reported to the SEC on Form 4 by officers, directors and 10% owners.",
                        source_item_type="insider_disclosure",
                        title=title,
                        url=url,
                        external_id=f"{accession}:{transaction.code}",
                        author=transaction.owner,
                        metadata={
                            "ticker": ticker,
                            "issuer": entity.name,
                            "owner": transaction.owner,
                            "role": transaction.role,
                            "transaction_code": transaction.code,
                            "transaction_type": TRANSACTION_TYPES.get(
                                transaction.code, ""
                            )
                            .replace(" {shares}", "")
                            .replace("{shares} ", ""),
                            "shares": transaction.shares,
                            "price": transaction.price,
                            "transaction_value": transaction.value,
                            "acquired": transaction.acquired,
                            "shares_owned_after": transaction.owned_after,
                            "under_10b5_1_plan": transaction.under_plan,
                            "transaction_date": transaction.transaction_date.isoformat(),
                            "last_transaction_date": (
                                transaction.last_date.isoformat()
                                if transaction.last_date
                                else None
                            ),
                            "trades": transaction.trades,
                            "filing_date": filed,
                            "accession": accession,
                        },
                        summary=summary,
                    )
                    counts["transactions"] += 1
            state[str(entity.id)] = newest
        self.maps._write_state("insider_filing_checks.json", state)
        return counts
