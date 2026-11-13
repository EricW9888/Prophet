from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from investos.core.valuation import quote_mark
from investos.db import get_session
from investos.main import app
from investos.models.entity import Entity, Security
from investos.models.portfolio import CashLedgerEntry, Position
from investos.services.market_data import MarketDataService
from investos.services.operating_state import OperatingStateService
from investos.services.retrieval import RetrievalService
from tests.test_investor_workflows import workflow_session


def receipt(security_id, ticker="SYNQ", quantity="2", price="25"):
    now = datetime.now(UTC)
    return {
        "version": 1,
        "security_id": str(security_id),
        "ticker": ticker,
        "instrument_type": "common_stock",
        "quantity": quantity,
        "direction": "long",
        "price": price,
        "quoted_at": (now - timedelta(minutes=2)).isoformat(),
        "observed_at": (now - timedelta(minutes=1)).isoformat(),
        "currency": "USD",
        "provider": "synthetic",
    }


def mark(source, security_id, **changes):
    return quote_mark(
        receipt=source,
        security_id=security_id,
        ticker="SYNQ",
        instrument_type="common_stock",
        quantity=2,
        direction="long",
        cost=25,
        **changes,
    )


@pytest.mark.parametrize(
    "fault",
    [
        "missing",
        "missing_time",
        "naive_time",
        "future_quote",
        "future_observation",
        "stale",
        "currency",
        "ticker",
        "security",
        "quantity",
        "instrument",
        "negative_price",
        "nonfinite",
    ],
)
def test_unusable_quotes_remain_unavailable_instead_of_cached_zero(fault):
    identifier = uuid4()
    source = receipt(identifier)
    now = datetime.now(UTC)
    if fault == "missing":
        source = None
    elif fault == "missing_time":
        source.pop("quoted_at")
    elif fault == "naive_time":
        source["quoted_at"] = "2026-01-01T12:00:00"
    elif fault == "future_quote":
        source["quoted_at"] = (now + timedelta(hours=1)).isoformat()
    elif fault == "future_observation":
        source["observed_at"] = (now + timedelta(hours=1)).isoformat()
    elif fault == "stale":
        source["quoted_at"] = (now - timedelta(days=10)).isoformat()
    elif fault == "currency":
        source["currency"] = "KRW"
    elif fault == "ticker":
        source["ticker"] = "ANOTHER"
    elif fault == "security":
        source["security_id"] = str(uuid4())
    elif fault == "quantity":
        source["quantity"] = "4"
    elif fault == "instrument":
        source["instrument_type"] = "call_option"
    elif fault == "negative_price":
        source["price"] = "-1"
    elif fault == "nonfinite":
        source["price"] = "NaN"
    result = mark(source, identifier)
    assert result["status"] == "unavailable"
    assert result["price"] is result["market_value"] is result["unrealized_pnl"] is None
    assert result["issues"]


def test_measured_zero_and_flat_pnl_are_preserved():
    identifier = uuid4()
    source = receipt(identifier)
    result = mark(source, identifier)
    assert result["market_value"] == 50 and result["unrealized_pnl"] == 0
    zero = mark({**source, "price": "0"}, identifier)
    assert zero["market_value"] == 0 and zero["unrealized_pnl"] == -50
    assert zero["status"] == "ready"
    assert source["price"] == "25"


async def test_normal_refresh_dto_context_and_overview_preserve_partial_coverage(
    workflow_session, monkeypatch
):
    session = workflow_session
    entity = Entity(name="Synthetic quote coverage", entity_type="company")
    session.add(entity)
    await session.flush()
    securities = [
        Security(
            entity_id=entity.id,
            ticker=ticker,
            asset_class="equity",
            instrument_type="common_stock",
        )
        for ticker in ["SYNQ", "SYNM"]
    ]
    session.add_all(securities)
    await session.flush()
    positions = [
        Position(
            security_id=s.id,
            direction="long",
            list_type="holding",
            quantity=2,
            avg_cost_basis=25,
            current_price=999999,
            market_value=1999998,
            unrealized_pnl=1999948,
        )
        for s in securities
    ]
    positions[0].added_at = datetime.now(UTC) - timedelta(days=2)
    positions[1].added_at = datetime.now(UTC) - timedelta(days=1)
    session.add_all(list(reversed(positions)))
    await session.flush()

    async def dependency():
        yield session

    app.dependency_overrides[get_session] = dependency
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://localhost"
        ) as client:
            before = (await client.get("/api/portfolio/overview?refresh=false")).json()
            assert before["total_value"] is None
            assert [row["ticker"] for row in before["holdings"]] == ["SYNQ", "SYNM"]
            assert all(p["market_value"] is None for p in before["holdings"])
            assert before["buying_power"] is None
            source = {
                "price": 25,
                "currency": "USD",
                "reported_symbol": "SYNQ",
                "quote_time": (datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
                "session": "regular",
            }

            async def quotes(self, tickers):
                return {"SYNQ": source}

            monkeypatch.setattr(MarketDataService, "fetch_quotes", quotes)
            await MarketDataService(session).refresh_live_prices()
            result = (await client.get("/api/portfolio/overview?refresh=false")).json()
            assert [row["id"] for row in result["holdings"]] == [
                row["id"] for row in before["holdings"]
            ]
            assert result["valuation_coverage"]["priced_positions"] == 1
            assert result["valuation_coverage"]["covered_market_value"] == 50
            assert result["valuation_coverage"]["unavailable_tickers"] == ["SYNM"]
            assert result["total_value"] is None
            assert (
                positions[1].market_value == 1999998
            ), "Legacy cache retained for audit, not admitted as a mark"
            state = await OperatingStateService(session).portfolio_state_payload()
            assert (
                state["total_market_value"] is None
                and state["remaining_buying_power"] is None
            )
            context = await RetrievalService(session)._portfolio_context(
                subject_id=None, subject_type="portfolio"
            )
            assert context["total_market_value"] is None
            assert context["valuation_coverage"]["priced_positions"] == 1
            rows = (await client.get("/api/portfolio/positions")).json()
            assert sorted(p["current_price"] or -1 for p in rows) == [-1, 25]
            positions[1].list_type = "watchlist"
            session.add(
                CashLedgerEntry(
                    entry_type="deposit",
                    amount=0,
                    balance_after=0,
                    executed_at=datetime.now(UTC),
                )
            )
            await session.flush()
            complete = (
                await client.get("/api/portfolio/overview?refresh=false")
            ).json()
            assert complete["total_value"] == 50 and complete["buying_power"] == 0
            positions[0].quantity = 4
            await session.flush()
            changed = (await client.get("/api/portfolio/overview?refresh=false")).json()
            assert changed["total_value"] is None
    finally:
        app.dependency_overrides.clear()


def test_dto_compares_exact_recorded_shares_before_float_presentation():
    from investos.schemas.portfolio import PositionResponse

    identifier = uuid4()
    exact = Decimal("0.123456789012345678901234")
    payload = {
        "id": uuid4(),
        "security_id": identifier,
        "ticker": "SYNQ",
        "instrument_type": "common_stock",
        "direction": "long",
        "list_type": "holding",
        "quantity": exact,
        "avg_cost_basis": Decimal("25"),
        "added_at": datetime.now(UTC),
        "quote_receipt_json": receipt(identifier, quantity=str(exact)),
    }
    result = PositionResponse.model_validate(payload)
    assert result.valuation["status"] == "ready"
    assert result.recorded_quantity == exact
    assert result.model_dump(mode="json")["quantity"] == float(exact)
    assert "recorded_quantity" not in result.model_dump()
    changed = PositionResponse.model_validate(
        {**payload, "quantity": exact + Decimal("0.000000000000000000000001")}
    )
    assert changed.market_value is None


def test_unrepresentable_calculations_are_unavailable_and_deterministic_chat_preserves_missing_values():
    from investos.services.agent import AgentService

    identifier = uuid4()
    result = mark(receipt(identifier, price="1e999999"), identifier)
    assert result["market_value"] is None
    answer = AgentService(None)._operating_answer_portfolio_state(
        {
            "portfolio_state": {
                "holdings_count": 1,
                "total_market_value": None,
                "remaining_buying_power": None,
                "top_holdings": [
                    {"ticker": "SYNQ", "name": "Synthetic", "market_value": None}
                ],
            }
        }
    )
    assert "unavailable" in answer["assistant_message"]
    assert "$0" not in answer["assistant_message"]
