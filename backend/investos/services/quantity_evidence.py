"""Bind model-selected quantities to immutable, dimensioned measurements.

The model selects evidence; it cannot define measurements or verify its own math.
This module performs no price discovery, account reconciliation or trade actions.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from decimal import Decimal, DecimalException, InvalidOperation

QUANTITY_RE = re.compile(
    r"(?<![\w.])(?P<prefix_sign>[+-])?(?P<currency>USD\s*|EUR\s*|GBP\s*|KRW\s*|[$€£])?"
    r"(?P<number>[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?:[eE][+-]?\d+)?)"
    r"(?:\s*(?P<scale>thousand|million|billion|trillion|[kmbt])\b)?"
    r"(?:\s*(?P<unit>percentage points|percent|shares|units|%))?",
    re.IGNORECASE,
)
SCALES = {
    "k": 3,
    "thousand": 3,
    "m": 6,
    "million": 6,
    "b": 9,
    "billion": 9,
    "t": 12,
    "trillion": 12,
}

QUANTITY_BINDING_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "quote": {
            "type": "string",
            "description": "Exact complete quantity phrase in this assertion, including its currency, magnitude and unit.",
        },
        "measurement_ref": {"type": "string"},
        "occurrence": {
            "type": "integer",
            "minimum": 0,
            "description": "Zero-based occurrence of this complete quantity phrase in the statement.",
        },
        "value": {"type": "number"},
        "unit": {"type": "string"},
        "basis": {"type": "string"},
        "subject_ref": {"type": "string"},
        "snapshot_ref": {"type": "string"},
        "as_of": {"type": ["string", "null"]},
        "period_start": {"type": ["string", "null"]},
        "period_end": {"type": ["string", "null"]},
    },
    "required": [
        "quote",
        "measurement_ref",
        "occurrence",
        "value",
        "unit",
        "basis",
        "subject_ref",
        "snapshot_ref",
        "as_of",
        "period_start",
        "period_end",
    ],
}

QUANTITY_RULES = (
    " Account and calculated quantities require quantity_bindings to the supplied "
    "quantity_measurements. Copy the measurement's reference, unit, basis, subject, "
    "snapshot, as_of and period exactly, and quote each complete quantity phrase "
    "in the assertion. The numeric value must equal both that phrase (including "
    "magnitude) and the supplied measurement. A context path is not verified math. "
    "Do not substitute holdings value for total equity, cash for broker buying power, "
    "or a return from another period or denominator. Missing, partial or stale "
    "measurements are uncertainty, not zero. For retrieved issuer figures use "
    "source evidence and issuer scope, not an account measurement."
)


def finite_decimal(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
    except (ValueError, InvalidOperation):
        return None
    return parsed if parsed.is_finite() else None


def measurement(
    *,
    value: object,
    unit: str,
    basis: str,
    subject_ref: str,
    snapshot_ref: str,
    as_of: str | None,
    period_start: str | None = None,
    period_end: str | None = None,
    status: str = "complete",
    source_ref: str | None = None,
    notes: list[str] | None = None,
) -> dict:
    """Called only by deterministic producers; its identity includes all dimensions."""
    number = finite_decimal(value)
    item = {
        "value": str(number) if number is not None else None,
        "unit": unit,
        "basis": basis,
        "subject_ref": subject_ref,
        "snapshot_ref": snapshot_ref,
        "as_of": as_of,
        "period_start": period_start,
        "period_end": period_end,
        "status": status if number is not None else "missing",
        "source_ref": source_ref,
        "notes": notes or [],
    }
    if item["status"] == "complete" and not all(
        (unit, basis, subject_ref, snapshot_ref, as_of)
    ):
        item["status"] = "missing_dimensions"
    digest = hashlib.sha256(json.dumps(item, sort_keys=True).encode()).hexdigest()
    return {"measurement_ref": f"quantity:{digest}", **item}


def parse_quantity(quote: str) -> tuple[Decimal | None, str | None]:
    match = QUANTITY_RE.fullmatch(quote.strip())
    if not match:
        return None, None
    number = finite_decimal(match["number"].replace(",", ""))
    if number is None:
        return None, None
    if match["prefix_sign"]:
        if match["number"].startswith(("+", "-")):
            return None, None
        if match["prefix_sign"] == "-":
            number = -number
    scale = SCALES.get((match["scale"] or "").lower(), 0)
    try:
        number *= Decimal(10) ** scale
    except DecimalException:
        return None, None
    currency = (match["currency"] or "").strip().upper()
    unit = (match["unit"] or "").lower()
    if currency and unit:
        return None, None
    return (
        number,
        {
            "$": "USD",
            "€": "EUR",
            "£": "GBP",
            "%": "percent",
            "percentage points": "percentage_points",
        }.get(currency or unit, currency or unit)
        or None,
    )


def validate_quantities(assertion: dict, packet_context: dict) -> dict:
    """Verify every literal quantity in a structured account/calculation assertion."""
    statement = str(assertion.get("statement") or "")
    tokens = list(QUANTITY_RE.finditer(statement))
    bindings = assertion.get("quantity_bindings") or []
    catalog = {
        item.get("measurement_ref"): item
        for item in (packet_context.get("quantity_measurements") or [])
        if isinstance(item, dict)
    }
    errors: list[str] = []
    verified: list[dict] = []
    covered: set[int] = set()
    dimensions = (
        "unit",
        "basis",
        "subject_ref",
        "snapshot_ref",
        "as_of",
        "period_start",
        "period_end",
    )
    for binding in bindings:
        if not isinstance(binding, dict):
            errors.append("invalid_binding")
            continue
        ref = binding.get("measurement_ref")
        source = catalog.get(ref)
        if source is None:
            errors.append("unknown_measurement")
            continue
        quote = str(binding.get("quote") or "").strip()
        matches = [
            i for i, token in enumerate(tokens) if token.group().strip() == quote
        ]
        if not matches:
            errors.append("quote_not_a_complete_quantity_in_statement")
            continue
        number, explicit_unit = parse_quantity(quote)
        claimed = finite_decimal(binding.get("value"))
        expected = finite_decimal(source.get("value"))
        problems = []
        if (
            number is None
            or claimed is None
            or expected is None
            or not number == claimed == expected
        ):
            problems.append("value_mismatch")
        if explicit_unit and explicit_unit != source.get("unit"):
            problems.append("quoted_unit_mismatch")
        for key in dimensions:
            if key not in binding or binding[key] != source.get(key):
                problems.append(f"{key}_mismatch")
        if source.get("status") != "complete":
            problems.append(f"measurement_{source.get('status', 'unverified')}")
        if problems:
            errors.extend(problems)
            continue
        occurrence = binding.get("occurrence")
        if (
            isinstance(occurrence, bool)
            or not isinstance(occurrence, int)
            or not 0 <= occurrence < len(matches)
        ):
            errors.append("invalid_quantity_occurrence")
            continue
        covered.add(matches[occurrence])
        verified.append({"quote": quote, **source})
    if tokens and len(covered) != len(tokens):
        errors.append("unbound_quantity")
    if not bindings:
        errors.append("missing_quantity_bindings")
    return {
        "status": "verified" if not errors else "unverified",
        "verified": not errors,
        "issues": sorted(set(errors)),
        "measurements": verified,
    }


async def portfolio_measurements(
    session, holdings: list[dict], cash, performance=None
) -> list[dict]:
    """Adapt existing ledger/risk/attribution receipts, never cached prose, to quantities."""
    from investos.schemas.portfolio_risk import RiskWatchPolicy
    from investos.services.portfolio_risk import PortfolioRiskService, evaluate_packet

    observed = datetime.now(UTC).isoformat()
    ledger = sorted(holdings, key=lambda item: item["security_id"])
    ledger_ref = hashlib.sha256(
        json.dumps(
            {
                "holdings": ledger,
                "cash_id": str(cash.id) if cash else None,
                "cash_balance": str(cash.balance_after) if cash else None,
            },
            sort_keys=True,
            default=str,
        ).encode()
    ).hexdigest()
    output = [
        measurement(
            value=item["quantity"],
            unit=(
                "shares"
                if item.get("instrument_type") in {"common_stock", "adr", "etf"}
                else "instrument_units"
            ),
            basis="recorded_ledger_quantity",
            subject_ref=f"security:{item['security_id']}",
            snapshot_ref=ledger_ref,
            as_of=observed,
            source_ref=f"ledger:{ledger_ref}",
        )
        for item in ledger
    ]
    if cash is not None:
        output.append(
            measurement(
                value=cash.balance_after,
                unit="USD",
                basis="reconstructed_cash_not_broker_buying_power",
                subject_ref="portfolio",
                snapshot_ref=ledger_ref,
                as_of=observed,
                source_ref=f"cash_entry:{cash.id}",
                notes=["Ledger balance, not broker-confirmed purchasing capacity."],
            )
        )
    row = await PortfolioRiskService(session).latest()
    if row is not None:
        packet = (
            row.shock_parameters_json
            if isinstance(row.shock_parameters_json, dict)
            else {}
        )
        recorded = packet.get("positions", [])

        def identity(items):
            return sorted(
                (
                    str(item.get("security_id")),
                    (
                        str(number.normalize())
                        if (number := finite_decimal(item.get("quantity"))) is not None
                        else None
                    ),
                    str(item.get("direction")),
                )
                for item in items
            )

        same_book = identity(ledger) == identity(recorded)
        try:
            replay = evaluate_packet(
                {**packet, "cutoff": observed},
                max_age_hours=RiskWatchPolicy.model_fields["max_age_hours"].default,
            )
        except (KeyError, TypeError, ValueError, OverflowError):
            replay = {
                "issues": [
                    "The recorded risk capture could not be replayed for quantity verification."
                ]
            }
        complete_mark = replay.get("valuation_complete") is True
        status = (
            "complete"
            if complete_mark and same_book
            else "book_changed" if not same_book else "partial_or_stale"
        )
        capture_ref = f"risk_capture:{row.id}:{ledger_ref}"
        kwargs = dict(
            snapshot_ref=capture_ref,
            as_of=packet.get("cutoff"),
            period_end=replay.get("latest_session_date"),
            status=status,
            source_ref=f"risk_capture:{row.id}",
            notes=replay.get("issues", []),
        )
        output.append(
            measurement(
                value=replay.get("covered_value"),
                unit="USD",
                basis="captured_close_holdings_value_excluding_cash",
                subject_ref="portfolio",
                **kwargs,
            )
        )
        for item in replay.get("positions", []):
            output.append(
                measurement(
                    value=item.get("weight_pct"),
                    unit="percent",
                    basis="position_close_mark_divided_by_captured_holdings_mark",
                    subject_ref=f"security:{item['security_id']}",
                    **kwargs,
                )
            )
    if performance is not None:
        p = (
            performance.model_dump(mode="json")
            if hasattr(performance, "model_dump")
            else performance
        )
        complete = (
            p.get("total_positions", 0) > 0
            and p.get("covered_positions") == p.get("total_positions")
            and not p.get("unavailable_tickers")
            and all(
                item.get("data_status", "complete") == "complete"
                for item in p.get("items", [])
            )
        )
        snapshot = hashlib.sha256(json.dumps(p, sort_keys=True).encode()).hexdigest()
        for key, unit, basis in [
            ("gain", "USD", "dated_cash_flow_adjusted_gain"),
            ("return_pct", "percent", "dated_modified_dietz_return"),
            ("benchmark_return_pct", "percent", "dated_benchmark_return"),
            (
                "active_return_pct",
                "percentage_points",
                "dated_portfolio_minus_benchmark_return",
            ),
            ("net_flow", "USD", "dated_net_external_flow"),
        ]:
            output.append(
                measurement(
                    value=p.get(key),
                    unit=unit,
                    basis=basis,
                    subject_ref=(
                        f"benchmark:{p.get('benchmark_ticker')}"
                        if key == "benchmark_return_pct"
                        else "portfolio"
                    ),
                    snapshot_ref=snapshot,
                    as_of=p.get("as_of"),
                    period_start=p.get("period_start"),
                    period_end=p.get("as_of"),
                    status="complete" if complete else "partial",
                    source_ref=f"attribution:{snapshot}",
                    notes=[str(p.get("method") or "Attribution method not recorded.")],
                )
            )
    return output


def ledger_holdings(rows) -> list[dict]:
    return [
        {
            "security_id": str(row[1].id),
            "quantity": str(row[0].quantity),
            "direction": row[0].direction,
            "instrument_type": row[1].instrument_type,
        }
        for row in rows
        if row[0].list_type == "holding" and row[0].quantity != 0
    ]


def financial_quotes(text: str) -> set[str]:
    return {
        token.group().strip()
        for token in QUANTITY_RE.finditer(text)
        if token["currency"] or token["unit"] in {"%", "percent", "percentage points"}
    }


def audit_numeric_narrative(texts: list[str], assertions: list[dict]) -> dict:
    """A correct assertion list must not launder a different amount in the answer."""
    supported = set()
    for assertion in assertions:
        if (
            assertion.get("status") == "corroborated"
            and assertion.get("evidence_basis") == "retrieved_source"
        ):
            supported.update(
                parse_quantity(quote)
                for quote in financial_quotes(str(assertion.get("statement") or ""))
            )
        check = assertion.get("quantity_verification") or {}
        if check.get("verified"):
            supported.update(
                (finite_decimal(item.get("value")), item.get("unit"))
                for item in check.get("measurements", [])
            )
    unbound = sorted(
        quote
        for quote in set().union(*(financial_quotes(text) for text in texts))
        if parse_quantity(quote) not in supported
    )
    return {
        "status": "verified" if not unbound else "unverified",
        "verified": not unbound,
        "unbound_financial_quantities": unbound,
    }


def qualify_numeric_message(message: str, result: dict) -> str:
    if (result.get("quantity_grounding") or {}).get("verified") is False:
        return (
            "**Numerical claims need verification.** The generated amounts or percentages below are not fully supported by the supplied measurements; they were not accepted as verified guidance.\n\n"
            + message
        )
    return message
