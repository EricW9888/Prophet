from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from investos.services.paper_broker import (
    LocalPaperBroker,
    PaperAccountEventRequest,
    PaperBrokerPolicy,
    PaperOrderRequest,
    PaperRecordedFillRequest,
)


def account_state(*, cash: float = 1_000.0, quantity: float = 2.0) -> dict:
    return {
        "cash": cash,
        "cash_reserved": 0.0,
        "positions": [
            {
                "security_id": "00000000-0000-0000-0000-000000000001",
                "quantity": quantity,
                "avg_cost_basis": 80.0,
                "list_type": "holding",
            }
        ],
    }


def order_request(
    *,
    security_id,
    side: str,
    quantity: str,
    price: str = "100",
    session: str = "regular",
) -> PaperOrderRequest:
    return PaperOrderRequest(
        security_id=security_id,
        ticker="TEST",
        side=side,
        quantity=Decimal(quantity),
        reference_price=Decimal(price),
        quote_session=session,
        submitted_at=datetime(2026, 7, 10, 18, 0, tzinfo=UTC),
        quote_time=datetime(2026, 7, 10, 18, 0, tzinfo=UTC),
        rationale="Source-backed paper order.",
        checkpoint_index=1,
        client_order_id=f"test:{uuid4()}",
        evidence_refs=("fact:1",),
    )


def test_paper_broker_fill_is_only_path_that_mutates_cash_and_positions():
    security_id = uuid4()
    state = {"cash": 1_000.0, "cash_reserved": 0.0, "positions": []}
    broker = LocalPaperBroker(
        PaperBrokerPolicy(
            slippage_bps=Decimal("10"),
            max_buy_order_pct_equity=Decimal("100"),
        )
    )

    execution = broker.submit_market_order(
        state=state,
        request=order_request(security_id=security_id, side="buy", quantity="2"),
        account_equity=Decimal("1000"),
        cash_reserve=Decimal("100"),
    )

    assert execution.status == "filled"
    assert execution.fill is not None
    assert execution.fill["price"] == 100.1
    assert execution.state["cash"] == 799.8
    assert execution.state["positions"][0]["quantity"] == 2.0
    assert execution.state["positions"][0]["current_price"] == 100.1
    assert state == {"cash": 1_000.0, "cash_reserved": 0.0, "positions": []}


def test_paper_broker_rejects_oversell_without_mutating_account():
    security_id = uuid4()
    state = {
        "cash": 100.0,
        "cash_reserved": 0.0,
        "positions": [
            {
                "security_id": str(security_id),
                "quantity": 1.0,
                "avg_cost_basis": 80.0,
                "list_type": "holding",
            }
        ],
    }
    broker = LocalPaperBroker(PaperBrokerPolicy())

    execution = broker.submit_market_order(
        state=state,
        request=order_request(security_id=security_id, side="sell", quantity="2"),
        account_equity=Decimal("200"),
        cash_reserve=Decimal("0"),
    )

    assert execution.status == "rejected"
    assert execution.rejection_reason == "insufficient_position_quantity"
    assert execution.fill is None
    assert execution.state["cash"] == 100.0
    assert execution.state["positions"][0]["quantity"] == 1.0


def test_after_hours_order_is_accepted_and_reserves_cash_until_regular_session():
    security_id = uuid4()
    broker = LocalPaperBroker(
        PaperBrokerPolicy(max_buy_order_pct_equity=Decimal("100"))
    )
    state = {"cash": 1_000.0, "cash_reserved": 0.0, "positions": []}
    request = order_request(
        security_id=security_id,
        side="buy",
        quantity="2",
        session="post_market",
    )

    accepted = broker.submit_market_order(
        state=state,
        request=request,
        account_equity=Decimal("1000"),
        cash_reserve=Decimal("0"),
    )

    assert accepted.status == "accepted"
    assert accepted.fill is None
    assert accepted.state["cash"] == 1_000.0
    assert accepted.state["cash_reserved"] == 200.0

    regular_request = PaperOrderRequest(
        **{
            **request.__dict__,
            "quote_session": "regular",
            "reference_price": Decimal("101"),
        }
    )
    filled = broker.submit_market_order(
        state=accepted.state,
        request=regular_request,
        account_equity=Decimal("1000"),
        cash_reserve=Decimal("0"),
        existing_reserved_notional=Decimal("200"),
    )

    assert filled.status == "filled"
    assert filled.state["cash_reserved"] == 0.0
    assert filled.state["positions"][0]["quantity"] == 2.0


def test_order_estimate_bounds_model_intent_to_real_cash_and_equity():
    security_id = uuid4()
    broker = LocalPaperBroker(
        PaperBrokerPolicy(
            slippage_bps=Decimal("0"),
            max_buy_order_pct_equity=Decimal("25"),
        )
    )

    quantity, adjustments = broker.estimate_quantity(
        state={"cash": 1_000.0, "cash_reserved": 0.0, "positions": []},
        security_id=security_id,
        side="buy",
        desired_quantity=Decimal("1000000"),
        reference_price=Decimal("100"),
        account_equity=Decimal("1000"),
        cash_reserve=Decimal("100"),
    )

    assert quantity == Decimal("2.50000000")
    assert adjustments


def test_paper_broker_rejects_missing_executable_quote():
    security_id = uuid4()
    broker = LocalPaperBroker(PaperBrokerPolicy())

    execution = broker.submit_market_order(
        state={"cash": 1_000.0, "cash_reserved": 0.0, "positions": []},
        request=order_request(
            security_id=security_id,
            side="buy",
            quantity="1",
            price="0",
            session="unavailable",
        ),
        account_equity=Decimal("1000"),
        cash_reserve=Decimal("0"),
    )

    assert execution.status == "rejected"
    assert execution.rejection_reason == "executable_quote_required"
    assert execution.fill is None


def account_event_request(
    *,
    security_id,
    event_type: str,
    occurred_at: datetime | None = None,
    split_ratio: str | None = None,
    dividend_per_share: str | None = None,
) -> PaperAccountEventRequest:
    return PaperAccountEventRequest(
        security_id=security_id,
        ticker="TEST",
        event_type=event_type,
        occurred_at=occurred_at or datetime(2026, 7, 10, 18, 0, tzinfo=UTC),
        source_transaction_id=uuid4(),
        split_ratio=None if split_ratio is None else Decimal(split_ratio),
        dividend_per_share=(
            None if dividend_per_share is None else Decimal(dividend_per_share)
        ),
        derivation="test_source_transaction",
    )


def test_split_adjusts_pre_event_mark_without_changing_position_value_or_input():
    security_id = uuid4()
    state = {
        "cash": 100.0,
        "cash_reserved": 0.0,
        "positions": [
            {
                "security_id": str(security_id),
                "quantity": 2.0,
                "avg_cost_basis": 80.0,
                "current_price": 100.0,
                "marked_at": "2026-07-09T18:00:00+00:00",
                "list_type": "holding",
            }
        ],
    }
    broker = LocalPaperBroker(PaperBrokerPolicy())

    execution = broker.apply_account_event(
        state=state,
        request=account_event_request(
            security_id=security_id,
            event_type="split",
            split_ratio="4",
        ),
    )

    position = execution.state["positions"][0]
    assert execution.status == "applied"
    assert execution.quantity_before == Decimal("2.0")
    assert execution.quantity_after == Decimal("8.0")
    assert position["avg_cost_basis"] == 20.0
    assert position["current_price"] == 25.0
    assert position["quantity"] * position["current_price"] == 200.0
    assert state["positions"][0]["quantity"] == 2.0


def test_split_does_not_double_adjust_a_post_event_market_mark():
    security_id = uuid4()
    state = {
        "cash": 100.0,
        "positions": [
            {
                "security_id": str(security_id),
                "quantity": 2.0,
                "avg_cost_basis": 80.0,
                "current_price": 25.0,
                "marked_at": "2026-07-11T18:00:00+00:00",
            }
        ],
    }
    execution = LocalPaperBroker(PaperBrokerPolicy()).apply_account_event(
        state=state,
        request=account_event_request(
            security_id=security_id,
            event_type="split",
            split_ratio="4",
        ),
    )

    position = execution.state["positions"][0]
    assert position["quantity"] == 8.0
    assert position["avg_cost_basis"] == 20.0
    assert position["current_price"] == 25.0


def test_dividend_credits_cash_for_paper_quantity_without_changing_real_input():
    security_id = uuid4()
    state = {
        "cash": 100.0,
        "positions": [
            {
                "security_id": str(security_id),
                "quantity": 2.5,
                "avg_cost_basis": 80.0,
            }
        ],
    }
    execution = LocalPaperBroker(PaperBrokerPolicy()).apply_account_event(
        state=state,
        request=account_event_request(
            security_id=security_id,
            event_type="dividend",
            dividend_per_share="1.20",
        ),
    )

    assert execution.status == "applied"
    assert execution.amount == Decimal("3.0000")
    assert execution.state["cash"] == 103.0
    assert execution.quantity_after == Decimal("2.5")
    assert state["cash"] == 100.0


def test_account_event_without_a_held_security_is_recordable_but_not_applicable():
    execution = LocalPaperBroker(PaperBrokerPolicy()).apply_account_event(
        state={"cash": 100.0, "positions": []},
        request=account_event_request(
            security_id=uuid4(),
            event_type="dividend",
            dividend_per_share="1.20",
        ),
    )

    assert execution.status == "not_applicable"
    assert execution.state["cash"] == 100.0
    assert execution.amount == Decimal("0")


def test_invalid_account_events_are_rejected_without_mutating_state():
    security_id = uuid4()
    state = account_state()
    state["positions"][0]["security_id"] = str(security_id)
    broker = LocalPaperBroker(PaperBrokerPolicy())

    split = broker.apply_account_event(
        state=state,
        request=account_event_request(
            security_id=security_id,
            event_type="split",
            split_ratio="0",
        ),
    )
    dividend = broker.apply_account_event(
        state=state,
        request=account_event_request(
            security_id=security_id,
            event_type="dividend",
        ),
    )

    assert split.status == "rejected"
    assert split.rejection_reason == "split_ratio_must_be_positive"
    assert dividend.status == "rejected"
    assert dividend.rejection_reason == "dividend_per_share_required"
    assert split.state["positions"][0]["quantity"] == 2.0
    assert dividend.state["cash"] == 1_000.0


def recorded_fill_request(
    *,
    security_id,
    side: str,
    quantity: str,
    price: str,
    gross_notional: str,
    fee: str = "0",
) -> PaperRecordedFillRequest:
    return PaperRecordedFillRequest(
        security_id=security_id,
        ticker="TEST",
        side=side,
        quantity=Decimal(quantity),
        price=Decimal(price),
        gross_notional=Decimal(gross_notional),
        fee=Decimal(fee),
        filled_at=datetime(2026, 7, 10, 19, 0, tzinfo=UTC),
    )


def test_recorded_fill_replay_preserves_corrected_split_chronology_and_input():
    security_id = uuid4()
    state = {
        "cash": 1_000.0,
        "cash_reserved": 0.0,
        "positions": [
            {
                "security_id": str(security_id),
                "quantity": 10.0,
                "avg_cost_basis": 100.0,
                "current_price": 100.0,
                "marked_at": "2026-07-10T17:00:00+00:00",
            }
        ],
    }
    broker = LocalPaperBroker(PaperBrokerPolicy())
    split = broker.apply_account_event(
        state=state,
        request=account_event_request(
            security_id=security_id,
            event_type="split",
            split_ratio="2",
            occurred_at=datetime(2026, 7, 10, 18, 0, tzinfo=UTC),
        ),
    )

    replay = broker.replay_recorded_fill(
        state=split.state,
        request=recorded_fill_request(
            security_id=security_id,
            side="sell",
            quantity="5",
            price="60",
            gross_notional="300",
            fee="1",
        ),
    )

    assert replay.status == "applied"
    assert replay.state["cash"] == 1_299.0
    assert replay.state["positions"][0]["quantity"] == 15.0
    assert replay.state["positions"][0]["avg_cost_basis"] == 50.0
    assert replay.state["positions"][0]["current_price"] == 60.0
    assert state["positions"][0]["quantity"] == 10.0
    assert state["cash"] == 1_000.0


def test_recorded_fill_replay_rejects_impossible_sale_without_mutating_state():
    security_id = uuid4()
    state = {
        "cash": 100.0,
        "cash_reserved": 0.0,
        "positions": [
            {
                "security_id": str(security_id),
                "quantity": 1.0,
                "avg_cost_basis": 80.0,
            }
        ],
    }

    replay = LocalPaperBroker(PaperBrokerPolicy()).replay_recorded_fill(
        state=state,
        request=recorded_fill_request(
            security_id=security_id,
            side="sell",
            quantity="2",
            price="100",
            gross_notional="200",
        ),
    )

    assert replay.status == "rejected"
    assert replay.rejection_reason == "recorded_fill_insufficient_position_quantity"
    assert replay.state["positions"][0]["quantity"] == 1.0
    assert state["positions"][0]["quantity"] == 1.0


def test_recorded_fill_replay_rejects_an_inconsistent_immutable_notional():
    replay = LocalPaperBroker(PaperBrokerPolicy()).replay_recorded_fill(
        state={"cash": 1_000.0, "cash_reserved": 0.0, "positions": []},
        request=recorded_fill_request(
            security_id=uuid4(),
            side="buy",
            quantity="2",
            price="100",
            gross_notional="250",
        ),
    )

    assert replay.status == "rejected"
    assert replay.rejection_reason == "recorded_fill_notional_mismatch"
    assert replay.state["cash"] == 1_000.0
    assert replay.state["positions"] == []
