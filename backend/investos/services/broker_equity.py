"""Reconcile independently reported USD account equity at each statement time."""

import hashlib
import json
from collections import defaultdict, deque
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import DateTime, String, func, literal, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from investos.models.entity import Security
from investos.models.portfolio import (
    BrokerEquityStatement,
    CashLedgerEntry,
    Position,
    Transaction,
)
from investos.schemas.broker_equity import (
    BrokerEquityHistory,
    BrokerEquityPoint,
    BrokerLedgerMarker,
    BrokerStatementInput,
)
from investos.services.portfolio import PortfolioService

POINT_LIMIT = 180
EVENT_LIMIT = 250
CENT = Decimal("0.01")
SHARE_ACTIONS = {"buy", "sell", "split", "expire"}
CASH_ACTIONS = {"buy", "sell", "deposit", "withdrawal", "dividend"}
INDEPENDENT_CASH_ACTIONS = {"deposit", "withdrawal", "fee", "dividend"}


class StaleStatementPreview(Exception):
    pass


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _amount(value: Decimal) -> str:
    return format(value.normalize(), "f") if value else "0"


class LedgerReplay:
    def __init__(self, rows):
        self.rows = iter(rows)
        self.next = next(self.rows, None)
        self.quantities: dict[UUID, Decimal] = defaultdict(Decimal)
        self.tickers: dict[UUID, str] = {}
        self.cash = Decimal(0)
        self.has_cash = False
        self.missing_cash = False
        self.unsupported = False
        self.proof = hashlib.sha256()

    def _apply(self, row) -> None:
        self.proof.update(
            json.dumps(tuple(map(str, row)), separators=(",", ":")).encode()
        )
        if row.receipt_type == "cash":
            if row.action in INDEPENDENT_CASH_ACTIONS:
                self.cash += row.cash
                self.has_cash = True
            else:
                self.missing_cash = True
            return
        if row.action in SHARE_ACTIONS:
            if (
                row.security_id is None
                or row.direction != "long"
                or not row.quantity
                or row.quantity < 0
            ):
                self.unsupported = True
            else:
                self.tickers[row.security_id] = row.ticker
                quantity = self.quantities[row.security_id]
                if row.action == "buy":
                    quantity += row.quantity
                elif row.action == "sell":
                    quantity -= row.quantity
                elif row.action == "split":
                    quantity *= row.quantity
                else:
                    quantity = Decimal(0)
                self.quantities[row.security_id] = quantity
                self.unsupported |= quantity < 0
        elif row.action not in CASH_ACTIONS:
            self.unsupported = True

        if row.action in CASH_ACTIONS:
            expected_cash = row.price
            if expected_cash is not None and row.action in {"buy", "sell"}:
                expected_cash *= row.quantity
            if expected_cash is not None and row.action in {"buy", "withdrawal"}:
                expected_cash = -expected_cash
            if (
                row.cash_count == 1
                and row.cash_time == row.at
                and expected_cash is not None
                and abs(row.cash - expected_cash) < CENT
            ):
                self.cash += row.cash
                self.has_cash = True
            else:
                # The cash ledger rebuild omits zero-value entries.
                if not (row.cash_count is None and expected_cash == 0):
                    self.missing_cash = True

    def advance(self, as_of: datetime) -> None:
        while self.next is not None and self.next.at <= as_of:
            self._apply(self.next)
            self.next = next(self.rows, None)

    def reconcile(self, as_of: datetime, statement: dict) -> dict:
        self.advance(as_of)
        book = [
            {"ticker": self.tickers[security], "quantity": quantity}
            for security, quantity in self.quantities.items()
            if quantity
        ]
        differences = [
            {
                key: _amount(value) if isinstance(value, Decimal) else value
                for key, value in row.items()
            }
            for row in PortfolioService.diff_positions(book, statement["holdings"])
        ]
        reasons = []
        if self.unsupported:
            reasons.append(
                "Holdings history includes an unsupported or incomplete transaction."
            )
        if len({row["ticker"] for row in book}) != len(book):
            reasons.append(
                "Multiple held instruments share a ticker. The statement cannot distinguish them."
            )
        if differences:
            reasons.append("Statement holdings differ from the ledger at this time.")
        if statement["cash"] is None:
            reasons.append("The statement has no cash balance.")
        if not self.has_cash or self.missing_cash:
            reasons.append("The ledger has incomplete cash activity through this time.")
        elif statement["cash"] is not None and self.cash.quantize(CENT) != Decimal(
            statement["cash"]
        ):
            reasons.append(
                "The statement cash balance differs from the ledger at this time."
            )
        return {
            "reasons": reasons,
            "holdings_differences": differences,
            "ledger_cash": (
                _amount(self.cash.quantize(CENT))
                if self.has_cash and not self.missing_cash
                else None
            ),
            "ledger_hash": self.proof.hexdigest(),
        }


class BrokerEquityService:
    def __init__(self, session: AsyncSession):
        self.session = session

    @staticmethod
    def normalize(payload: BrokerStatementInput) -> dict:
        parsed = PortfolioService.parse_holdings_snapshot(payload.holdings_text)
        cash = payload.cash
        if parsed["cash"] is not None:
            if cash is not None and cash != parsed["cash"]:
                raise ValueError(
                    "The cash field disagrees with the pasted cash balance"
                )
            cash = parsed["cash"]
        if cash is not None and cash != cash.quantize(CENT):
            raise ValueError("Enter the broker cash balance to the cent")
        if not parsed["holdings"] and not payload.cash_only:
            raise ValueError(
                "Supply all holdings or confirm the statement has no holdings"
            )
        if parsed["holdings"] and payload.cash_only:
            raise ValueError("A cash-only statement cannot include holdings")
        if payload.cash_only and cash is not None and cash != payload.equity:
            raise ValueError(
                "A cash-only statement must report the same equity and cash"
            )
        holdings = {}
        for row in parsed["holdings"]:
            if row["ticker"] in holdings:
                raise ValueError(
                    "Combine duplicate rows for the same ticker before importing"
                )
            holdings[row["ticker"]] = _amount(row["quantity"])
        return {
            "as_of": payload.as_of.isoformat(),
            "currency": payload.currency,
            "equity": _amount(payload.equity),
            "cash": _amount(cash) if cash is not None else None,
            "holdings": [
                {"ticker": ticker, "quantity": quantity}
                for ticker, quantity in sorted(holdings.items())
            ],
        }

    async def ledger(self, until: datetime):
        cash = (
            select(
                CashLedgerEntry.transaction_id,
                func.count().label("count"),
                func.sum(CashLedgerEntry.amount).label("amount"),
                func.max(CashLedgerEntry.executed_at).label("at"),
            )
            .where(
                CashLedgerEntry.transaction_id.is_not(None),
                CashLedgerEntry.executed_at <= until,
            )
            .group_by(CashLedgerEntry.transaction_id)
            .subquery()
        )
        transactions = (
            select(
                Transaction.id,
                Transaction.executed_at.label("at"),
                Transaction.action,
                Position.security_id,
                Security.ticker,
                Position.direction,
                Transaction.quantity,
                Transaction.price,
                cash.c.amount.label("cash"),
                cash.c.count.label("cash_count"),
                cash.c.at.label("cash_time"),
                literal("transaction").label("receipt_type"),
            )
            .outerjoin(Position, Position.id == Transaction.position_id)
            .outerjoin(Security, Security.id == Position.security_id)
            .outerjoin(cash, cash.c.transaction_id == Transaction.id)
            .where(
                PortfolioService.active_transaction_clause(),
                Transaction.superseded_by_id.is_(None),
                Transaction.executed_at <= until,
            )
        )
        independent_cash = select(
            CashLedgerEntry.id,
            CashLedgerEntry.executed_at,
            CashLedgerEntry.entry_type,
            literal(None, type_=Position.__table__.c.security_id.type),
            literal(None, String),
            literal(None, String),
            literal(None, type_=Transaction.__table__.c.quantity.type),
            literal(None, type_=Transaction.__table__.c.price.type),
            CashLedgerEntry.amount,
            literal(1),
            literal(None, DateTime(timezone=True)),
            literal("cash"),
        ).where(
            CashLedgerEntry.transaction_id.is_(None),
            CashLedgerEntry.executed_at <= until,
        )
        return (
            await self.session.execute(
                transactions.union_all(independent_cash).order_by("at", "id")
            )
        ).all()

    async def preview(self, payload: BrokerStatementInput) -> dict:
        statement = self.normalize(payload)
        replay = LedgerReplay(await self.ledger(payload.as_of))
        check = replay.reconcile(payload.as_of, statement)
        return {
            "statement": statement,
            "reconciliation": check,
            "preview_token": _digest([statement, check]),
        }

    async def save(self, payload: BrokerStatementInput, preview_token: str) -> UUID:
        preview = await self.preview(payload)
        if preview["preview_token"] != preview_token:
            raise StaleStatementPreview(
                "The statement or ledger changed. Preview it again before saving."
            )
        statement_hash = _digest(preview["statement"])
        inserted = await self.session.scalar(
            insert(BrokerEquityStatement)
            .values(
                as_of=payload.as_of,
                statement_hash=statement_hash,
                statement=preview["statement"],
                reconciliation=preview["reconciliation"],
            )
            .on_conflict_do_nothing(
                index_elements=[BrokerEquityStatement.statement_hash]
            )
            .returning(BrokerEquityStatement.id)
        )
        if inserted is None:
            inserted = await self.session.scalar(
                select(BrokerEquityStatement.id).where(
                    BrokerEquityStatement.statement_hash == statement_hash
                )
            )
        assert inserted is not None
        await self.session.commit()
        return inserted

    async def history(self) -> BrokerEquityHistory:
        statements = (
            await self.session.scalars(
                select(BrokerEquityStatement)
                .distinct(BrokerEquityStatement.as_of)
                .order_by(
                    BrokerEquityStatement.as_of.desc(),
                    BrokerEquityStatement.recorded_at.desc(),
                    BrokerEquityStatement.id.desc(),
                )
                .limit(POINT_LIMIT)
            )
        ).all()
        if not statements:
            return BrokerEquityHistory(points=[], events=[], older_events_omitted=False)
        rows = await self.ledger(statements[0].as_of)
        replay = LedgerReplay(rows)
        points = []
        for receipt in reversed(statements):
            check = replay.reconcile(receipt.as_of, receipt.statement)
            points.append(
                BrokerEquityPoint(
                    id=receipt.id,
                    as_of=receipt.as_of,
                    value=(
                        None
                        if check["reasons"]
                        else Decimal(receipt.statement["equity"])
                    ),
                    reasons=check["reasons"],
                )
            )
        events: deque = deque(maxlen=EVENT_LIMIT)
        count = 0
        for row in rows:
            if (
                row.at < points[0].as_of
                or row.action not in SHARE_ACTIONS | INDEPENDENT_CASH_ACTIONS
            ):
                continue
            events.append(
                BrokerLedgerMarker(
                    id=row.id,
                    at=row.at,
                    action=row.action,
                    ticker=row.ticker,
                    quantity=row.quantity if row.action in SHARE_ACTIONS else None,
                    cash=row.cash,
                    receipt_type=row.receipt_type,
                )
            )
            count += 1
        return BrokerEquityHistory(
            points=points, events=list(events), older_events_omitted=count > EVENT_LIMIT
        )
