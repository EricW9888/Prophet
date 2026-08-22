from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

from investos.services.portfolio import PortfolioService


class _Result:
    def __init__(self, items):
        self._items = items

    def scalars(self):
        return self

    def all(self):
        return self._items


class _FakeSession:
    """Returns positions on the first execute, transactions on the second."""

    def __init__(self, positions, transactions):
        self._queue = [positions, transactions]

    async def execute(self, _stmt):
        return _Result(self._queue.pop(0))

    async def flush(self):
        return None


def _pos(list_type, added_at, sec):
    return SimpleNamespace(
        id=uuid4(),
        security_id=sec,
        direction="long",
        list_type=list_type,
        added_at=added_at,
        quantity=Decimal("0"),
        market_value=Decimal("0"),
        unrealized_pnl=Decimal("0"),
    )


async def _run(positions, transactions):
    svc = PortfolioService.__new__(PortfolioService)
    svc.session = _FakeSession(positions, transactions)
    await svc._consolidate_trade_positions()


async def test_buys_and_sells_reunite_on_one_row():
    sec = uuid4()
    closed_row = _pos("closed", datetime(2025, 1, 1), sec)  # holds the BUY
    holding_row = _pos("holding", datetime(2025, 2, 1), sec)  # holds the SELL
    buy = SimpleNamespace(position_id=closed_row.id)
    sell = SimpleNamespace(position_id=holding_row.id)

    await _run([closed_row, holding_row], [buy, sell])

    # A holding row is the canonical target; both txns now point at it.
    canonical = holding_row
    assert buy.position_id == canonical.id
    assert sell.position_id == canonical.id
    assert canonical.list_type == "holding"
    assert closed_row.list_type == "closed"
    assert closed_row.quantity == Decimal("0")


async def test_distinct_securities_are_not_merged():
    a, b = uuid4(), uuid4()
    pa = _pos("holding", datetime(2025, 1, 1), a)
    pb = _pos("holding", datetime(2025, 1, 1), b)
    ta = SimpleNamespace(position_id=pa.id)
    tb = SimpleNamespace(position_id=pb.id)

    await _run([pa, pb], [ta, tb])

    assert ta.position_id == pa.id
    assert tb.position_id == pb.id


async def test_single_row_is_left_untouched():
    sec = uuid4()
    only = _pos("holding", datetime(2025, 1, 1), sec)
    t = SimpleNamespace(position_id=only.id)

    await _run([only], [t])

    assert t.position_id == only.id
    assert only.list_type == "holding"
