from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from investos.services.market_data import MarketDataService


@pytest.mark.asyncio
async def test_quote_time_matches_the_price_session(monkeypatch):
    meta = {
        "symbol": "SYN",
        "currency": "USD",
        "marketState": "POST",
        "regularMarketPrice": 19,
        "regularMarketTime": 1_800_000_000,
        "postMarketPrice": 20,
        "postMarketTime": 1_800_001_000,
    }

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def get(self, *_args, **_kwargs):
            return SimpleNamespace(
                raise_for_status=lambda: None,
                json=lambda: {"chart": {"result": [{"meta": meta}]}},
            )

    monkeypatch.setattr(
        "investos.services.market_data.httpx.AsyncClient", lambda **_kwargs: Client()
    )
    service = MarketDataService(None)
    first = await service.get_live_price("SYN")
    assert first["price"] == 20 and first["session"] == "post_market"
    assert (
        datetime.fromisoformat(first["quote_time"]).timestamp()
        == meta["postMarketTime"]
    )
    meta.pop("postMarketTime")
    assert (await service.get_live_price("SYN"))["quote_time"] is None


def test_market_signal_snapshot_preserves_point_in_time_observations():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    observations = [
        {
            "timestamp": start + timedelta(days=index),
            "close": 100.0 + index,
            "volume": 1_000.0 if index < 60 else 2_000.0,
        }
        for index in range(65)
    ]

    snapshot = MarketDataService.build_signal_snapshot(
        ticker="xyz",
        observations=observations,
    )

    assert snapshot["ticker"] == "XYZ"
    assert snapshot["as_of"] == observations[-1]["timestamp"].isoformat()
    assert snapshot["signal_ref"] == "market:XYZ:2026-03-06"
    assert snapshot["observations"] == 65
    assert snapshot["return_5d_pct"] > 0
    assert snapshot["return_20d_pct"] > snapshot["return_5d_pct"]
    assert snapshot["moving_average_20d"] is not None
    assert snapshot["moving_average_50d"] is not None
    assert snapshot["drawdown_from_period_high_pct"] == 0.0
    assert snapshot["latest_volume_vs_prior_20d"] > 1.0


def test_market_signal_snapshot_does_not_invent_data_for_empty_series():
    snapshot = MarketDataService.build_signal_snapshot(ticker="xyz", observations=[])

    assert snapshot == {
        "ticker": "XYZ",
        "as_of": None,
        "signal_ref": None,
        "observations": 0,
    }


def test_price_direction_matches_volume_session_even_after_longer_drawdown():
    start = datetime(2025, 1, 1, tzinfo=UTC)
    closes = [150] + [100] * 20 + [105]
    snapshot = MarketDataService.build_signal_snapshot(
        ticker="SYN",
        observations=[
            {
                "timestamp": start + timedelta(days=i),
                "close": close,
                "volume": 3000 if i == 21 else 1000,
            }
            for i, close in enumerate(closes)
        ],
    )
    assert snapshot["return_1d_pct"] == 5
    assert snapshot["drawdown_from_period_high_pct"] < 0
    assert snapshot["latest_volume_vs_prior_20d"] == 3


def test_daily_bar_cannot_use_open_timestamp_as_final_close_availability():
    start = datetime(2025, 1, 1, 14, 30, tzinfo=UTC)
    observations = [
        {
            "timestamp": start + timedelta(days=i),
            "close": 100 - i * 10,
            "completed_at": start + timedelta(days=i, hours=6, minutes=30),
            "available_at": start + timedelta(days=i, hours=6, minutes=31),
        }
        for i in range(2)
    ]
    snapshot = MarketDataService.build_signal_snapshot(
        ticker="SYN",
        observations=observations,
        cutoff=start + timedelta(days=1, hours=1),
    )
    assert snapshot["current_price"] == 100
    assert snapshot["return_1d_pct"] is None
    snapshot = MarketDataService.build_signal_snapshot(
        ticker="SYN",
        observations=observations,
        cutoff=start + timedelta(days=1, hours=7),
    )
    assert snapshot["return_1d_pct"] == -10


def test_split_adjusted_returns_do_not_report_a_mechanical_price_collapse():
    start = datetime(2025, 1, 1, tzinfo=UTC)
    snapshot = MarketDataService.build_signal_snapshot(
        ticker="SYN",
        observations=[
            {"timestamp": start, "close": 100, "adjusted_close": 50},
            {"timestamp": start + timedelta(days=1), "close": 51, "adjusted_close": 51},
        ],
    )
    assert snapshot["current_price"] == 51
    assert snapshot["return_1d_pct"] == 2
    assert snapshot["return_price_basis"] == "provider_adjusted"


@pytest.mark.asyncio
async def test_historical_cutoff_does_not_fetch_todays_revised_history(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Historical capture must not fetch present-day data")

    monkeypatch.setattr("investos.services.market_data.httpx.AsyncClient", forbidden)
    snapshot = await MarketDataService(None).fetch_signal_snapshot(
        "SYN", cutoff=datetime(2025, 1, 1, tzinfo=UTC)
    )
    assert snapshot["status"] == "historical_capture_required"


@pytest.mark.asyncio
async def test_live_adapter_uses_provider_session_end_not_a_fixed_close(monkeypatch):
    class FrozenDateTime(datetime):
        instant = datetime(2025, 5, 21, 16, tzinfo=UTC)

        @classmethod
        def now(cls, tz=None):
            return cls.fromtimestamp(cls.instant.timestamp(), tz=tz or UTC)

    # An early close from the provider, rather than an invented 16:00 NY close.
    start = int(datetime(2025, 5, 21, 13, 30, tzinfo=UTC).timestamp())
    end = int(datetime(2025, 5, 21, 17, tzinfo=UTC).timestamp())
    payload = {
        "chart": {
            "result": [
                {
                    "timestamp": [start - 86400, start],
                    "meta": {
                        "symbol": "SYN",
                        "currency": "USD",
                        "exchangeTimezoneName": "America/New_York",
                        "currentTradingPeriod": {
                            "regular": {"start": start, "end": end}
                        },
                    },
                    "indicators": {
                        "quote": [{"close": [100, 90], "volume": [1000, 3000]}],
                        "adjclose": [{"adjclose": [100, 90]}],
                    },
                }
            ]
        }
    }

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, *args, **kwargs):
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: payload)

    monkeypatch.setattr("investos.services.market_data.datetime", FrozenDateTime)
    monkeypatch.setattr(
        "investos.services.market_data.httpx.AsyncClient", lambda **kwargs: Client()
    )
    before = await MarketDataService(None).fetch_signal_snapshot("SYN")
    assert before["current_price"] == 100
    FrozenDateTime.instant = datetime(2025, 5, 21, 17, 1, tzinfo=UTC)
    after = await MarketDataService(None).fetch_signal_snapshot("SYN")
    assert after["current_price"] == 90
    assert after["completed_at"] == "2025-05-21T17:00:00+00:00"
    assert after["retrieved_at"] == after["available_at"]


@pytest.mark.asyncio
async def test_partial_live_quote_refresh_preserves_missing_value_and_normalizes_all_weights(
    monkeypatch,
):
    positions = [
        SimpleNamespace(
            security=SimpleNamespace(ticker=ticker, entity=None),
            quantity=Decimal("10"),
            avg_cost_basis=Decimal("5"),
            current_price=Decimal(str(price)),
            market_value=Decimal(str(price * 10)),
            weight_pct=weight,
        )
        for ticker, price, weight in [("SYN-A", 5, 25), ("SYN-B", 15, 75)]
    ]

    class Session:
        async def execute(self, statement):
            return SimpleNamespace(
                scalars=lambda: SimpleNamespace(all=lambda: positions)
            )

        async def commit(self):
            pass

    monkeypatch.setattr(
        "investos.services.market_data.RuntimeSettingsStore.load",
        lambda: SimpleNamespace(
            market_data=SimpleNamespace(enabled=True, provider="yahoo_finance")
        ),
    )

    async def quotes(self, tickers):
        return {"SYN-A": {"price": 10, "currency": "USD", "reported_symbol": "SYN-A"}}

    monkeypatch.setattr(MarketDataService, "fetch_quotes", quotes)
    result = await MarketDataService(Session()).refresh_live_prices()
    assert result["detail"] == "partial"
    assert result["unavailable_tickers"] == ["SYN-B"]
    assert positions[1].market_value == 150
    assert [p.weight_pct for p in positions] == [40, 60]
    assert positions[1].current_price == 15


@pytest.mark.asyncio
async def test_one_failed_symbol_does_not_discard_other_live_quotes(monkeypatch):
    async def fetch(self, tickers):
        if tickers == ["SYN-B"]:
            raise ValueError("Synthetic provider failure")
        return {tickers[0]: {"price": 10}}

    monkeypatch.setattr(MarketDataService, "_fetch_quotes", fetch)
    quotes = await MarketDataService(None).fetch_quotes(["syn-a", "SYN-B", " SYN-A "])
    assert quotes == {"SYN-A": {"price": 10}}
