"""Receipts for observed manual paper-account valuations (not calibration data)."""

import hashlib
import json
import math
from datetime import UTC, datetime

from investos.core.dates import parse_iso_datetime

RETAINED_MARKS = 256


def record_manual_equity(
    details: dict,
    state: dict,
    quotes: dict,
    *,
    captured_at: datetime,
    opening: bool = False,
) -> None:
    cash = float(state.get("cash") or 0)
    held = [
        row for row in state.get("positions", []) if float(row.get("quantity") or 0) > 0
    ]
    prices = []
    for holding in held:
        security_id = str(holding.get("security_id") or "")
        ticker, quote = quotes.get(security_id, (None, {}))
        observed = parse_iso_datetime(quote.get("quote_time"))
        price = quote.get("price")
        if (
            ticker
            and quote.get("reported_symbol") == ticker
            and quote.get("currency") == "USD"
            and quote.get("session")
            and observed
            and observed <= captured_at
            and isinstance(price, (int, float))
            and math.isfinite(price)
            and price > 0
        ):
            prices.append(
                {
                    "security_id": security_id,
                    "ticker": ticker,
                    "quantity": float(holding["quantity"]),
                    "price": price,
                    "quote_time": observed.astimezone(UTC).isoformat(),
                    "session": quote["session"],
                }
            )
    price_windows = {(item["quote_time"][:10], item["session"]) for item in prices}
    equity = cash + sum(row["quantity"] * row["price"] for row in prices)
    complete = (
        math.isfinite(equity) and len(prices) == len(held) and len(price_windows) <= 1
    )
    details["manual_equity_status"] = {
        "captured_at": captured_at.isoformat(),
        "covered_positions": len(prices),
        "total_positions": len(held),
        "status": "complete" if complete else "incomplete",
    }
    if not complete:
        return
    prices.sort(key=lambda item: item["security_id"])
    basis = {"cash": cash, "prices": prices, "currency": "USD"}
    fingerprint = hashlib.sha256(
        json.dumps(basis, sort_keys=True, allow_nan=False).encode()
    ).hexdigest()
    marks = list(details.get("manual_equity_marks") or [])
    last = marks[-1] if marks else None
    if last and not last.get("superseded_at") and last["receipt_hash"] == fingerprint:
        return
    marks.append(
        {
            **basis,
            "receipt_hash": fingerprint,
            "captured_at": captured_at.isoformat(),
            "equity": round(equity, 2),
            "basis": (
                "opening_cash"
                if opening
                else "quote_batch" if prices else "cash_balance"
            ),
        }
    )
    details["manual_equity_marks"] = marks[-RETAINED_MARKS:]


def supersede_manual_equity(details: dict, *, rebuilt_at: str) -> None:
    if "manual_equity_marks" in details:
        details["manual_equity_marks"] = [
            {**mark, "superseded_at": mark.get("superseded_at") or rebuilt_at}
            for mark in details["manual_equity_marks"]
        ]
        details.pop("manual_equity_status", None)
