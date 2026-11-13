"""Read retained quote receipts without treating position caches as market evidence."""

import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal, DecimalException, InvalidOperation

from investos.schemas.portfolio_risk import RiskWatchPolicy


def number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def quote_mark(
    *,
    receipt,
    security_id,
    ticker,
    instrument_type,
    quantity,
    direction,
    cost,
    now=None,
):
    """A complete mark requires instrument, share basis, currency and both clocks."""
    now = now or datetime.now(UTC)
    receipt = receipt if isinstance(receipt, dict) else {}
    issues = []
    qty, price, basis = number(quantity), number(receipt.get("price")), number(cost)
    if not receipt:
        issues.append("No quote receipt; retained position caches are unverified.")
    if receipt.get("version") != 1 or not receipt.get("provider"):
        issues.append("The quote's source provenance is unavailable.")
    if instrument_type not in {"common_stock", "adr", "etf"} or direction != "long":
        issues.append("This instrument requires a supported valuation model.")
    if (
        receipt.get("security_id") != str(security_id)
        or receipt.get("ticker") != ticker
        or receipt.get("instrument_type") != instrument_type
    ):
        issues.append("The quote's instrument identity does not match this position.")
    if (
        qty is None
        or qty < 0
        or number(receipt.get("quantity")) != qty
        or receipt.get("direction") != direction
    ):
        issues.append("The position changed since this quote was captured.")
    if receipt.get("currency") != "USD":
        issues.append("A matching USD quote is unavailable; no FX rate was assumed.")
    if price is None or price < 0:
        issues.append("The quote has no finite nonnegative price.")
    quoted = observed = None
    try:
        quoted = datetime.fromisoformat(receipt["quoted_at"])
        observed = datetime.fromisoformat(receipt["observed_at"])
        if quoted.tzinfo is None or observed.tzinfo is None:
            raise ValueError("Missing timezone")
        if not quoted <= observed <= now:
            raise ValueError("Future quote or observation")
        if now - quoted > timedelta(
            hours=RiskWatchPolicy.model_fields["max_age_hours"].default
        ):
            issues.append(
                "The retained quote is stale under the portfolio freshness policy."
            )
    except (KeyError, TypeError, ValueError):
        issues.append("The quote has no valid, available source and observation time.")
    ready = not issues
    value = pnl = None
    if ready:
        try:
            value = qty * price
            pnl = qty * (price - basis) if basis is not None and basis >= 0 else None
            if not all(
                math.isfinite(float(item))
                for item in (price, value, pnl)
                if item is not None
            ):
                raise ValueError("Unrepresentable mark")
        except (DecimalException, OverflowError, ValueError):
            ready = False
            value = pnl = None
            issues.append(
                "The quoted calculation cannot be represented as a finite value."
            )
    return {
        "status": "ready" if ready else "unavailable",
        "ticker": ticker,
        "issues": issues,
        "price": float(price) if ready else None,
        "market_value": float(value) if value is not None else None,
        "unrealized_pnl": float(pnl) if pnl is not None else None,
        "quoted_at": quoted.isoformat() if quoted is not None else None,
        "observed_at": observed.isoformat() if observed is not None else None,
        "currency": receipt.get("currency"),
        "provider": receipt.get("provider"),
        "basis": "recorded_quote_times_current_recorded_shares_excluding_cash",
        "receipt": receipt or None,
    }


def mark_rows(rows):
    marks = {
        p.id: quote_mark(
            receipt=getattr(p, "quote_receipt_json", None),
            security_id=s.id,
            ticker=s.ticker,
            instrument_type=s.instrument_type,
            quantity=p.quantity,
            direction=p.direction,
            cost=p.avg_cost_basis,
        )
        for p, s, *_ in rows
    }
    active = [
        marks[p.id] for p, *_ in rows if p.list_type == "holding" and p.quantity != 0
    ]
    return marks, quote_coverage(active)


def quote_coverage(marks):
    priced = [mark for mark in marks if mark["market_value"] is not None]
    return {
        "complete": len(priced) == len(marks),
        "total_positions": len(marks),
        "priced_positions": len(priced),
        "covered_market_value": float(
            sum((number(mark["market_value"]) for mark in priced), Decimal("0"))
        ),
        "unavailable_tickers": [
            mark["ticker"] for mark in marks if mark["market_value"] is None
        ],
        "basis": "retained_source_quotes_for_recorded_shares_excluding_cash",
    }
