from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from investos.services.risk import RiskService


def _transaction(
    *, action: str, quantity: float, price: float | None, executed_at: datetime
):
    return SimpleNamespace(
        action=action,
        quantity=quantity,
        price=price,
        executed_at=executed_at,
    )


def test_modified_dietz_matches_simple_buy_and_hold_return():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = start + timedelta(days=30)

    result = RiskService._modified_dietz_values(
        start_quantity=10,
        end_quantity=10,
        start_price=10,
        end_price=11,
        transactions=[],
        period_start=start,
        period_end=end,
    )

    assert result["gain"] == pytest.approx(10)
    assert result["denominator"] == pytest.approx(100)
    assert result["return_pct"] == pytest.approx(10)


def test_modified_dietz_time_weights_midperiod_purchase():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = start + timedelta(days=30)
    purchase = _transaction(
        action="buy",
        quantity=10,
        price=10,
        executed_at=start + timedelta(days=15),
    )

    result = RiskService._modified_dietz_values(
        start_quantity=0,
        end_quantity=10,
        start_price=10,
        end_price=11,
        transactions=[purchase],
        period_start=start,
        period_end=end,
    )

    assert result["net_flow"] == pytest.approx(100)
    assert result["gain"] == pytest.approx(10)
    assert result["denominator"] == pytest.approx(50)
    assert result["return_pct"] == pytest.approx(20)
    assert result["capital_return_pct"] == pytest.approx(10)


def test_modified_dietz_preserves_realized_and_open_gain_after_sale():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = start + timedelta(days=30)
    sale = _transaction(
        action="sell",
        quantity=5,
        price=11,
        executed_at=start + timedelta(days=15),
    )

    result = RiskService._modified_dietz_values(
        start_quantity=10,
        end_quantity=5,
        start_price=10,
        end_price=12,
        transactions=[sale],
        period_start=start,
        period_end=end,
    )

    assert result["net_flow"] == pytest.approx(-55)
    assert result["gain"] == pytest.approx(15)
    assert result["denominator"] == pytest.approx(72.5)
    assert result["return_pct"] == pytest.approx((15 / 72.5) * 100)
    assert result["capital_return_pct"] == pytest.approx(15)


def test_quantity_reversal_handles_buys_sells_and_splits_in_reverse_order():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    transactions = [
        _transaction(action="buy", quantity=5, price=10, executed_at=start),
        _transaction(
            action="split",
            quantity=2,
            price=None,
            executed_at=start + timedelta(days=5),
        ),
        _transaction(
            action="sell", quantity=4, price=12, executed_at=start + timedelta(days=10)
        ),
    ]

    assert RiskService._reverse_start_quantity(16, transactions) == pytest.approx(5)
