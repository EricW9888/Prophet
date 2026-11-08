import copy
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from investos.schemas.portfolio_risk import RiskWatchPolicy
from investos.services.market_data import MarketDataService
from investos.services.portfolio_risk import (
    VERSION,
    budget_decision,
    digest,
    evaluate_packet,
    freeze_packet,
)
from investos.services.risk_benchmark import BenchmarkResponsePolicy, run_benchmark


def frozen_packet(*, rising=False, quiet=False):
    """Wholly synthetic exposure, no owner book or historical event inputs."""
    cutoff = datetime(2025, 5, 21, 22, tzinfo=UTC)
    positions = []
    snapshots = {}
    for index, ticker in enumerate(("SYN-A", "SYN-B", "SYN-C")):
        observations = []
        close = 100.0
        for day in range(61):
            timestamp = cutoff - timedelta(days=60 - day, hours=12)
            change = (
                0.002
                if rising
                else (0.0001 if quiet else (-0.06 if day % 13 == 0 else 0.004))
            )
            close *= 1 + change * (1 + index * 0.2)
            observations.append(
                {
                    "timestamp": timestamp,
                    "completed_at": timestamp + timedelta(hours=6),
                    "available_at": timestamp + timedelta(hours=6, minutes=1),
                    "close": close,
                    "adjusted_close": close,
                    "volume": 1000 + day,
                }
            )
        snapshot = MarketDataService.build_signal_snapshot(
            ticker=ticker, observations=observations, cutoff=cutoff
        )
        snapshot.update(
            {
                "currency": "USD",
                "price_basis": "provider_adjusted_current_revision",
                "retrieved_at": (cutoff - timedelta(minutes=1)).isoformat(),
                "provider": "synthetic",
                "reported_symbol": ticker,
            }
        )
        snapshots[ticker] = snapshot
        positions.append(
            {
                "security_id": f"synthetic-security-{index}",
                "ticker": ticker,
                "quantity": 10,
                "direction": "long",
                "is_nonlinear": False,
                "instrument_type": "common_stock",
                "multiplier": 1,
            }
        )
    return {
        "version": VERSION,
        "cutoff": cutoff.isoformat(),
        "positions": positions,
        "snapshots": snapshots,
    }


def test_concentrated_portfolio_stress_preserves_simultaneous_moves():
    packet = frozen_packet()
    result = evaluate_packet(packet)
    assert result["status"] == "ready"
    assert result["matched_returns"] == 60
    assert result["historical_stress_loss"] > 100
    assert result["median_pair_correlation"] == pytest.approx(1)
    assert sum(
        row["worst_session_impact"] for row in result["positions"]
    ) == pytest.approx(-result["worst_observed_loss"], abs=0.02)
    decision = budget_decision(result, RiskWatchPolicy(loss_budget=50))
    assert decision["state"] == "breached"
    assert 0 < decision["uniform_reduction_to_budget_pct"] < 100


@pytest.mark.parametrize("kind", ["rising", "quiet"])
def test_non_event_controls_do_not_invent_selling(kind):
    result = evaluate_packet(frozen_packet(**{kind: True}))
    assert result["status"] == "ready"
    assert result["historical_stress_loss"] == 0
    assert (
        budget_decision(result, RiskWatchPolicy(loss_budget=50))["state"]
        == "within_sample_budget"
    )
    assert result["positions"][0]["return_1d_pct"] > 0


def test_future_close_and_late_retrieval_cannot_change_pre_cutoff_risk():
    packet = frozen_packet()
    baseline = evaluate_packet(packet)
    for snapshot in packet["snapshots"].values():
        future = copy.deepcopy(snapshot["daily_observations"][-1])
        future.update(
            {
                "timestamp": "2025-05-22T10:00:00+00:00",
                "completed_at": "2025-05-22T16:00:00+00:00",
                "available_at": "2025-05-22T16:01:00+00:00",
                "close": 1,
                "adjusted_close": 1,
            }
        )
        snapshot["daily_observations"].append(future)
        snapshot["return_1d_pct"] = -99
        snapshot["latest_volume_vs_prior_20d"] = 999
    assert evaluate_packet(packet) == baseline
    packet["snapshots"]["SYN-A"]["retrieved_at"] = "2025-05-22T00:00:00+00:00"
    unavailable = evaluate_packet(packet)
    assert unavailable["status"] == "insufficient_data"
    assert unavailable["historical_stress_loss"] is None
    assert (
        budget_decision(unavailable, RiskWatchPolicy(loss_budget=50))["state"]
        == "unavailable"
    )


@pytest.mark.parametrize(
    "fault",
    [
        "currency",
        "stale",
        "basis",
        "duplicate",
        "negative",
        "option",
        "missing_adjusted",
        "short",
        "nonlinear",
        "multiplier",
        "identity",
    ],
)
def test_incomplete_or_unsupported_exposure_never_becomes_zero_risk(fault):
    packet = frozen_packet()
    snap = packet["snapshots"]["SYN-A"]
    if fault == "currency":
        snap["currency"] = "EUR"
    if fault == "basis":
        snap["price_basis"] = "unknown"
    if fault == "stale":
        snap["daily_observations"] = snap["daily_observations"][:-5]
    if fault == "duplicate":
        snap["daily_observations"].append(copy.deepcopy(snap["daily_observations"][-1]))
    if fault == "negative":
        packet["positions"][0]["quantity"] = -1
    if fault == "option":
        packet["positions"][0]["instrument_type"] = "option"
    if fault == "short":
        packet["positions"][0]["direction"] = "short"
    if fault == "nonlinear":
        packet["positions"][0]["is_nonlinear"] = True
    if fault == "multiplier":
        packet["positions"][0]["multiplier"] = 0
    if fault == "identity":
        snap["reported_symbol"] = "DIFFERENT-SYNTHETIC-INSTRUMENT"
    if fault == "missing_adjusted":
        for bar in snap["daily_observations"]:
            bar["adjusted_close"] = None
    result = evaluate_packet(packet)
    assert result["status"] == "insufficient_data"
    assert result["historical_stress_loss"] is None
    assert result["excluded"]
    assert result["covered_stress_loss"] > 0
    assert (
        budget_decision(result, RiskWatchPolicy(loss_budget=1))["state"]
        == "unavailable"
    )


def test_missing_session_does_not_align_two_day_and_one_day_returns():
    packet = frozen_packet()
    del packet["snapshots"]["SYN-A"]["daily_observations"][-3]
    assert evaluate_packet(packet)["matched_returns"] == 58


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf")])
def test_budget_must_be_an_explicit_positive_finite_owner_choice(value):
    with pytest.raises(ValidationError):
        RiskWatchPolicy(loss_budget=value)


def test_frozen_packet_hash_changes_when_evidence_changes():
    packet = frozen_packet()
    baseline = digest(packet)
    assert digest(copy.deepcopy(packet)) == baseline
    packet["positions"][0]["quantity"] += 1
    assert digest(packet) != baseline


def test_freeze_removes_future_bars_and_recomputes_derived_features():
    packet = frozen_packet()
    baseline = copy.deepcopy(packet)
    future = copy.deepcopy(packet["snapshots"]["SYN-A"]["daily_observations"][-1])
    future.update(
        {
            "timestamp": "2025-05-22T10:00:00+00:00",
            "completed_at": "2025-05-22T16:00:00+00:00",
            "available_at": "2025-05-22T16:01:00+00:00",
            "close": 1,
            "adjusted_close": 1,
        }
    )
    packet["snapshots"]["SYN-A"]["daily_observations"].append(future)
    packet["snapshots"]["SYN-A"]["return_1d_pct"] = -99
    assert freeze_packet(packet) == baseline


def test_versioned_regression_manifest_freezes_inputs_policy_and_controls():
    fixture = json.loads(
        (Path(__file__).parent / "fixtures" / "portfolio-risk-v1.json").read_text()
    )
    for name, kwargs in [
        ("shared_exposure", {}),
        ("rising", {"rising": True}),
        ("quiet", {"quiet": True}),
    ]:
        assert digest(frozen_packet(**kwargs)) == fixture["packet_hashes"][name]
    for name, kwargs in [
        ("decline", {}),
        ("rebound", {"final_fraction": 1.1}),
        ("host_offline", {"host": False}),
        ("late_response", {"delay": 68400}),
    ]:
        assert benchmark_case(**kwargs) == fixture["cases"][name]


def benchmark_case(*, packet=None, final_fraction=0.85, host=True, delay=600):
    packet = packet or frozen_packet()
    initial = evaluate_packet(packet)["covered_value"]
    cutoff = datetime.fromisoformat(packet["cutoff"])
    quotes = [
        {
            "timestamp": (cutoff + timedelta(hours=hours)).isoformat(),
            "available_at": (cutoff + timedelta(hours=hours)).isoformat(),
            "book_value": initial * fraction,
        }
        for hours, fraction in [(0, 1), (16, 0.96), (18, 0.86), (40, final_fraction)]
    ]
    policy = BenchmarkResponsePolicy(
        loss_budget=50,
        reduction_fraction=0.25,
        delivery_seconds=30,
        response_seconds=delay,
        cost_bps_each_way=10,
    )
    series = [
        {
            "timestamp": (cutoff - timedelta(days=40 - i)).isoformat(),
            "completed_at": (cutoff - timedelta(days=40 - i)).isoformat(),
            "available_at": (cutoff - timedelta(days=40 - i)).isoformat(),
            "close": 100 + i * 0.001,
        }
        for i in range(41)
    ]
    return run_benchmark(
        packet=packet,
        policy=policy,
        benchmark_observations=series,
        outcome_quotes=quotes,
        host_available=host,
    )


def test_benchmark_uses_post_delivery_price_and_keeps_opening_gap():
    result = benchmark_case()
    assert result["candidate_alert"]
    assert result["unchanged_benchmark_regime"] == "transition"
    assert result["unchanged_rule"]["turnover"] == 0
    assert result["candidate"]["fill_at"] == "2025-05-22T14:00:00+00:00"
    assert result["candidate"]["cost"] > 0
    assert 0 < result["candidate_loss_avoided"] < result["hold_loss"] * 0.25


def test_benchmark_reports_unnecessary_alert_lost_upside_and_host_downtime():
    rise = benchmark_case(final_fraction=1.1)
    assert rise["unnecessary_alert"] and rise["lost_upside"] > 0
    offline = benchmark_case(host=False)
    assert offline["missed_warning"] and offline["candidate"]["turnover"] == 0
    assert offline["detected_at"] is None and offline["delivered_at"] is None
    late = benchmark_case(delay=19 * 3600)
    assert late["candidate_loss_avoided"] < benchmark_case()["candidate_loss_avoided"]
