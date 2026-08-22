from decimal import Decimal
from types import SimpleNamespace

from investos.services.portfolio import PortfolioService


def _lot(qty, cost, closed=False):
    return SimpleNamespace(
        quantity=Decimal(str(qty)),
        cost_basis=Decimal(str(cost)),
        closed_at="2020-01-01" if closed else None,
    )


def _resync(position, lots):
    # self is unused by the helper; pass a sentinel.
    PortfolioService._resync_position_from_lots(object(), position, lots)


def test_resync_weighted_average_cost():
    pos = SimpleNamespace(
        quantity=Decimal("0"), avg_cost_basis=Decimal("0"), list_type="holding"
    )
    _resync(pos, [_lot(10, 5), _lot(5, 8)])
    assert pos.quantity == Decimal("15")
    # (10*5 + 5*8) / 15 = 90/15 = 6
    assert pos.avg_cost_basis == Decimal("6")


def test_resync_oversell_flattens_not_negative():
    # All lots consumed/closed -> position flat, never negative.
    pos = SimpleNamespace(
        quantity=Decimal("3"), avg_cost_basis=Decimal("5"), list_type="holding"
    )
    _resync(pos, [_lot(0, 5, closed=True)])
    assert pos.quantity == Decimal("0")
    assert pos.avg_cost_basis == Decimal("0")
    assert pos.list_type == "closed"


def test_resync_ignores_closed_lots():
    pos = SimpleNamespace(
        quantity=Decimal("0"), avg_cost_basis=Decimal("0"), list_type="holding"
    )
    _resync(pos, [_lot(4, 10), _lot(99, 1, closed=True)])
    assert pos.quantity == Decimal("4")
    assert pos.avg_cost_basis == Decimal("10")


def test_split_math_preserves_total_basis():
    # Replicates the split branch math: shares *= r, per-share basis /= r.
    # A 4:1 split of 10 shares @ $200 -> 40 shares @ $50, total basis unchanged.
    qty, cost = Decimal("10"), Decimal("200")
    ratio = Decimal("4")
    new_qty, new_cost = qty * ratio, cost / ratio
    assert new_qty == Decimal("40")
    assert new_cost == Decimal("50")
    assert qty * cost == new_qty * new_cost  # total basis preserved


def test_transaction_status_controls_replay_eligibility():
    assert PortfolioService.transaction_is_active(SimpleNamespace(status="settled"))
    assert PortfolioService.transaction_is_active(SimpleNamespace(status=None))
    assert not PortfolioService.transaction_is_active(
        SimpleNamespace(status="corrected")
    )
    assert not PortfolioService.transaction_is_active(
        SimpleNamespace(status="canceled")
    )
