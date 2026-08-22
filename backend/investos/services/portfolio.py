import csv
import io
import re
from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import desc, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased, selectinload

from investos.core.llm import call_llm_json
from investos.models.coverage import (
    CoverageMap,
    MissingEvidenceClass,
    UnresolvedQuestion,
)
from investos.models.entity import Entity, Security
from investos.models.portfolio import CashLedgerEntry, Lot, Position, Transaction
from investos.models.profile import Profile
from investos.models.review import ReviewQueueItem
from investos.schemas.portfolio import (
    PortfolioBuildPoint,
    PortfolioImportResponse,
    PortfolioOverviewResponse,
    PortfolioSimpleImportRequest,
    ResearchObjectCreate,
    ResearchObjectResponse,
    TransactionCorrectionRequest,
    TransactionCorrectionResponse,
    TransactionCreate,
)
from investos.services.canonical_state import CanonicalStateService
from investos.services.runtime_settings import RuntimeSettingsStore
from investos.services.transaction_provenance import transaction_source_summary

IMPORT_NORMALIZATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "format_summary": {"type": "string"},
        "rows": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "row_number": {"type": "integer"},
                    "keep": {"type": "boolean"},
                    "skip_reason": {"type": ["string", "null"]},
                    "ticker": {"type": ["string", "null"]},
                    "entity_name": {"type": ["string", "null"]},
                    "action": {
                        "type": ["string", "null"],
                        "enum": [
                            "buy",
                            "sell",
                            "dividend",
                            "deposit",
                            "withdrawal",
                            None,
                        ],
                    },
                    "quantity": {"type": ["number", "null"]},
                    "price": {"type": ["number", "null"]},
                    "executed_at": {"type": ["string", "null"]},
                    "notes": {"type": ["string", "null"]},
                    "list_type": {"type": ["string", "null"]},
                    "direction": {"type": ["string", "null"]},
                    "confidence": {"type": ["number", "null"]},
                },
                "required": [
                    "row_number",
                    "keep",
                    "skip_reason",
                    "ticker",
                    "entity_name",
                    "action",
                    "quantity",
                    "price",
                    "executed_at",
                    "notes",
                    "list_type",
                    "direction",
                    "confidence",
                ],
                "additionalProperties": False,
            },
        },
    },
    "required": ["format_summary", "rows"],
    "additionalProperties": False,
}


def _to_decimal(value: float | int | Decimal | None) -> Decimal:
    if value is None:
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


class PortfolioService:
    def __init__(self, session: AsyncSession):
        self.session = session

    @staticmethod
    def active_transaction_clause():
        return or_(Transaction.status == "settled", Transaction.status.is_(None))

    @staticmethod
    def transaction_is_active(txn: Transaction) -> bool:
        return getattr(txn, "status", None) in {None, "settled"}

    async def get_position(self, position_id: UUID) -> Position | None:
        stmt = (
            select(Position)
            .where(Position.id == position_id)
            .options(
                selectinload(Position.security).selectinload(Security.entity),
                selectinload(Position.lots),
                selectinload(Position.transactions),
            )
        )
        position = (await self.session.execute(stmt)).scalar_one_or_none()
        if position:
            self._decorate_position(position)
        return position

    async def list_positions(self, list_type: str = "holding") -> list[Position]:
        stmt = select(Position).options(
            selectinload(Position.security).selectinload(Security.entity),
            selectinload(Position.lots),
            selectinload(Position.transactions),
        )
        if list_type != "all":
            stmt = stmt.where(Position.list_type == list_type)
        positions = list((await self.session.execute(stmt)).scalars().all())
        for position in positions:
            self._decorate_position(position)
        return positions

    async def overview(self) -> PortfolioOverviewResponse:
        holdings = await self.list_positions("holding")
        watchlist = await self.list_positions("watchlist")
        considering = await self.list_positions("considering")
        recent_rows = (
            await self.session.execute(
                select(Transaction, Security, Entity)
                .outerjoin(Position, Transaction.position_id == Position.id)
                .outerjoin(Security, Position.security_id == Security.id)
                .outerjoin(Entity, Security.entity_id == Entity.id)
                .where(self.active_transaction_clause())
                .order_by(desc(Transaction.executed_at))
                .limit(12)
            )
        ).all()
        recent_transactions = []
        for txn, security, entity in recent_rows:
            self._decorate_transaction(txn, security=security, entity=entity)
            recent_transactions.append(txn)

        # Calculate rankings by Total PnL (Unrealized + Realized)
        sorted_by_pnl = sorted(
            holdings,
            key=lambda x: _to_decimal(getattr(x, "unrealized_pnl", 0.0))
            + _to_decimal(getattr(x, "realized_pnl", 0.0)),
            reverse=True,
        )
        top_winners = sorted_by_pnl[:5]  # Increasing to 5 for better overview
        top_losers = sorted_by_pnl[-5:] if len(sorted_by_pnl) > 5 else []

        # Get current buying power
        latest_ledger = (
            await self.session.execute(
                select(CashLedgerEntry)
                .order_by(
                    desc(CashLedgerEntry.executed_at), desc(CashLedgerEntry.created_at)
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if latest_ledger is not None:
            buying_power = float(latest_ledger.balance_after)
        else:
            buying_power = float(
                RuntimeSettingsStore.load().portfolio.remaining_buying_power or 0.0
            )

        return PortfolioOverviewResponse(
            holdings=holdings,
            watchlist=watchlist,
            considering=considering,
            recent_transactions=recent_transactions,
            top_winners=top_winners,
            top_losers=top_losers,
            total_value=sum((float(h.market_value or 0.0) for h in holdings), 0.0)
            + buying_power,
            buying_power=buying_power,
            build_series=await self.build_series(),
        )

    async def build_series(self) -> list[PortfolioBuildPoint]:
        return await self._build_series()

    def _decorate_position(self, position: Position) -> None:
        security = getattr(position, "security", None)
        setattr(position, "ticker", getattr(security, "ticker", None))
        setattr(
            position,
            "entity_name",
            getattr(getattr(security, "entity", None), "name", None),
        )
        # Calculate aggregate realized PnL across all lots for this position
        total_realized = sum(
            (_to_decimal(lot.realized_pnl) for lot in position.lots), Decimal("0")
        )
        setattr(position, "realized_pnl", float(total_realized))

    def _decorate_transaction(
        self,
        txn: Transaction,
        *,
        security: Security | None = None,
        entity: Entity | None = None,
    ) -> Transaction:
        setattr(
            txn,
            "ticker",
            getattr(security, "ticker", None)
            or ("CASH" if txn.position_id is None else None),
        )
        setattr(txn, "entity_name", getattr(entity, "name", None))
        for key, value in transaction_source_summary(txn).items():
            setattr(txn, key, value)
        return txn

    async def add_transaction(
        self, position_id: UUID | None, txn_data: TransactionCreate
    ) -> Transaction:
        if position_id is not None:
            position = await self.get_position(position_id)
            if not position:
                raise ValueError(f"Position {position_id} not found")
        else:
            position = None

        txn = Transaction(
            position_id=position_id,
            action=txn_data.action,
            quantity=txn_data.quantity,
            price=txn_data.price,
            executed_at=txn_data.executed_at,
            notes=txn_data.notes,
            provenance_json=txn_data.provenance_json,
        )
        self.session.add(txn)
        await self.session.flush()

        if txn.action in {"buy", "sell"} and txn.price is None:
            raise ValueError("Buy and sell transactions require a price")

        if txn.action in {"buy", "sell"}:
            if not position:
                raise ValueError("Buy and sell transactions require a position")
            current_qty = _to_decimal(position.quantity)
            avg_cost = _to_decimal(position.avg_cost_basis)
            trade_qty = _to_decimal(txn.quantity)
            trade_price = _to_decimal(txn.price)

            if txn.action == "buy":
                await self._record_cash_event(
                    entry_type="trade_settlement",
                    amount=-float(trade_qty * trade_price),
                    transaction_id=txn.id,
                    description=f"Bought {trade_qty} {getattr(position.security, 'ticker', 'shares')} @ {trade_price}",
                    executed_at=txn.executed_at,
                )
                new_lot = Lot(
                    position_id=position_id,
                    quantity=trade_qty,
                    cost_basis=trade_price,
                    acquired_at=txn.executed_at,
                    lot_type=txn_data.lot_type,
                )
                self.session.add(new_lot)

                total_cost = (current_qty * avg_cost) + (trade_qty * trade_price)
                new_qty = current_qty + trade_qty
                position.quantity = new_qty
                position.avg_cost_basis = (
                    total_cost / new_qty if new_qty > 0 else Decimal("0")
                )
                position.current_price = trade_price
                # Re-buying a name that was previously sold to zero reactivates it.
                # A prior sell flips the row to "closed"; without this it would
                # keep nonzero shares yet stay hidden from the holdings list.
                if new_qty > 0 and position.list_type == "closed":
                    position.list_type = "holding"

            elif txn.action == "sell":
                await self._record_cash_event(
                    entry_type="trade_settlement",
                    amount=float(trade_qty * trade_price),
                    transaction_id=txn.id,
                    description=f"Sold {trade_qty} {getattr(position.security, 'ticker', 'shares')} @ {trade_price}",
                    executed_at=txn.executed_at,
                )
                open_lots = sorted(
                    [
                        lot
                        for lot in position.lots
                        if lot.closed_at is None and _to_decimal(lot.quantity) > 0
                    ],
                    key=lambda lot: lot.acquired_at,
                )
                available_qty = sum(
                    (_to_decimal(lot.quantity) for lot in open_lots), Decimal("0")
                )
                if trade_qty > available_qty and position.direction != "short":
                    # Bypass ValueError for automated imports to allow temporary out-of-order lot states
                    # during batch processing/syncs. These will be corrected during the final recalculation.
                    if txn_data.lot_type not in {
                        "broker_confirmation",
                        "csv_import",
                        "rebuild",
                    }:
                        raise ValueError("Sell quantity exceeds available open lots")

                qty_to_sell = trade_qty
                for lot in open_lots:
                    if qty_to_sell <= 0:
                        break

                    lot_qty = _to_decimal(lot.quantity)
                    sell_qty = min(lot_qty, qty_to_sell)
                    realized_pnl = (
                        trade_price - _to_decimal(lot.cost_basis)
                    ) * sell_qty

                    lot.quantity -= sell_qty
                    lot.realized_pnl += realized_pnl
                    qty_to_sell -= sell_qty

                    if _to_decimal(lot.quantity) <= Decimal("0"):
                        lot.quantity = Decimal("0")
                        lot.closed_at = txn.executed_at

                position.quantity = current_qty - trade_qty
                position.current_price = trade_price
                if _to_decimal(position.quantity) <= Decimal("0"):
                    position.quantity = Decimal("0")
                    position.avg_cost_basis = Decimal("0")
                    position.list_type = "closed"

        elif txn.action == "dividend" and txn.price:
            if not position:
                raise ValueError("Dividend transactions require a position")
            await self._record_cash_event(
                entry_type="dividend",
                amount=float(txn.price),
                transaction_id=txn.id,
                description=f"Dividend for {getattr(position.security, 'ticker', 'shares')}",
                executed_at=txn.executed_at,
            )
        elif txn.action == "deposit" and txn.price:
            await self._record_cash_event(
                entry_type="deposit",
                amount=float(txn.price),
                transaction_id=txn.id,
                description=txn.notes or "Deposit",
                executed_at=txn.executed_at,
            )
        elif txn.action == "withdrawal" and txn.price:
            await self._record_cash_event(
                entry_type="withdrawal",
                amount=-float(txn.price),
                transaction_id=txn.id,
                description=txn.notes or "Withdrawal",
                executed_at=txn.executed_at,
            )

        # Only trades set the mark. A dividend/deposit/split amount must never
        # be mistaken for the security's price.
        if txn.action in {"buy", "sell"} and txn.price is not None and position:
            trade_price = _to_decimal(txn.price)
            position.current_price = trade_price

        if position:
            position.market_value = _to_decimal(position.quantity) * _to_decimal(
                position.current_price
            )
            position.unrealized_pnl = _to_decimal(position.quantity) * (
                _to_decimal(position.current_price)
                - _to_decimal(position.avg_cost_basis)
            )

        await self.session.commit()
        await self.session.refresh(txn)
        return txn

    async def _record_cash_event(
        self,
        *,
        entry_type: str,
        amount: float,
        transaction_id: UUID | None = None,
        description: str | None = None,
        executed_at: datetime,
    ) -> CashLedgerEntry:
        latest = (
            await self.session.execute(
                select(CashLedgerEntry)
                .order_by(
                    desc(CashLedgerEntry.executed_at), desc(CashLedgerEntry.created_at)
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        current_balance = float(latest.balance_after) if latest else 0.0
        new_balance = current_balance + amount

        entry = CashLedgerEntry(
            entry_type=entry_type,
            amount=amount,
            balance_after=new_balance,
            transaction_id=transaction_id,
            description=description,
            executed_at=executed_at,
        )
        self.session.add(entry)
        return entry

    async def add_transaction_by_ticker(
        self,
        ticker: str,
        txn_data: TransactionCreate,
        list_type: str = "holding",
        direction: str = "long",
        entity_name: str | None = None,
    ) -> Transaction:
        # Standardize deposit/withdrawal ticker CASH or actions
        if txn_data.action in {"deposit", "withdrawal"} or ticker == "CASH":
            action = txn_data.action
            if action not in {"deposit", "withdrawal"}:
                if txn_data.quantity < 0 or (txn_data.price and txn_data.price < 0):
                    action = "withdrawal"
                else:
                    action = "deposit"
            txn_data.action = action
            return await self.add_transaction(None, txn_data)

        security = await self._get_or_create_security_for_ticker(
            ticker=ticker, entity_name=entity_name
        )

        pos_stmt = select(Position).where(
            Position.security_id == security.id,
            Position.list_type == list_type,
        )
        position = (await self.session.execute(pos_stmt)).scalar_one_or_none()

        if not position:
            position = Position(
                security_id=security.id,
                direction=direction,
                list_type=list_type,
            )
            self.session.add(position)
            await self.session.flush()

        return await self.add_transaction(position.id, txn_data)

    async def correct_transaction(
        self,
        transaction_id: UUID,
        payload: TransactionCorrectionRequest,
    ) -> Transaction:
        original = (
            await self.session.execute(
                select(Transaction).where(Transaction.id == transaction_id)
            )
        ).scalar_one_or_none()
        if original is None:
            raise ValueError("Transaction not found")
        if not self.transaction_is_active(original):
            raise ValueError("Only active settled transactions can be corrected")

        action = payload.action or original.action
        quantity = original.quantity if payload.quantity is None else payload.quantity
        price = original.price if payload.price is None else payload.price
        executed_at = payload.executed_at or original.executed_at
        notes = payload.notes if payload.notes is not None else original.notes
        lot_type = payload.lot_type or "manual_correction"

        if action in {"buy", "sell"} and price is None:
            raise ValueError("Corrected buy and sell transactions require a price")
        if action in {"buy", "sell"} and original.position_id is None:
            raise ValueError("Corrected buy and sell transactions require a position")

        provenance = {
            "source_type": "manual_correction",
            "source_label": "Manual correction",
            "corrects_transaction_id": str(original.id),
            "reason": (payload.reason or "").strip() or None,
            "corrected_at": datetime.now(UTC).isoformat(),
            "lot_type": lot_type,
            "original_source_type": (
                original.provenance_json.get("source_type")
                if isinstance(original.provenance_json, dict)
                else None
            ),
            "original_source_label": (
                original.provenance_json.get("source_label")
                if isinstance(original.provenance_json, dict)
                else None
            ),
        }
        provenance = {
            key: value for key, value in provenance.items() if value is not None
        }

        replacement = Transaction(
            position_id=original.position_id,
            action=action,
            quantity=quantity,
            price=price,
            executed_at=executed_at,
            notes=notes,
            status="settled",
            provenance_json=provenance,
        )
        self.session.add(replacement)
        await self.session.flush()

        original.status = "corrected"
        original.superseded_by_id = replacement.id
        if isinstance(original.provenance_json, dict):
            original.provenance_json = {
                **original.provenance_json,
                "corrected_by_transaction_id": str(replacement.id),
                "correction_reason": (payload.reason or "").strip() or None,
            }
        else:
            original.provenance_json = {
                "corrected_by_transaction_id": str(replacement.id),
                "correction_reason": (payload.reason or "").strip() or None,
            }
        original.provenance_json = {
            key: value
            for key, value in original.provenance_json.items()
            if value is not None
        }

        await self.recalculate_all_positions()
        await self.session.refresh(replacement)
        self._decorate_transaction(replacement)
        return replacement

    async def transaction_corrections(
        self, *, limit: int = 50
    ) -> list[TransactionCorrectionResponse]:
        replacement_txn = aliased(Transaction)
        rows = (
            await self.session.execute(
                select(Transaction, replacement_txn, Security, Entity)
                .join(
                    replacement_txn, Transaction.superseded_by_id == replacement_txn.id
                )
                .outerjoin(Position, Transaction.position_id == Position.id)
                .outerjoin(Security, Position.security_id == Security.id)
                .outerjoin(Entity, Security.entity_id == Entity.id)
                .where(Transaction.status == "corrected")
                .order_by(desc(Transaction.executed_at))
                .limit(max(1, min(limit, 200)))
            )
        ).all()
        corrections: list[TransactionCorrectionResponse] = []
        for original, replacement, security, entity in rows:
            self._decorate_transaction(original, security=security, entity=entity)
            self._decorate_transaction(replacement, security=security, entity=entity)
            original_provenance = (
                original.provenance_json
                if isinstance(original.provenance_json, dict)
                else {}
            )
            replacement_provenance = (
                replacement.provenance_json
                if isinstance(replacement.provenance_json, dict)
                else {}
            )
            corrected_at = None
            raw_corrected_at = replacement_provenance.get("corrected_at")
            if raw_corrected_at:
                try:
                    corrected_at = datetime.fromisoformat(str(raw_corrected_at))
                except ValueError:
                    corrected_at = None
            corrections.append(
                TransactionCorrectionResponse(
                    original=original,
                    replacement=replacement,
                    reason=(
                        replacement_provenance.get("reason")
                        or original_provenance.get("correction_reason")
                    ),
                    corrected_at=corrected_at,
                )
            )
        return corrections

    async def create_research_object(
        self,
        payload: ResearchObjectCreate,
    ) -> ResearchObjectResponse:
        list_type = payload.list_type.strip().lower() or "watchlist"
        if list_type not in {"watchlist", "considering", "theme_basket"}:
            raise ValueError(
                "Research object list_type must be watchlist, considering, or theme_basket."
            )

        security = await self._get_or_create_security_for_ticker(
            ticker=payload.ticker,
            entity_name=payload.entity_name,
        )

        position = (
            await self.session.execute(
                select(Position).where(
                    Position.security_id == security.id,
                    Position.list_type == list_type,
                )
            )
        ).scalar_one_or_none()
        if position is None:
            position = Position(
                security_id=security.id,
                direction=payload.direction,
                list_type=list_type,
                conviction=payload.conviction,
            )
            self.session.add(position)
            await self.session.flush()
        else:
            if payload.conviction is not None:
                position.conviction = payload.conviction

        profile = (
            await self.session.execute(
                select(Profile).where(
                    Profile.subject_type == "entity",
                    Profile.subject_id == security.entity_id,
                )
            )
        ).scalar_one_or_none()
        if profile is None:
            profile = Profile(
                subject_type="entity",
                subject_id=security.entity_id,
            )
            self.session.add(profile)
            await self.session.flush()

        if payload.summary and not profile.executive_summary:
            profile.executive_summary = payload.summary
        if payload.bull_case and not profile.bull_case:
            profile.bull_case = payload.bull_case
        if payload.bear_case and not profile.bear_case:
            profile.bear_case = payload.bear_case

        coverage = await CanonicalStateService(self.session).ensure_coverage_map(
            subject_type="entity",
            subject_id=security.entity_id,
            create=lambda: CoverageMap(
                subject_type="entity",
                subject_id=security.entity_id,
                total_evidence_count=0,
                high_tier_evidence_count=0,
                contradiction_count=0,
                unresolved_contradiction_count=0,
                overall_coverage_score=0.0,
                evidence_class_coverage_json={
                    "trusted_source_coverage": False,
                    "official_source_coverage": False,
                    "benchmark_coverage": False,
                    "macro_coverage": False,
                    "peer_coverage": False,
                    "historical_coverage": False,
                    "lesson_coverage": False,
                },
            ),
        )

        existing_missing = list(
            (
                await self.session.execute(
                    select(MissingEvidenceClass).where(
                        MissingEvidenceClass.coverage_map_id == coverage.id
                    )
                )
            )
            .scalars()
            .all()
        )
        if not existing_missing:
            for class_name, importance in (
                ("official_source_coverage", "high"),
                ("benchmark_coverage", "medium"),
                ("contradiction_coverage", "high"),
                ("peer_coverage", "medium"),
            ):
                self.session.add(
                    MissingEvidenceClass(
                        coverage_map_id=coverage.id,
                        class_name=class_name,
                        importance_to_thesis=importance,
                    )
                )

        existing_questions = list(
            (
                await self.session.execute(
                    select(UnresolvedQuestion).where(
                        UnresolvedQuestion.coverage_map_id == coverage.id,
                        UnresolvedQuestion.status == "open",
                    )
                )
            )
            .scalars()
            .all()
        )
        if not existing_questions:
            seeded_questions = payload.open_questions or [
                f"What is the core thesis for {security.ticker} and what would falsify it?",
                f"What evidence would make {security.ticker} more attractive versus current holdings?",
                f"What benchmark, peer, or macro confounders matter most for {security.ticker}?",
            ]
            for index, question_text in enumerate(seeded_questions[:5]):
                self.session.add(
                    UnresolvedQuestion(
                        coverage_map_id=coverage.id,
                        question_text=question_text.strip(),
                        urgency=5 if index == 0 else 3,
                        status="open",
                    )
                )

        await self.session.commit()
        return ResearchObjectResponse(
            position_id=position.id,
            profile_id=profile.id,
            coverage_map_id=coverage.id,
            ticker=security.ticker,
            entity_name=security.entity.name,
            list_type=position.list_type,
            open_question_count=min(
                len(payload.open_questions) if payload.open_questions else 3, 5
            ),
        )

    async def import_transactions_csv(
        self, csv_content: str
    ) -> PortfolioImportResponse:
        reader = csv.DictReader(io.StringIO(csv_content))
        imported_count = 0
        skipped_count = 0
        errors: list[str] = []
        inferred_remaining_buying_power: float | None = None
        inference_note: str | None = None
        had_existing_positions = (
            await self.session.execute(select(Position.id).limit(1))
        ).scalar_one_or_none() is not None
        had_existing_transactions = (
            await self.session.execute(select(Transaction.id).limit(1))
        ).scalar_one_or_none() is not None
        had_existing_ledger = (
            await self.session.execute(select(CashLedgerEntry.id).limit(1))
        ).scalar_one_or_none() is not None

        required_columns = {"ticker", "action", "quantity", "executed_at"}
        robinhood_columns = {
            "Activity Date",
            "Instrument",
            "Description",
            "Trans Code",
            "Quantity",
            "Price",
            "Amount",
        }
        normalized_headers = {name.strip() for name in reader.fieldnames or []}
        if not reader.fieldnames:
            raise ValueError("CSV must include a header row.")

        if required_columns.issubset(normalized_headers):
            parsed_rows = self._parse_normalized_csv_rows(reader)
        elif robinhood_columns.issubset(normalized_headers):
            parsed_rows = self._parse_robinhood_activity_rows(reader)
        else:
            parsed_rows = await self._parse_llm_csv_rows(csv_content)

        imported_cash_events = any(
            row.get("action") in {"deposit", "withdrawal"} and not row.get("invalid")
            for row in parsed_rows
        )

        for row in parsed_rows:
            try:
                if row.get("invalid"):
                    skipped_count += 1
                    errors.append(f"Row {row['row_number']}: {row['invalid']}")
                    continue

                # Handle direct cash entries
                if row.get("action") in {"deposit", "withdrawal"}:
                    txn = Transaction(
                        position_id=None,
                        action=str(row["action"]),
                        quantity=1.0,
                        price=float(row["price"] if row["price"] is not None else 0.0),
                        executed_at=row["executed_at"],
                        notes=str(row["notes"] or "cash_transfer"),
                    )
                    self.session.add(txn)
                    imported_count += 1
                    continue

                if await self._matching_transaction_exists(
                    ticker=str(row["ticker"]),
                    action=str(row["action"]),
                    quantity=float(row["quantity"]),
                    price=float(row["price"]) if row["price"] is not None else None,
                    executed_at=row["executed_at"],
                    notes=str(row["notes"]) if row["notes"] is not None else None,
                ):
                    skipped_count += 1
                    errors.append(
                        f"Row {row['row_number']}: matching transaction already exists."
                    )
                    continue
                await self.add_transaction_by_ticker(
                    ticker=str(row["ticker"]),
                    txn_data=TransactionCreate(
                        action=str(row["action"]),
                        quantity=float(row["quantity"]),
                        price=float(row["price"]) if row["price"] is not None else None,
                        executed_at=row["executed_at"],
                        notes=str(row["notes"]) if row["notes"] is not None else None,
                        lot_type="csv_import",
                    ),
                    list_type=str(row["list_type"]),
                    direction=str(row["direction"]),
                    entity_name=(
                        str(row["entity_name"]) if row.get("entity_name") else None
                    ),
                )
                imported_count += 1
            except Exception as exc:
                skipped_count += 1
                errors.append(f"Row {row['row_number']}: {exc}")

        if (
            imported_count > 0
            and not had_existing_positions
            and not had_existing_transactions
            and not had_existing_ledger
            and not imported_cash_events
        ):
            target_buying_power = float(
                RuntimeSettingsStore.load().portfolio.remaining_buying_power or 0.0
            )
            opening_cash = self._infer_opening_cash_for_transaction_history(
                parsed_rows=parsed_rows,
                target_buying_power=target_buying_power,
            )
            earliest_imported_at = min(
                (row["executed_at"] for row in parsed_rows if not row.get("invalid")),
                default=datetime.now(UTC),
            )
            if opening_cash > 0:
                self.session.add(
                    Transaction(
                        position_id=None,
                        action="deposit",
                        quantity=1.0,
                        price=opening_cash,
                        executed_at=earliest_imported_at.replace(microsecond=0),
                        notes="initial_transaction_history_seed",
                    )
                )
                await self.session.commit()
                inferred_remaining_buying_power = round(target_buying_power, 2)
                inference_note = (
                    "Inferred opening cash for the first transaction-history import because "
                    "the source file did not include funding transfers."
                )

        await self.recalculate_cash_ledger()

        return PortfolioImportResponse(
            imported_count=imported_count,
            skipped_count=skipped_count,
            errors=errors,
            inferred_remaining_buying_power=(
                None
                if inferred_remaining_buying_power is None
                else round(inferred_remaining_buying_power, 2)
            ),
            inference_note=inference_note,
        )

    def _infer_opening_cash_for_transaction_history(
        self,
        *,
        parsed_rows: list[dict[str, Any]],
        target_buying_power: float,
    ) -> float:
        running_balance = 0.0
        min_balance = 0.0
        valid_rows = [row for row in parsed_rows if not row.get("invalid")]
        valid_rows.sort(key=lambda row: (row["executed_at"], row["row_number"]))

        for row in valid_rows:
            action = str(row.get("action") or "").lower()
            quantity = float(row.get("quantity") or 0.0)
            price = float(row.get("price") or 0.0)

            if action == "buy":
                running_balance -= quantity * price
            elif action == "sell":
                running_balance += quantity * price
            elif action == "dividend":
                running_balance += price
            elif action == "deposit":
                running_balance += price
            elif action == "withdrawal":
                running_balance -= price

            min_balance = min(min_balance, running_balance)

        required_for_target_ending_balance = (
            float(target_buying_power) - running_balance
        )
        required_to_avoid_negative_cash = -float(min_balance)
        return round(
            max(
                0.0, required_for_target_ending_balance, required_to_avoid_negative_cash
            ),
            2,
        )

    async def _parse_llm_csv_rows(self, csv_content: str) -> list[dict[str, Any]]:
        payload = await call_llm_json(
            system_prompt=(
                "You normalize brokerage history exports into deterministic Prophet transaction rows. "
                "Keep rows that represent real buy, sell, or cash-dividend history for securities. "
                "Also keep rows for cash deposits and withdrawals (e.g. ACH transfers, wire deposits). "
                "Skip promos, stock lending, rewards, interest, referrals, or generic internal adjustments. "
                "Do not invent values. If a row is ambiguous, set keep=false and explain why. "
                "Output ISO-8601 UTC timestamps. If the source only has a date, use midnight UTC on that date."
            ),
            user_prompt=(
                "Normalize this portfolio-history CSV into transaction rows.\n\n"
                f"{csv_content[:40000]}"
            ),
            schema=IMPORT_NORMALIZATION_SCHEMA,
            timeout_seconds=90,
        )
        rows = payload.get("rows")
        if not isinstance(rows, list):
            raise ValueError("LLM import fallback did not return any rows.")

        parsed_rows: list[dict[str, Any]] = []
        for raw_row in rows:
            if not isinstance(raw_row, dict):
                continue
            row_number = int(raw_row.get("row_number") or 0)
            if not raw_row.get("keep"):
                parsed_rows.append(
                    {
                        "row_number": row_number or 0,
                        "ticker": "",
                        "entity_name": None,
                        "action": "buy",
                        "quantity": 0.0,
                        "price": None,
                        "executed_at": datetime.now(UTC),
                        "notes": None,
                        "list_type": "holding",
                        "direction": "long",
                        "invalid": raw_row.get("skip_reason") or "llm skipped row",
                    }
                )
                continue

            confidence = raw_row.get("confidence")
            if isinstance(confidence, (int, float)) and confidence < 0.6:
                parsed_rows.append(
                    {
                        "row_number": row_number or 0,
                        "ticker": str(raw_row.get("ticker") or "").strip().upper(),
                        "entity_name": str(raw_row.get("entity_name") or "").strip()
                        or None,
                        "action": str(raw_row.get("action") or "buy"),
                        "quantity": 0.0,
                        "price": None,
                        "executed_at": datetime.now(UTC),
                        "notes": None,
                        "list_type": "holding",
                        "direction": "long",
                        "invalid": "low-confidence normalized row",
                    }
                )
                continue

            ticker = str(raw_row.get("ticker") or "").strip().upper()
            action = str(raw_row.get("action") or "").strip().lower()
            executed_at_raw = str(raw_row.get("executed_at") or "").strip()
            if (
                not ticker
                or action not in {"buy", "sell", "dividend"}
                or not executed_at_raw
            ):
                parsed_rows.append(
                    {
                        "row_number": row_number or 0,
                        "ticker": ticker,
                        "entity_name": str(raw_row.get("entity_name") or "").strip()
                        or None,
                        "action": action or "buy",
                        "quantity": 0.0,
                        "price": None,
                        "executed_at": datetime.now(UTC),
                        "notes": None,
                        "list_type": "holding",
                        "direction": "long",
                        "invalid": "missing normalized ticker/action/executed_at",
                    }
                )
                continue

            quantity = float(raw_row.get("quantity") or 0.0)
            price_raw = raw_row.get("price")
            price = float(price_raw) if price_raw is not None else None
            parsed_rows.append(
                {
                    "row_number": row_number or 0,
                    "ticker": ticker,
                    "entity_name": str(raw_row.get("entity_name") or "").strip()
                    or None,
                    "action": action,
                    "quantity": quantity,
                    "price": price,
                    "executed_at": txn_datetime(executed_at_raw),
                    "notes": (
                        str(raw_row.get("notes") or "").strip()
                        or "llm_normalized_import"
                    ),
                    "list_type": str(raw_row.get("list_type") or "holding").strip()
                    or "holding",
                    "direction": str(raw_row.get("direction") or "long").strip()
                    or "long",
                }
            )
        sorted_rows = self._sort_import_rows(parsed_rows)
        if not sorted_rows:
            raise ValueError(
                "LLM import fallback could not normalize any usable transaction rows."
            )
        return sorted_rows

    def _parse_normalized_csv_rows(
        self, reader: csv.DictReader
    ) -> list[dict[str, Any]]:
        parsed_rows: list[dict[str, Any]] = []
        for index, row in enumerate(reader, start=2):
            ticker = (row.get("ticker") or "").strip().upper()
            action = (row.get("action") or "").strip().lower()
            quantity_raw = (row.get("quantity") or "").strip()
            executed_at_raw = (row.get("executed_at") or "").strip()
            if not ticker or not action or not quantity_raw or not executed_at_raw:
                parsed_rows.append(
                    {
                        "row_number": index,
                        "ticker": ticker,
                        "entity_name": (
                            row.get("entity_name") or row.get("name") or ""
                        ).strip()
                        or None,
                        "action": action,
                        "quantity": 0.0,
                        "price": None,
                        "executed_at": datetime.now(UTC),
                        "notes": "missing required values",
                        "list_type": "holding",
                        "direction": "long",
                        "invalid": "missing required values",
                    }
                )
                continue

            price_raw = (row.get("price") or "").strip()
            parsed_rows.append(
                {
                    "row_number": index,
                    "ticker": ticker,
                    "entity_name": (
                        row.get("entity_name") or row.get("name") or ""
                    ).strip()
                    or None,
                    "action": action,
                    "quantity": self._parse_decimal_string(quantity_raw),
                    "price": (
                        self._parse_decimal_string(price_raw) if price_raw else None
                    ),
                    "executed_at": txn_datetime(executed_at_raw),
                    "notes": (row.get("notes") or "").strip() or None,
                    "list_type": (row.get("list_type") or "holding").strip()
                    or "holding",
                    "direction": (row.get("direction") or "long").strip() or "long",
                }
            )
        return self._sort_import_rows(parsed_rows)

    async def _matching_transaction_exists(
        self,
        *,
        ticker: str,
        action: str,
        quantity: float,
        price: float | None,
        executed_at: datetime,
        notes: str | None,
    ) -> bool:
        stmt = (
            select(Transaction.id)
            .join(Position, Transaction.position_id == Position.id)
            .join(Security, Position.security_id == Security.id)
            .where(
                Security.ticker == ticker.strip().upper(),
                Transaction.action == action,
                Transaction.quantity == quantity,
                Transaction.executed_at == executed_at,
                self.active_transaction_clause(),
            )
        )
        if price is None:
            stmt = stmt.where(Transaction.price.is_(None))
        else:
            stmt = stmt.where(Transaction.price == price)
        normalized_notes = (notes or "").strip()
        if normalized_notes:
            stmt = stmt.where(Transaction.notes == normalized_notes)
        else:
            stmt = stmt.where((Transaction.notes.is_(None)) | (Transaction.notes == ""))
        existing = (await self.session.execute(stmt.limit(1))).scalar_one_or_none()
        return existing is not None

    def _parse_robinhood_activity_rows(
        self, reader: csv.DictReader
    ) -> list[dict[str, Any]]:
        parsed_rows: list[dict[str, Any]] = []
        for index, row in enumerate(reader, start=2):
            trans_code = (row.get("Trans Code") or "").strip().upper()
            ticker = (row.get("Instrument") or "").strip().upper()
            description = " ".join((row.get("Description") or "").split()) or None
            entity_name = self._extract_broker_entity_name(row.get("Description"))

            if trans_code in {"", "SLIP", "REC"}:
                continue
            if trans_code not in {"BUY", "SELL", "CDIV", "ACH"}:
                continue
            if not ticker and trans_code != "ACH":
                continue

            executed_at_raw = (row.get("Activity Date") or "").strip()
            if not executed_at_raw:
                parsed_rows.append(
                    {
                        "row_number": index,
                        "ticker": ticker,
                        "entity_name": entity_name,
                        "action": "buy",
                        "quantity": 0.0,
                        "price": None,
                        "executed_at": datetime.now(UTC),
                        "notes": description,
                        "list_type": "holding",
                        "direction": "long",
                        "invalid": "missing Activity Date",
                    }
                )
                continue

            if trans_code == "CDIV":
                amount = self._parse_decimal_string((row.get("Amount") or "").strip())
                if amount <= 0:
                    continue
                parsed_rows.append(
                    {
                        "row_number": index,
                        "ticker": ticker,
                        "entity_name": entity_name,
                        "action": "dividend",
                        "quantity": 0.0,
                        "price": amount,
                        "executed_at": txn_datetime(executed_at_raw),
                        "notes": description or "robinhood_cash_dividend",
                        "list_type": "holding",
                        "direction": "long",
                    }
                )
                continue

            if trans_code == "ACH":
                amount = self._parse_decimal_string((row.get("Amount") or "").strip())
                if amount == 0:
                    continue
                parsed_rows.append(
                    {
                        "row_number": index,
                        "ticker": "CASH",
                        "entity_name": "USD",
                        "action": "deposit" if amount > 0 else "withdrawal",
                        "quantity": 0.0,
                        "price": amount,
                        "executed_at": txn_datetime(executed_at_raw),
                        "notes": description or "robinhood_cash_transfer",
                        "list_type": "holding",
                        "direction": "long",
                    }
                )
                continue

            quantity_raw = (row.get("Quantity") or "").strip()
            price_raw = (row.get("Price") or "").strip()
            if not quantity_raw or not price_raw:
                parsed_rows.append(
                    {
                        "row_number": index,
                        "ticker": ticker,
                        "entity_name": entity_name,
                        "action": trans_code.lower(),
                        "quantity": 0.0,
                        "price": None,
                        "executed_at": txn_datetime(executed_at_raw),
                        "notes": description,
                        "list_type": "holding",
                        "direction": "long",
                        "invalid": "missing quantity or price",
                    }
                )
                continue

            parsed_rows.append(
                {
                    "row_number": index,
                    "ticker": ticker,
                    "entity_name": entity_name,
                    "action": trans_code.lower(),
                    "quantity": self._parse_decimal_string(quantity_raw),
                    "price": self._parse_decimal_string(price_raw),
                    "executed_at": txn_datetime(executed_at_raw),
                    "notes": description,
                    "list_type": "holding",
                    "direction": "long",
                }
            )
        return self._sort_import_rows(parsed_rows)

    def _sort_import_rows(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        valid_rows = [row for row in rows if not row.get("invalid")]
        invalid_rows = [row for row in rows if row.get("invalid")]
        valid_rows.sort(key=lambda row: (row["executed_at"], row["row_number"]))
        return valid_rows + invalid_rows

    def _parse_decimal_string(self, raw_value: str) -> float:
        normalized = raw_value.strip().replace("$", "").replace(",", "")
        if not normalized:
            return 0.0
        if normalized.startswith("(") and normalized.endswith(")"):
            normalized = f"-{normalized[1:-1]}"
        return float(normalized)

    def _extract_broker_entity_name(self, raw_description: str | None) -> str | None:
        if not raw_description:
            return None
        first_line = raw_description.splitlines()[0].strip()
        if not first_line:
            return None
        if first_line.upper() in {"ACH DEPOSIT", "STOCK LENDING"}:
            return None
        return first_line

    async def import_simple_text(
        self,
        payload: PortfolioSimpleImportRequest,
    ) -> PortfolioImportResponse:
        mode = payload.mode.strip().lower()
        if mode not in {"holdings", "transactions"}:
            raise ValueError("Simple import mode must be 'holdings' or 'transactions'.")

        imported_count = 0
        skipped_count = 0
        errors: list[str] = []
        inferred_remaining_buying_power: float | None = None
        inference_note: str | None = None
        default_executed_at = payload.default_executed_at or datetime.now(UTC)
        had_existing_positions = (
            await self.session.execute(select(Position.id).limit(1))
        ).scalar_one_or_none() is not None
        had_existing_transactions = (
            await self.session.execute(select(Transaction.id).limit(1))
        ).scalar_one_or_none() is not None
        had_existing_ledger = (
            await self.session.execute(select(CashLedgerEntry.id).limit(1))
        ).scalar_one_or_none() is not None
        imported_holdings_notional = 0.0
        earliest_imported_at: datetime | None = None

        for index, raw_line in enumerate(payload.content.splitlines(), start=1):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                if mode == "holdings":
                    ticker, quantity, avg_cost, executed_at = (
                        self._parse_simple_holding_line(
                            line=line,
                            default_executed_at=default_executed_at,
                        )
                    )
                    imported_holdings_notional += quantity * avg_cost
                    earliest_imported_at = (
                        executed_at
                        if earliest_imported_at is None
                        or executed_at < earliest_imported_at
                        else earliest_imported_at
                    )
                    await self.add_transaction_by_ticker(
                        ticker=ticker,
                        txn_data=TransactionCreate(
                            action="buy",
                            quantity=quantity,
                            price=avg_cost,
                            executed_at=executed_at,
                            notes="simple_holdings_import",
                            lot_type="simple_import",
                        ),
                        list_type="holding",
                        direction="long",
                    )
                else:
                    row = self._parse_simple_transaction_line(line=line)
                    await self.add_transaction_by_ticker(
                        ticker=row["ticker"],
                        txn_data=TransactionCreate(
                            action=row["action"],
                            quantity=row["quantity"],
                            price=row["price"],
                            executed_at=row["executed_at"],
                            notes=row["notes"],
                            lot_type="simple_import",
                        ),
                        list_type=row["list_type"],
                        direction=row["direction"],
                    )
                imported_count += 1
            except Exception as exc:
                skipped_count += 1
                errors.append(f"Line {index}: {exc}")

        if (
            mode == "holdings"
            and imported_count > 0
            and not had_existing_positions
            and not had_existing_transactions
            and not had_existing_ledger
        ):
            target_buying_power = float(
                RuntimeSettingsStore.load().portfolio.remaining_buying_power or 0.0
            )
            await self._seed_initial_cash_for_holdings_snapshot(
                imported_holdings_notional=imported_holdings_notional,
                target_buying_power=target_buying_power,
                executed_at=(earliest_imported_at or default_executed_at),
            )
            await self.recalculate_cash_ledger()
            inferred_remaining_buying_power = round(target_buying_power, 2)
            inference_note = (
                "Seeded opening cash for the initial holdings snapshot so buying power "
                f"starts at ${target_buying_power:,.2f} instead of going negative."
            )

        return PortfolioImportResponse(
            imported_count=imported_count,
            skipped_count=skipped_count,
            errors=errors,
            inferred_remaining_buying_power=inferred_remaining_buying_power,
            inference_note=inference_note,
        )

    async def _seed_initial_cash_for_holdings_snapshot(
        self,
        *,
        imported_holdings_notional: float,
        target_buying_power: float,
        executed_at: datetime,
    ) -> None:
        """
        A holdings-only snapshot is a point-in-time book, not a full trade history.
        Seed an opening cash deposit so the imported buy transactions do not imply
        the user overspent from a zero-cash account on day one.
        """
        seed_amount = round(
            float(imported_holdings_notional) + float(target_buying_power), 2
        )
        if seed_amount == 0.0:
            return

        self.session.add(
            Transaction(
                position_id=None,
                action="deposit",
                quantity=1.0,
                price=seed_amount,
                executed_at=executed_at.replace(microsecond=0),
                notes="initial_holdings_snapshot_seed",
            )
        )
        await self.session.commit()

    def _parse_simple_holding_line(
        self,
        *,
        line: str,
        default_executed_at: datetime,
    ) -> tuple[str, float, float, datetime]:
        parts = (
            [part.strip() for part in line.split(",")] if "," in line else line.split()
        )
        if len(parts) < 3:
            raise ValueError(
                "Holdings lines must be 'ticker, quantity, avg_cost' with optional executed_at."
            )
        ticker = parts[0].upper()
        quantity = float(parts[1])
        avg_cost = float(parts[2].replace("$", ""))
        executed_at = (
            txn_datetime(parts[3])
            if len(parts) >= 4 and parts[3]
            else default_executed_at
        )
        if quantity <= 0:
            raise ValueError("Holding quantity must be positive.")
        if avg_cost <= 0:
            raise ValueError("Average cost must be positive.")
        return ticker, quantity, avg_cost, executed_at

    def _parse_simple_transaction_line(self, *, line: str) -> dict[str, object]:
        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 4:
            raise ValueError(
                "Transaction lines must be 'ticker, action, quantity, executed_at' with optional price, notes, list_type, direction."
            )
        ticker = parts[0].upper()
        action = parts[1].lower()
        quantity = float(parts[2])
        executed_at = txn_datetime(parts[3])
        price = None
        if len(parts) >= 5 and parts[4]:
            price = float(parts[4].replace("$", ""))
        notes = parts[5] if len(parts) >= 6 and parts[5] else None
        list_type = parts[6] if len(parts) >= 7 and parts[6] else "holding"
        direction = parts[7] if len(parts) >= 8 and parts[7] else "long"
        if action not in {"buy", "sell"}:
            raise ValueError(
                "Simple transaction import currently supports buy and sell rows only."
            )
        return {
            "ticker": ticker,
            "action": action,
            "quantity": quantity,
            "executed_at": executed_at,
            "price": price,
            "notes": notes,
            "list_type": list_type,
            "direction": direction,
        }

    async def _get_or_create_security_for_ticker(
        self,
        *,
        ticker: str,
        entity_name: str | None = None,
    ) -> Security:
        normalized_ticker = ticker.strip().upper()
        stmt = (
            select(Security)
            .where(Security.ticker == normalized_ticker)
            .options(selectinload(Security.entity))
        )
        security = (await self.session.execute(stmt)).scalar_one_or_none()
        if security is not None:
            if (
                entity_name
                and security.entity is not None
                and security.entity.name.strip().upper() == normalized_ticker
            ):
                security.entity.name = entity_name.strip()
                await self.session.flush()
            return security

        entity = Entity(
            name=(entity_name or normalized_ticker).strip() or normalized_ticker,
            entity_type="company",
        )
        self.session.add(entity)
        await self.session.flush()

        security = Security(
            entity_id=entity.id,
            ticker=normalized_ticker,
            asset_class="equity",
            instrument_type="common_stock",
        )
        self.session.add(security)
        await self.session.flush()
        await self.session.refresh(security, ["entity"])
        return security

    def _resync_position_from_lots(self, position: Position, lots: list[Lot]) -> None:
        """Recompute quantity and avg cost basis from the position's open lots.

        Deriving from lots (rather than blindly subtracting a trade quantity)
        keeps the book consistent when the lot history is incomplete — an
        oversell can at worst flatten the position, never make it negative.
        """
        open_lots = [
            l for l in lots if l.closed_at is None and _to_decimal(l.quantity) > 0
        ]
        total_qty = sum((_to_decimal(l.quantity) for l in open_lots), Decimal("0"))
        if total_qty > 0:
            total_cost = sum(
                (
                    _to_decimal(l.quantity) * _to_decimal(l.cost_basis)
                    for l in open_lots
                ),
                Decimal("0"),
            )
            position.quantity = total_qty
            position.avg_cost_basis = total_cost / total_qty
        else:
            position.quantity = Decimal("0")
            position.avg_cost_basis = Decimal("0")
            position.list_type = "closed"

    @staticmethod
    def _norm_ticker(ticker: str | None) -> str:
        return (ticker or "").strip().upper()

    @staticmethod
    def _snapshot_number(token: str) -> float | None:
        cleaned = (
            (token or "")
            .strip()
            .strip(":")
            .replace("$", "")
            .replace(",", "")
            .replace("%", "")
        )
        if cleaned in ("", "-", "."):
            return None
        try:
            return float(cleaned)
        except ValueError:
            return None

    @staticmethod
    def _looks_like_ticker(token: str) -> bool:
        token = (token or "").strip().strip(":")
        return (
            bool(re.match(r"^[A-Za-z][A-Za-z.\-]{0,5}$", token)) and not token.isdigit()
        )

    @staticmethod
    def parse_holdings_snapshot(raw: str) -> dict:
        """Parse a pasted/exported holdings snapshot into a reconcile payload.

        Deterministic (no LLM) so it can be unit-tested. Accepts:
          * a freeform paste, one holding per line: ``EXMPL 10``, ``EXMPL,10``,
            ``10 EXMPL``, ``EXMPL: 10.5``
          * a CSV/TSV export with a ``Symbol``/``Ticker``/``Instrument`` column
            and a ``Quantity``/``Shares`` column (e.g. a Robinhood statement)
          * a cash line: ``CASH 2500`` / ``Buying power: 2500``
        Returns ``{"holdings": [{"ticker","quantity"}], "cash": float|None}``.
        """

        def split_row(line: str) -> list[str]:
            if "," in line:
                parts = line.split(",")
            elif "\t" in line:
                parts = line.split("\t")
            else:
                parts = line.split()
            return [p.strip() for p in parts if p.strip() != ""]

        def extract_number(text: str) -> float | None:
            match = re.search(r"[-+]?\$?[\d,]*\.?\d+", text or "")
            return PortfolioService._snapshot_number(match.group(0)) if match else None

        holdings: list[dict] = []
        cash: float | None = None

        # Cash lines are matched on the raw text first, so a thousands-comma in
        # the value can't collide with comma-as-delimiter.
        cash_re = re.compile(
            r"^\s*(cash(?:\s*balance)?|buying\s*power|total\s*cash|settled\s*cash)\b[:=]?\s*(.+)$",
            re.IGNORECASE,
        )
        data_lines: list[str] = []
        for line in (raw or "").splitlines():
            if not line.strip():
                continue
            match = cash_re.match(line)
            if match:
                number = extract_number(match.group(2))
                if number is not None:
                    cash = number
                continue
            data_lines.append(line)

        rows = [split_row(ln) for ln in data_lines]

        sym_keys = {"symbol", "ticker", "instrument"}
        qty_keys = {"quantity", "shares", "qty", "units", "share quantity"}
        sym_i = qty_i = None
        if rows:
            header = [c.lower() for c in rows[0]]
            if any(c in sym_keys for c in header) and any(
                c in qty_keys for c in header
            ):
                sym_i = next(i for i, c in enumerate(header) if c in sym_keys)
                qty_i = next(i for i, c in enumerate(header) if c in qty_keys)
                rows = rows[1:]

        for cols in rows:
            if not cols:
                continue
            if sym_i is not None:
                if len(cols) <= max(sym_i, qty_i):
                    continue
                ticker, qty = cols[sym_i], PortfolioService._snapshot_number(
                    cols[qty_i]
                )
                if PortfolioService._looks_like_ticker(ticker) and qty is not None:
                    holdings.append({"ticker": ticker.upper(), "quantity": qty})
                continue
            tickers = [c for c in cols if PortfolioService._looks_like_ticker(c)]
            numbers = [
                n
                for n in (PortfolioService._snapshot_number(c) for c in cols)
                if n is not None
            ]
            if len(tickers) == 1 and numbers:
                holdings.append(
                    {"ticker": tickers[0].strip(":").upper(), "quantity": numbers[0]}
                )

        return {"holdings": holdings, "cash": cash}

    @staticmethod
    def diff_positions(
        book: list[dict],
        snapshot: list[dict],
        qty_tolerance: Decimal = Decimal("0.0001"),
    ) -> list[dict]:
        """Compare the reconstructed book to an authoritative broker snapshot.

        Pure function (no DB) so it can be unit-tested. Each input item is a
        dict with at least ``ticker`` and ``quantity``. Returns one diff per
        ticker whose share count disagrees beyond tolerance.
        """

        def fold(items: list[dict]) -> dict[str, Decimal]:
            out: dict[str, Decimal] = {}
            for item in items:
                t = PortfolioService._norm_ticker(item.get("ticker"))
                if not t:
                    continue
                out[t] = out.get(t, Decimal("0")) + _to_decimal(item.get("quantity"))
            return out

        book_by = fold(book)
        snap_by = fold(snapshot)
        diffs: list[dict] = []
        for ticker in sorted(set(book_by) | set(snap_by)):
            b = book_by.get(ticker, Decimal("0"))
            s = snap_by.get(ticker, Decimal("0"))
            delta = s - b
            if abs(delta) <= qty_tolerance:
                continue
            if b == 0:
                kind = "missing_in_book"  # broker has it, we don't
            elif s == 0:
                kind = "extra_in_book"  # we have it, broker doesn't
            else:
                kind = "quantity_mismatch"
            diffs.append(
                {
                    "ticker": ticker,
                    "kind": kind,
                    "book_quantity": b,
                    "broker_quantity": s,
                    "delta": delta,
                }
            )
        return diffs

    async def reconcile_positions(
        self,
        snapshot: list[dict],
        *,
        broker_cash: float | None = None,
        create_review_items: bool = True,
    ) -> dict:
        """Reconcile the reconstructed book against an authoritative snapshot.

        ``snapshot`` is provider-agnostic: a list of ``{"ticker", "quantity"}``
        from a broker export or manual current-holdings entry. (Plaid's holdings
        coverage for Robinhood is unreliable, so manual entry is the practical
        truth source.) Optionally compares reconstructed cash to ``broker_cash``.
        Material discrepancies are surfaced as pending review-queue items rather
        than silently corrected — the book is rebuilt from evidence, so a human
        decides what truth wins.
        """
        positions = (
            (
                await self.session.execute(
                    select(Position).options(selectinload(Position.security))
                )
            )
            .scalars()
            .all()
        )

        book = [
            {"ticker": getattr(p.security, "ticker", None), "quantity": p.quantity}
            for p in positions
            if _to_decimal(p.quantity) != 0
        ]
        pos_id_by_ticker = {
            self._norm_ticker(getattr(p.security, "ticker", None)): p.id
            for p in positions
        }

        diffs = self.diff_positions(book, snapshot)

        cash_discrepancy = None
        if broker_cash is not None:
            latest_ledger = (
                await self.session.execute(
                    select(CashLedgerEntry)
                    .order_by(
                        desc(CashLedgerEntry.executed_at),
                        desc(CashLedgerEntry.created_at),
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            book_cash = (
                _to_decimal(latest_ledger.balance_after)
                if latest_ledger
                else Decimal("0")
            )
            delta = _to_decimal(broker_cash) - book_cash
            if abs(delta) > Decimal("1"):  # ignore sub-dollar rounding
                cash_discrepancy = {
                    "book_cash": float(book_cash),
                    "broker_cash": float(broker_cash),
                    "delta": float(delta),
                }

        created = 0
        if create_review_items:
            for d in diffs:
                position_id = pos_id_by_ticker.get(d["ticker"])
                item_type = "position" if position_id else "reconciliation"
                item_id = position_id or uuid5(
                    NAMESPACE_URL, f"reconcile:{d['ticker']}"
                )
                reason = (
                    f"Reconciliation discrepancy for {d['ticker']}: "
                    f"book={d['book_quantity']} vs broker={d['broker_quantity']} "
                    f"({d['kind']})"
                )
                existing = (
                    (
                        await self.session.execute(
                            select(ReviewQueueItem).where(
                                ReviewQueueItem.item_id == item_id,
                                ReviewQueueItem.status == "pending",
                                ReviewQueueItem.trigger_reason.like(
                                    "Reconciliation discrepancy%"
                                ),
                            )
                        )
                    )
                    .scalars()
                    .first()
                )
                if existing is not None:
                    existing.trigger_reason = reason  # refresh, don't duplicate
                    continue
                self.session.add(
                    ReviewQueueItem(
                        item_type=item_type,
                        item_id=item_id,
                        priority_score=(
                            60.0 if d["kind"] != "quantity_mismatch" else 45.0
                        ),
                        trigger_reason=reason,
                    )
                )
                created += 1

            if cash_discrepancy is not None:
                item_id = uuid5(NAMESPACE_URL, "reconcile:CASH")
                reason = (
                    f"Cash discrepancy: book=${cash_discrepancy['book_cash']:.2f} "
                    f"vs broker=${cash_discrepancy['broker_cash']:.2f} "
                    f"(delta ${cash_discrepancy['delta']:.2f})"
                )
                existing = (
                    (
                        await self.session.execute(
                            select(ReviewQueueItem).where(
                                ReviewQueueItem.item_id == item_id,
                                ReviewQueueItem.status == "pending",
                            )
                        )
                    )
                    .scalars()
                    .first()
                )
                if existing is not None:
                    existing.trigger_reason = reason
                else:
                    self.session.add(
                        ReviewQueueItem(
                            item_type="reconciliation",
                            item_id=item_id,
                            priority_score=55.0,
                            trigger_reason=reason,
                        )
                    )
                    created += 1

            await self.session.commit()

        return {
            "in_sync": len(diffs) == 0 and cash_discrepancy is None,
            "discrepancies": diffs,
            "cash_discrepancy": cash_discrepancy,
            "review_items_created": created,
        }

    async def _consolidate_trade_positions(self) -> None:
        """Merge fragmented trade rows for the same security + direction.

        Repeated email backfills split a single economic position across many
        rows: a sell that zeroes a holding flips it to ``closed`` (see
        ``add_transaction``), then the next buy email spawns a brand-new holding
        row. The per-row replay in ``recalculate_all_positions`` cannot net buys
        against sells that landed on different rows, so current holdings get
        overstated. Re-point every trade transaction for a (security, direction)
        onto one canonical position so the replay sees the complete, nettable
        history. Idempotent: a second pass finds nothing left to merge.
        """
        positions = (await self.session.execute(select(Position))).scalars().all()
        transactions = (
            (
                await self.session.execute(
                    select(Transaction).where(self.active_transaction_clause())
                )
            )
            .scalars()
            .all()
        )

        txns_with_pos = [t for t in transactions if t.position_id]
        pos_ids_with_txns = {t.position_id for t in txns_with_pos}

        groups: dict[tuple, list[Position]] = defaultdict(list)
        for p in positions:
            if p.list_type in ("holding", "closed") or p.id in pos_ids_with_txns:
                groups[(p.security_id, p.direction)].append(p)

        def _order_key(p: Position):
            return (getattr(p, "added_at", None) or datetime.max, str(p.id))

        for plist in groups.values():
            if len(plist) <= 1:
                continue
            holdings = [p for p in plist if p.list_type == "holding"]
            canonical = sorted(holdings or plist, key=_order_key)[0]
            canonical.list_type = "holding"
            plist_ids = {p.id for p in plist}
            for t in txns_with_pos:
                if t.position_id in plist_ids and t.position_id != canonical.id:
                    t.position_id = canonical.id
            for p in plist:
                if p.id != canonical.id:
                    p.list_type = "closed"
                    p.quantity = Decimal("0")
                    p.market_value = Decimal("0")
                    p.unrealized_pnl = Decimal("0")
        await self.session.flush()

    async def recalculate_all_positions(self) -> None:
        """
        High-performance hard reset of all positions and lots.
        Rebuilds the entire book state in-memory before committing to avoid DB contention.
        """
        import asyncio

        from sqlalchemy import delete

        # 0. Heal fragmented trade rows so buys and sells for the same security
        #    net against each other regardless of which row they were attached to.
        await self._consolidate_trade_positions()

        # 1. Clear everything derived
        await self.session.execute(delete(Lot))
        positions = (await self.session.execute(select(Position))).scalars().all()
        # Preserve any live market price; the replay below must not clobber it
        # with a stale last-trade price (only fall back to trade price when a
        # position has never been priced by the market-data refresh).
        live_price_before = {p.id: _to_decimal(p.current_price) for p in positions}
        for p in positions:
            p.quantity = Decimal("0")
            p.avg_cost_basis = Decimal("0")
            p.unrealized_pnl = Decimal("0")
            p.market_value = Decimal("0")
        await self.session.flush()

        # 2. Get all transactions chronologically
        transactions = (
            (
                await self.session.execute(
                    select(Transaction)
                    .where(self.active_transaction_clause())
                    .order_by(Transaction.executed_at.asc(), Transaction.id.asc())
                )
            )
            .scalars()
            .all()
        )

        # 3. Re-play in-memory
        pos_map = {p.id: p for p in positions}
        lots_by_pos = defaultdict(list)

        for i, txn in enumerate(transactions):
            # Yield to event loop every 100 transactions to keep UI responsive
            if i % 100 == 0:
                await asyncio.sleep(0)

            if not txn.position_id:
                continue
            position = pos_map.get(txn.position_id)
            if not position:
                continue

            current_qty = _to_decimal(position.quantity)
            avg_cost = _to_decimal(position.avg_cost_basis)
            trade_qty = _to_decimal(txn.quantity)
            trade_price = _to_decimal(txn.price or 0.0)

            if txn.action == "buy":
                new_lot = Lot(
                    position_id=position.id,
                    quantity=trade_qty,
                    cost_basis=trade_price,
                    acquired_at=txn.executed_at,
                    lot_type="rebuild",
                )
                self.session.add(new_lot)
                lots_by_pos[position.id].append(new_lot)

                total_cost = (current_qty * avg_cost) + (trade_qty * trade_price)
                new_qty = current_qty + trade_qty
                position.quantity = new_qty
                position.avg_cost_basis = (
                    total_cost / new_qty if new_qty > 0 else Decimal("0")
                )
                position.current_price = trade_price
                # Re-buying a name that was previously sold to zero reactivates it.
                # A prior sell flips the row to "closed"; without this it would
                # keep nonzero shares yet stay hidden from the holdings list.
                if new_qty > 0 and position.list_type == "closed":
                    position.list_type = "holding"

            elif txn.action == "sell":
                open_lots = [
                    l
                    for l in lots_by_pos[position.id]
                    if l.closed_at is None and _to_decimal(l.quantity) > 0
                ]
                # Sort in-memory to ensure FIFO
                open_lots.sort(key=lambda x: x.acquired_at)

                qty_to_sell = trade_qty
                for lot in open_lots:
                    if qty_to_sell <= 0:
                        break
                    lot_qty = _to_decimal(lot.quantity)
                    sell_qty = min(lot_qty, qty_to_sell)
                    realized_pnl = (
                        trade_price - _to_decimal(lot.cost_basis)
                    ) * sell_qty
                    lot.quantity = lot_qty - sell_qty
                    lot.realized_pnl = (
                        _to_decimal(getattr(lot, "realized_pnl", 0.0)) + realized_pnl
                    )
                    qty_to_sell -= sell_qty
                    if _to_decimal(lot.quantity) <= Decimal("0"):
                        lot.quantity = Decimal("0")
                        lot.closed_at = txn.executed_at

                # Derive quantity/avg-cost from remaining open lots so a sell that
                # exceeds known lots (e.g. a missing buy email) cannot drive the
                # position negative. The shortfall is left for reconciliation.
                self._resync_position_from_lots(position, lots_by_pos[position.id])
                position.current_price = trade_price

            elif txn.action == "split":
                # Convention: txn.quantity carries the split ratio (post/pre).
                # 4:1 split -> 4.0 ; 1:10 reverse split -> 0.1. Total basis is
                # preserved: shares scale up by r, per-share basis scales down by r.
                ratio = trade_qty if trade_qty > 0 else Decimal("1")
                for lot in lots_by_pos[position.id]:
                    if lot.closed_at is not None:
                        continue
                    lot.quantity = _to_decimal(lot.quantity) * ratio
                    lot.cost_basis = _to_decimal(lot.cost_basis) / ratio
                if _to_decimal(position.current_price) > 0:
                    position.current_price = _to_decimal(position.current_price) / ratio
                self._resync_position_from_lots(position, lots_by_pos[position.id])

            elif txn.action == "expire":
                # Option/right expired worthless: close all open lots, realizing
                # the remaining cost basis as a loss.
                for lot in lots_by_pos[position.id]:
                    if lot.closed_at is not None:
                        continue
                    lot_qty = _to_decimal(lot.quantity)
                    lot.realized_pnl = _to_decimal(
                        getattr(lot, "realized_pnl", 0.0)
                    ) - (lot_qty * _to_decimal(lot.cost_basis))
                    lot.quantity = Decimal("0")
                    lot.closed_at = txn.executed_at
                self._resync_position_from_lots(position, lots_by_pos[position.id])

            # NOTE: merger | spinoff | exercise | assign require target-security
            # mapping and ratios that broker emails do not reliably carry. Rather
            # than corrupt the book by guessing, they are left unreplayed here and
            # surfaced by the reconciliation pass as a discrepancy to correct.

            position.market_value = _to_decimal(position.quantity) * _to_decimal(
                position.current_price
            )
            position.unrealized_pnl = _to_decimal(position.quantity) * (
                _to_decimal(position.current_price)
                - _to_decimal(position.avg_cost_basis)
            )

        # 3b. Restore live market prices clobbered by the replay's last-trade
        #     fallback, then recompute valuation from the authoritative price.
        for position in positions:
            live = live_price_before.get(position.id, Decimal("0"))
            if live > 0:
                position.current_price = live
            position.market_value = _to_decimal(position.quantity) * _to_decimal(
                position.current_price
            )
            position.unrealized_pnl = _to_decimal(position.quantity) * (
                _to_decimal(position.current_price)
                - _to_decimal(position.avg_cost_basis)
            )

        # 4. Final sync of cash ledger (single pass)
        await self.recalculate_cash_ledger()
        await self.session.commit()

    async def recalculate_cash_ledger(self) -> None:
        """
        High-performance rebuild of the cash ledger.
        """
        import asyncio

        from sqlalchemy import delete

        await self.session.execute(delete(CashLedgerEntry))
        await self.session.flush()

        transactions = (
            (
                await self.session.execute(
                    select(Transaction)
                    .where(self.active_transaction_clause())
                    .order_by(Transaction.executed_at.asc(), Transaction.id.asc())
                )
            )
            .scalars()
            .all()
        )

        # Pre-cache positions to avoid DB queries in the loop
        positions = (
            (
                await self.session.execute(
                    select(Position).options(selectinload(Position.security))
                )
            )
            .scalars()
            .all()
        )
        pos_map = {p.id: p for p in positions}

        current_balance = 0.0
        for i, txn in enumerate(transactions):
            if i % 100 == 0:
                await asyncio.sleep(0)

            ticker = "shares"
            amount = 0.0
            entry_type = "trade_settlement"
            description = ""

            if txn.action in {"buy", "sell", "dividend"}:
                position = pos_map.get(txn.position_id) if txn.position_id else None
                if not position:
                    continue
                ticker = getattr(position.security, "ticker", "shares")
                if txn.action == "buy":
                    amount = -float(_to_decimal(txn.quantity) * _to_decimal(txn.price))
                    description = f"Bought {txn.quantity} {ticker} @ {txn.price}"
                elif txn.action == "sell":
                    amount = float(_to_decimal(txn.quantity) * _to_decimal(txn.price))
                    description = f"Sold {txn.quantity} {ticker} @ {txn.price}"
                elif txn.action == "dividend":
                    amount = float(_to_decimal(txn.price))
                    entry_type = "dividend"
                    description = f"Dividend for {ticker}"
            elif txn.action == "deposit":
                amount = float(_to_decimal(txn.price))
                entry_type = "deposit"
                description = txn.notes or "Deposit"
            elif txn.action == "withdrawal":
                amount = -float(_to_decimal(txn.price))
                entry_type = "withdrawal"
                description = txn.notes or "Withdrawal"
            else:
                continue

            if amount == 0 and txn.action not in {"dividend"}:
                continue

            current_balance += amount
            self.session.add(
                CashLedgerEntry(
                    entry_type=entry_type,
                    amount=amount,
                    balance_after=current_balance,
                    transaction_id=txn.id,
                    description=description,
                    executed_at=txn.executed_at,
                )
            )

        await self.session.commit()

    async def _build_series(self) -> list[PortfolioBuildPoint]:
        rows = (
            await self.session.execute(
                select(Transaction, Position)
                .join(Position, Transaction.position_id == Position.id)
                .where(self.active_transaction_clause())
                .order_by(Transaction.executed_at.asc(), Transaction.id.asc())
            )
        ).all()
        if not rows:
            return []

        grouped: dict[str, dict[str, object]] = defaultdict(
            lambda: {
                "as_of": None,
                "net_capital_delta": 0.0,
                "gross_trade_notional": 0.0,
                "transaction_count": 0,
                "active_holding_count": 0,
            }
        )
        running_quantities: dict[UUID, Decimal] = defaultdict(lambda: Decimal("0"))

        for txn, position in rows:
            trade_qty = _to_decimal(txn.quantity)
            trade_price = _to_decimal(txn.price)
            signed_qty = (
                trade_qty
                if txn.action == "buy"
                else -trade_qty if txn.action == "sell" else Decimal("0")
            )
            notional = abs(trade_qty * trade_price)
            running_quantities[position.id] += signed_qty
            as_of = txn.executed_at.replace(hour=0, minute=0, second=0, microsecond=0)
            bucket = grouped[as_of.isoformat()]
            bucket["as_of"] = as_of
            bucket["net_capital_delta"] = float(bucket["net_capital_delta"]) + float(
                signed_qty * trade_price
            )
            bucket["gross_trade_notional"] = float(
                bucket["gross_trade_notional"]
            ) + float(notional)
            bucket["transaction_count"] = int(bucket["transaction_count"]) + 1
            bucket["active_holding_count"] = sum(
                1 for qty in running_quantities.values() if qty > 0
            )

        cumulative_net = 0.0
        cumulative_gross = 0.0
        cumulative_transactions = 0
        series: list[PortfolioBuildPoint] = []
        for key in sorted(grouped.keys()):
            bucket = grouped[key]
            cumulative_net += float(bucket["net_capital_delta"])
            cumulative_gross += float(bucket["gross_trade_notional"])
            cumulative_transactions += int(bucket["transaction_count"])
            series.append(
                PortfolioBuildPoint(
                    as_of=bucket["as_of"],
                    net_capital_deployed=round(cumulative_net, 2),
                    gross_trade_notional=round(cumulative_gross, 2),
                    active_holding_count=int(bucket["active_holding_count"]),
                    transaction_count=cumulative_transactions,
                )
            )
        return series[-90:]


def txn_datetime(value: str):
    normalized = value.strip()
    if not normalized:
        raise ValueError("executed_at is required")

    if "/" in normalized and "T" not in normalized and " " not in normalized:
        return datetime.strptime(normalized, "%m/%d/%Y").replace(tzinfo=UTC)

    normalized = normalized.replace("Z", "+00:00")
    if "T" not in normalized and " " not in normalized:
        normalized = f"{normalized}T00:00:00+00:00"

    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
