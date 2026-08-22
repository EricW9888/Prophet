from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_DOWN, Decimal
from typing import Any
from uuid import UUID

_QUANTITY_STEP = Decimal("0.00000001")
_PRICE_STEP = Decimal("0.0001")
_MONEY_STEP = Decimal("0.0001")


def _decimal(value: Any, *, default: str = "0") -> Decimal:
    try:
        result = Decimal(str(value))
    except Exception:
        result = Decimal(default)
    return result if result.is_finite() else Decimal(default)


def _as_float(value: Decimal) -> float:
    return float(value.quantize(_MONEY_STEP))


@dataclass(frozen=True)
class PaperBrokerPolicy:
    slippage_bps: Decimal = Decimal("5")
    fee_per_order: Decimal = Decimal("0")
    max_buy_order_pct_equity: Decimal = Decimal("25")
    allow_fractional: bool = True
    require_regular_session: bool = True


@dataclass(frozen=True)
class PaperOrderRequest:
    security_id: UUID
    ticker: str
    side: str
    quantity: Decimal
    reference_price: Decimal
    quote_session: str
    submitted_at: datetime
    rationale: str
    checkpoint_index: int
    client_order_id: str
    quote_time: datetime | None = None
    evidence_refs: tuple[str, ...] = ()
    source_decision: dict[str, Any] | None = None


@dataclass(frozen=True)
class PaperBrokerExecution:
    status: str
    state: dict[str, Any]
    order: dict[str, Any]
    fill: dict[str, Any] | None
    rejection_reason: str | None = None


@dataclass(frozen=True)
class PaperAccountEventRequest:
    security_id: UUID
    ticker: str
    event_type: str
    occurred_at: datetime
    source_transaction_id: UUID
    split_ratio: Decimal | None = None
    dividend_per_share: Decimal | None = None
    derivation: str = "source_transaction"


@dataclass(frozen=True)
class PaperAccountEventExecution:
    status: str
    state: dict[str, Any]
    quantity_before: Decimal
    quantity_after: Decimal
    cash_before: Decimal
    cash_after: Decimal
    amount: Decimal
    detail: str
    rejection_reason: str | None = None


@dataclass(frozen=True)
class PaperRecordedFillRequest:
    security_id: UUID
    ticker: str
    side: str
    quantity: Decimal
    price: Decimal
    gross_notional: Decimal
    fee: Decimal
    filled_at: datetime


@dataclass(frozen=True)
class PaperRecordedFillExecution:
    status: str
    state: dict[str, Any]
    detail: str
    rejection_reason: str | None = None


class LocalPaperBroker:
    """Deterministic cash-account simulator. Model output never mutates account state."""

    def __init__(self, policy: PaperBrokerPolicy):
        self.policy = policy

    def estimate_quantity(
        self,
        *,
        state: dict[str, Any],
        security_id: UUID,
        side: str,
        desired_quantity: Decimal,
        reference_price: Decimal,
        account_equity: Decimal,
        cash_reserve: Decimal,
    ) -> tuple[Decimal, list[str]]:
        normalized = self.normalize_state(state)
        side = side.strip().lower()
        desired = max(Decimal("0"), _decimal(desired_quantity))
        price = max(Decimal("0"), _decimal(reference_price))
        adjustments: list[str] = []
        if side not in {"buy", "sell"} or desired <= 0 or price <= 0:
            return Decimal("0"), [
                "Order intent did not contain a valid executable quantity and quote."
            ]

        if side == "sell":
            position = self._position(normalized, str(security_id), create=False)
            available = _decimal(position.get("quantity"))
            executable = min(desired, available)
            if executable < desired:
                adjustments.append(
                    "Requested sale was reduced to the quantity held by the paper account."
                )
            return self._round_quantity(executable), adjustments

        cash = _decimal(normalized.get("cash"))
        reserved = _decimal(normalized.get("cash_reserved"))
        available_cash = max(
            Decimal("0"), cash - reserved - max(Decimal("0"), cash_reserve)
        )
        slippage_multiplier = Decimal("1") + (
            max(Decimal("0"), self.policy.slippage_bps) / Decimal("10000")
        )
        estimated_price = price * slippage_multiplier
        affordable = max(
            Decimal("0"),
            (available_cash - max(Decimal("0"), self.policy.fee_per_order))
            / estimated_price,
        )
        max_notional = (
            max(Decimal("0"), account_equity)
            * max(Decimal("0"), self.policy.max_buy_order_pct_equity)
            / Decimal("100")
        )
        max_by_equity = (
            max_notional / estimated_price if estimated_price > 0 else Decimal("0")
        )
        executable = min(desired, affordable, max_by_equity)
        if executable < desired:
            adjustments.append(
                "Requested size was reduced by deterministic cash, reserve, or per-order equity limits."
            )
        return self._round_quantity(executable), adjustments

    def submit_market_order(
        self,
        *,
        state: dict[str, Any],
        request: PaperOrderRequest,
        account_equity: Decimal,
        cash_reserve: Decimal,
        existing_reserved_notional: Decimal = Decimal("0"),
    ) -> PaperBrokerExecution:
        normalized = self.normalize_state(state)
        side = request.side.strip().lower()
        quantity = self._round_quantity(_decimal(request.quantity))
        reference_price = _decimal(request.reference_price)
        reserved_to_release = max(Decimal("0"), _decimal(existing_reserved_notional))
        normalized["cash_reserved"] = _as_float(
            max(
                Decimal("0"),
                _decimal(normalized.get("cash_reserved")) - reserved_to_release,
            )
        )

        rejection = self._validate_request(
            normalized,
            request=request,
            side=side,
            quantity=quantity,
            reference_price=reference_price,
            account_equity=account_equity,
            cash_reserve=cash_reserve,
        )
        if rejection:
            return self._rejected(
                normalized, request, quantity, reference_price, rejection
            )

        if (
            self.policy.require_regular_session
            and request.quote_session.lower()
            not in {
                "regular",
                "open",
            }
        ):
            estimated = quantity * reference_price + (
                self.policy.fee_per_order if side == "buy" else Decimal("0")
            )
            if side == "buy":
                normalized["cash_reserved"] = _as_float(
                    _decimal(normalized.get("cash_reserved")) + estimated
                )
            order = self._order_payload(
                request,
                quantity=quantity,
                reference_price=reference_price,
                status="accepted",
                reserved_notional=estimated if side == "buy" else Decimal("0"),
            )
            return PaperBrokerExecution(
                status="accepted",
                state=normalized,
                order=order,
                fill=None,
                rejection_reason="waiting_for_regular_market_session",
            )

        slippage = max(Decimal("0"), self.policy.slippage_bps) / Decimal("10000")
        fill_price = reference_price * (
            Decimal("1") + slippage if side == "buy" else Decimal("1") - slippage
        )
        fill_price = max(Decimal("0"), fill_price.quantize(_PRICE_STEP))
        gross_notional = (quantity * fill_price).quantize(_MONEY_STEP)
        fee = max(Decimal("0"), self.policy.fee_per_order).quantize(_MONEY_STEP)
        cash = _decimal(normalized.get("cash"))
        cash_reserved = _decimal(normalized.get("cash_reserved"))
        position = self._position(normalized, str(request.security_id), create=True)
        current_quantity = _decimal(position.get("quantity"))
        current_cost = _decimal(position.get("avg_cost_basis"))
        filled_at = request.quote_time or datetime.now(UTC)
        position["current_price"] = _as_float(fill_price)
        position["marked_at"] = filled_at.isoformat()

        if side == "buy":
            total_debit = gross_notional + fee
            available = cash - cash_reserved - max(Decimal("0"), cash_reserve)
            if total_debit > available:
                return self._rejected(
                    normalized,
                    request,
                    quantity,
                    reference_price,
                    "insufficient_buying_power_at_fill_price",
                )
            new_quantity = current_quantity + quantity
            position["quantity"] = _as_float(new_quantity)
            position["avg_cost_basis"] = _as_float(
                ((current_quantity * current_cost) + gross_notional + fee)
                / new_quantity
            )
            cash -= total_debit
        else:
            if quantity > current_quantity:
                return self._rejected(
                    normalized,
                    request,
                    quantity,
                    reference_price,
                    "insufficient_position_quantity",
                )
            new_quantity = current_quantity - quantity
            position["quantity"] = _as_float(new_quantity)
            if new_quantity == 0:
                position["avg_cost_basis"] = 0.0
            cash += gross_notional - fee

        normalized["cash"] = _as_float(cash)
        normalized["cash_reserved"] = _as_float(cash_reserved)
        order = self._order_payload(
            request,
            quantity=quantity,
            reference_price=reference_price,
            status="filled",
            reserved_notional=Decimal("0"),
            filled_quantity=quantity,
            filled_avg_price=fill_price,
            filled_at=filled_at,
        )
        fill = {
            "side": side,
            "quantity": _as_float(quantity),
            "price": _as_float(fill_price),
            "gross_notional": _as_float(gross_notional),
            "fee": _as_float(fee),
            "slippage_bps": _as_float(max(Decimal("0"), self.policy.slippage_bps)),
            "filled_at": filled_at,
            "quote_time": request.quote_time,
            "quote_session": request.quote_session,
            "cash_after": _as_float(cash),
            "position_quantity_after": _as_float(_decimal(position.get("quantity"))),
        }
        return PaperBrokerExecution(
            status="filled",
            state=normalized,
            order=order,
            fill=fill,
        )

    def apply_account_event(
        self,
        *,
        state: dict[str, Any],
        request: PaperAccountEventRequest,
    ) -> PaperAccountEventExecution:
        """Apply a source-backed corporate action without model discretion."""
        normalized = self.normalize_state(state)
        cash_before = _decimal(normalized.get("cash"))
        position = self._position(
            normalized,
            str(request.security_id),
            create=False,
        )
        quantity_before = _decimal(position.get("quantity"))
        event_type = request.event_type.strip().lower()

        if request.occurred_at.tzinfo is None:
            return self._account_event_result(
                normalized,
                status="rejected",
                quantity_before=quantity_before,
                quantity_after=quantity_before,
                cash_before=cash_before,
                detail="Corporate-action time must include a timezone.",
                rejection_reason="occurred_at_must_be_timezone_aware",
            )
        if event_type not in {"split", "dividend"}:
            return self._account_event_result(
                normalized,
                status="rejected",
                quantity_before=quantity_before,
                quantity_after=quantity_before,
                cash_before=cash_before,
                detail="The paper broker does not support this account event.",
                rejection_reason="unsupported_account_event",
            )
        if quantity_before <= 0:
            return self._account_event_result(
                normalized,
                status="not_applicable",
                quantity_before=quantity_before,
                quantity_after=quantity_before,
                cash_before=cash_before,
                detail=f"No {request.ticker.upper()} shares were held when the event occurred.",
            )

        if event_type == "dividend":
            dividend_per_share = _decimal(request.dividend_per_share)
            if dividend_per_share < 0 or request.dividend_per_share is None:
                return self._account_event_result(
                    normalized,
                    status="rejected",
                    quantity_before=quantity_before,
                    quantity_after=quantity_before,
                    cash_before=cash_before,
                    detail="A non-negative per-share dividend is required.",
                    rejection_reason="dividend_per_share_required",
                )
            amount = (quantity_before * dividend_per_share).quantize(_MONEY_STEP)
            normalized["cash"] = _as_float(cash_before + amount)
            return self._account_event_result(
                normalized,
                status="applied",
                quantity_before=quantity_before,
                quantity_after=quantity_before,
                cash_before=cash_before,
                amount=amount,
                detail=(
                    f"Credited {request.ticker.upper()} dividend at "
                    f"{dividend_per_share} per share ({request.derivation})."
                ),
            )

        split_ratio = _decimal(request.split_ratio)
        if split_ratio <= 0 or request.split_ratio is None:
            return self._account_event_result(
                normalized,
                status="rejected",
                quantity_before=quantity_before,
                quantity_after=quantity_before,
                cash_before=cash_before,
                detail="A positive split ratio is required.",
                rejection_reason="split_ratio_must_be_positive",
            )
        position["quantity"] = _as_float(quantity_before * split_ratio)
        position["avg_cost_basis"] = _as_float(
            _decimal(position.get("avg_cost_basis")) / split_ratio
        )
        marked_at = self._parse_timestamp(position.get("marked_at"))
        if marked_at is None or marked_at < request.occurred_at:
            position["current_price"] = _as_float(
                _decimal(position.get("current_price")) / split_ratio
            )
        return self._account_event_result(
            normalized,
            status="applied",
            quantity_before=quantity_before,
            quantity_after=_decimal(position.get("quantity")),
            cash_before=cash_before,
            detail=(
                f"Applied {request.ticker.upper()} split ratio {split_ratio}; "
                "share quantity and per-share cost were adjusted without changing basis."
            ),
        )

    def replay_recorded_fill(
        self,
        *,
        state: dict[str, Any],
        request: PaperRecordedFillRequest,
    ) -> PaperRecordedFillExecution:
        """Replay an immutable fill exactly when rebuilding an account timeline."""
        normalized = self.normalize_state(state)
        side = request.side.strip().lower()
        quantity = self._round_quantity(_decimal(request.quantity))
        price = _decimal(request.price)
        gross_notional = _decimal(request.gross_notional)
        fee = _decimal(request.fee)

        if request.filled_at.tzinfo is None:
            return self._recorded_fill_result(
                normalized,
                status="rejected",
                detail="Recorded fill time must include a timezone.",
                rejection_reason="filled_at_must_be_timezone_aware",
            )
        if side not in {"buy", "sell"}:
            return self._recorded_fill_result(
                normalized,
                status="rejected",
                detail="Recorded fill has an unsupported side.",
                rejection_reason="unsupported_order_side",
            )
        if quantity <= 0 or price <= 0 or gross_notional <= 0 or fee < 0:
            return self._recorded_fill_result(
                normalized,
                status="rejected",
                detail="Recorded fill has invalid quantity, price, notional, or fee.",
                rejection_reason="invalid_recorded_fill_values",
            )
        expected_notional = (quantity * price).quantize(_MONEY_STEP)
        if abs(expected_notional - gross_notional) > _MONEY_STEP:
            return self._recorded_fill_result(
                normalized,
                status="rejected",
                detail="Recorded fill notional does not reconcile to quantity and price.",
                rejection_reason="recorded_fill_notional_mismatch",
            )

        cash = _decimal(normalized.get("cash"))
        position = self._position(
            normalized,
            str(request.security_id),
            create=side == "buy",
        )
        current_quantity = _decimal(position.get("quantity"))
        current_cost = _decimal(position.get("avg_cost_basis"))
        if side == "buy":
            debit = gross_notional + fee
            if debit > cash:
                return self._recorded_fill_result(
                    normalized,
                    status="rejected",
                    detail="Recorded buy cannot be replayed from the rebuilt cash balance.",
                    rejection_reason="recorded_fill_insufficient_cash",
                )
            next_quantity = current_quantity + quantity
            position["quantity"] = _as_float(next_quantity)
            position["avg_cost_basis"] = _as_float(
                ((current_quantity * current_cost) + debit) / next_quantity
            )
            cash -= debit
        else:
            if quantity > current_quantity:
                return self._recorded_fill_result(
                    normalized,
                    status="rejected",
                    detail=(
                        "Recorded sale exceeds the shares available after corrected "
                        "corporate actions."
                    ),
                    rejection_reason="recorded_fill_insufficient_position_quantity",
                )
            next_quantity = current_quantity - quantity
            position["quantity"] = _as_float(next_quantity)
            if next_quantity == 0:
                position["avg_cost_basis"] = 0.0
            cash += gross_notional - fee

        normalized["cash"] = _as_float(cash)
        position["current_price"] = _as_float(price)
        position["marked_at"] = request.filled_at.isoformat()
        return self._recorded_fill_result(
            normalized,
            status="applied",
            detail=(
                f"Replayed {request.ticker.upper()} {side} fill: "
                f"{quantity} shares at {price}."
            ),
        )

    def normalize_state(self, state: dict[str, Any]) -> dict[str, Any]:
        normalized = deepcopy(state or {})
        normalized["cash"] = _as_float(
            max(Decimal("0"), _decimal(normalized.get("cash")))
        )
        normalized["cash_reserved"] = _as_float(
            max(Decimal("0"), _decimal(normalized.get("cash_reserved")))
        )
        positions: list[dict[str, Any]] = []
        for item in normalized.get("positions") or []:
            security_id = str(item.get("security_id") or "").strip()
            if not security_id:
                continue
            positions.append(
                {
                    "security_id": security_id,
                    "quantity": _as_float(
                        max(Decimal("0"), _decimal(item.get("quantity")))
                    ),
                    "avg_cost_basis": _as_float(
                        max(Decimal("0"), _decimal(item.get("avg_cost_basis")))
                    ),
                    "current_price": _as_float(
                        max(Decimal("0"), _decimal(item.get("current_price")))
                    ),
                    "marked_at": item.get("marked_at"),
                    "list_type": item.get("list_type") or "holding",
                }
            )
        normalized["positions"] = positions
        return normalized

    @staticmethod
    def _parse_timestamp(value: Any) -> datetime | None:
        if isinstance(value, datetime):
            return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)

    @staticmethod
    def _account_event_result(
        state: dict[str, Any],
        *,
        status: str,
        quantity_before: Decimal,
        quantity_after: Decimal,
        cash_before: Decimal,
        detail: str,
        amount: Decimal = Decimal("0"),
        rejection_reason: str | None = None,
    ) -> PaperAccountEventExecution:
        return PaperAccountEventExecution(
            status=status,
            state=state,
            quantity_before=quantity_before,
            quantity_after=quantity_after,
            cash_before=cash_before,
            cash_after=_decimal(state.get("cash")),
            amount=amount,
            detail=detail,
            rejection_reason=rejection_reason,
        )

    @staticmethod
    def _recorded_fill_result(
        state: dict[str, Any],
        *,
        status: str,
        detail: str,
        rejection_reason: str | None = None,
    ) -> PaperRecordedFillExecution:
        return PaperRecordedFillExecution(
            status=status,
            state=state,
            detail=detail,
            rejection_reason=rejection_reason,
        )

    def _validate_request(
        self,
        state: dict[str, Any],
        *,
        request: PaperOrderRequest,
        side: str,
        quantity: Decimal,
        reference_price: Decimal,
        account_equity: Decimal,
        cash_reserve: Decimal,
    ) -> str | None:
        if side not in {"buy", "sell"}:
            return "unsupported_order_side"
        if quantity <= 0:
            return "quantity_must_be_positive"
        if reference_price <= 0:
            return "executable_quote_required"
        if request.submitted_at.tzinfo is None:
            return "submitted_at_must_be_timezone_aware"
        if side == "sell":
            position = self._position(state, str(request.security_id), create=False)
            if quantity > _decimal(position.get("quantity")):
                return "insufficient_position_quantity"
            return None

        estimated = quantity * reference_price + max(
            Decimal("0"), self.policy.fee_per_order
        )
        if account_equity > 0:
            max_notional = (
                account_equity
                * max(Decimal("0"), self.policy.max_buy_order_pct_equity)
                / Decimal("100")
            )
            if estimated > max_notional:
                return "order_exceeds_max_buy_pct_equity"
        available = (
            _decimal(state.get("cash"))
            - _decimal(state.get("cash_reserved"))
            - max(Decimal("0"), cash_reserve)
        )
        if estimated > available:
            return "insufficient_buying_power"
        return None

    def _round_quantity(self, quantity: Decimal) -> Decimal:
        if not self.policy.allow_fractional:
            return quantity.to_integral_value(rounding=ROUND_DOWN)
        return quantity.quantize(_QUANTITY_STEP, rounding=ROUND_DOWN)

    @staticmethod
    def _position(
        state: dict[str, Any],
        security_id: str | None,
        *,
        create: bool,
    ) -> dict[str, Any]:
        key = security_id
        if key:
            for item in state.get("positions") or []:
                if str(item.get("security_id")) == key:
                    return item
        if not create or not key:
            return {"security_id": key or "", "quantity": 0.0, "avg_cost_basis": 0.0}
        position = {
            "security_id": key,
            "quantity": 0.0,
            "avg_cost_basis": 0.0,
            "current_price": 0.0,
            "marked_at": None,
            "list_type": "holding",
        }
        state.setdefault("positions", []).append(position)
        return position

    def _rejected(
        self,
        state: dict[str, Any],
        request: PaperOrderRequest,
        quantity: Decimal,
        reference_price: Decimal,
        reason: str,
    ) -> PaperBrokerExecution:
        return PaperBrokerExecution(
            status="rejected",
            state=state,
            order=self._order_payload(
                request,
                quantity=quantity,
                reference_price=reference_price,
                status="rejected",
                reserved_notional=Decimal("0"),
                rejection_reason=reason,
            ),
            fill=None,
            rejection_reason=reason,
        )

    @staticmethod
    def _order_payload(
        request: PaperOrderRequest,
        *,
        quantity: Decimal,
        reference_price: Decimal,
        status: str,
        reserved_notional: Decimal,
        rejection_reason: str | None = None,
        filled_quantity: Decimal = Decimal("0"),
        filled_avg_price: Decimal | None = None,
        filled_at: datetime | None = None,
    ) -> dict[str, Any]:
        return {
            "client_order_id": request.client_order_id,
            "ticker": request.ticker.upper(),
            "side": request.side.lower(),
            "order_type": "market",
            "time_in_force": "day",
            "requested_quantity": _as_float(quantity),
            "filled_quantity": _as_float(filled_quantity),
            "reference_price": _as_float(reference_price),
            "filled_avg_price": (
                None if filled_avg_price is None else _as_float(filled_avg_price)
            ),
            "status": status,
            "submitted_at": request.submitted_at,
            "accepted_at": (
                request.submitted_at if status in {"accepted", "filled"} else None
            ),
            "filled_at": filled_at,
            "rejection_reason": rejection_reason,
            "reserved_notional": _as_float(reserved_notional),
            "quote_session": request.quote_session,
            "quote_time": request.quote_time,
            "rationale": request.rationale,
            "checkpoint_index": request.checkpoint_index,
            "evidence_refs": list(request.evidence_refs),
            "source_decision": request.source_decision or {},
        }
