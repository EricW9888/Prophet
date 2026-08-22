from datetime import UTC, datetime, timedelta

from investos.services.market_data import MarketDataService


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
