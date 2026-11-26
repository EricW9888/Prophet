"""Today's change is measured from the previous regular close."""

from datetime import datetime
from uuid import uuid4
from zoneinfo import ZoneInfo

from investos.services.market_data import MarketDataService
from tests.test_quote_valuation import mark, receipt

NEW_YORK = ZoneInfo("America/New_York")


def _epoch(day: int, hour: int = 16) -> int:
    return int(datetime(2026, 11, day, hour, tzinfo=NEW_YORK).timestamp())


def _chart(regular_day: int, bars: dict[int, float | None], regular_price=101.0):
    return {
        "meta": {
            "regularMarketTime": _epoch(regular_day),
            "regularMarketPrice": regular_price,
        },
        "timestamp": [_epoch(day, 9) for day in bars],
        "indicators": {"quote": [{"close": list(bars.values())}]},
    }


def test_during_and_after_the_session_the_reference_is_the_prior_bar():
    chart = _chart(25, {20: 96.0, 21: 97.0, 24: 99.0, 25: 101.0})
    for session in ("regular", "post_market"):
        assert MarketDataService._previous_close(chart, NEW_YORK, session) == (
            99.0,
            "2026-11-24",
        )


def test_before_the_open_the_reference_is_the_latest_regular_close():
    chart = _chart(25, {24: 99.0, 25: 101.0}, regular_price=101.0)
    assert MarketDataService._previous_close(chart, NEW_YORK, "pre_market") == (
        101.0,
        "2026-11-25",
    )


def test_while_closed_the_last_session_is_compared_with_the_one_before():
    # Saturday: the price is Friday's close, measured from Thursday's.
    chart = _chart(20, {18: 95.0, 19: 98.0, 20: 100.0})
    assert MarketDataService._previous_close(chart, NEW_YORK, "historical_close") == (
        98.0,
        "2026-11-19",
    )


def test_missing_closes_are_skipped_and_no_history_gives_none():
    chart = _chart(25, {21: 97.0, 24: None, 25: 101.0})
    assert MarketDataService._previous_close(chart, NEW_YORK, "regular") == (
        97.0,
        "2026-11-21",
    )
    assert MarketDataService._previous_close(
        _chart(25, {25: 101.0}), NEW_YORK, "regular"
    ) == (None, None)
    assert MarketDataService._previous_close({"meta": {}}, NEW_YORK, "regular") == (
        None,
        None,
    )


def test_valuation_reports_the_change_for_the_shares_held_now():
    identifier = uuid4()
    source = {
        **receipt(identifier, quantity="2", price="25"),
        "previous_close": 20.0,
        "previous_close_on": "2026-11-24",
    }
    result = mark(source, identifier)
    assert result["day_change"] == 10.0
    assert result["day_change_pct"] == 25.0
    assert result["previous_close_on"] == "2026-11-24"


def test_no_reference_or_an_unusable_quote_reports_no_change():
    identifier = uuid4()
    assert mark(receipt(identifier), identifier)["day_change"] is None
    stale = {
        **receipt(identifier),
        "previous_close": 20.0,
        "currency": "EUR",
    }
    result = mark(stale, identifier)
    assert result["status"] == "unavailable"
    assert result["day_change"] is None and result["previous_close_on"] is None
