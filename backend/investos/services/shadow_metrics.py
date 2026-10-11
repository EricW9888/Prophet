"""Measured statistics over captured paper equity, never terminal-return proxies."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime
from statistics import mean, stdev
from typing import Any


def unavailable_metrics(reason: str, *, observation_count: int = 0) -> dict[str, Any]:
    return {
        "version": 1,
        "source": "captured_paper_equity",
        "coverage": "observed_checkpoints_only",
        "observation_count": observation_count,
        "return_count": max(0, observation_count - 1),
        "return_convention": "simple_returns_without_external_cash_flows",
        "cash_flow_policy": "nonzero_or_unknown_external_flows_are_unsupported",
        "cash_comparator_return": 0.0,
        "annualization": "none_per_observed_interval",
        "standard_deviation": "sample_ddof_1",
        "minimum_return_count": 2,
        "max_drawdown": None,
        "sharpe_ratio": None,
        "drawdown_status": "unavailable",
        "drawdown_reason": reason,
        "sharpe_status": "unavailable",
        "sharpe_reason": reason,
    }


def calculate_shadow_metrics(observations: list[dict[str, Any]]) -> dict[str, Any]:
    """Drawdown is nonpositive; Sharpe is per equally spaced observed interval.

    Cash in the paper simulator earns zero interest. Buys, sells, fees and
    dividends are internal portfolio activity, not external contributions.
    Nonzero/unknown external flows need a separate flow-adjusted valuation path.
    Sparse observations cannot establish an unobserved intraperiod drawdown.
    """
    metrics = unavailable_metrics(
        "insufficient_observations", observation_count=len(observations)
    )
    if not observations:
        return metrics
    values: list[float] = []
    times: list[datetime] = []
    try:
        for observation in observations:
            captured_at = datetime.fromisoformat(
                str(observation["captured_at"]).replace("Z", "+00:00")
            )
            if captured_at.tzinfo is None or captured_at.utcoffset() is None:
                raise ValueError("undated equity")
            equity = float(observation["equity"])
            if not math.isfinite(equity) or equity < 0:
                raise ValueError("invalid equity")
            times.append(captured_at)
            values.append(equity)
    except (KeyError, TypeError, ValueError, OverflowError):
        return unavailable_metrics(
            "invalid_observation", observation_count=len(observations)
        )
    if values[0] <= 0:
        return unavailable_metrics(
            "nonpositive_starting_equity", observation_count=len(observations)
        )
    intervals = [
        (right - left).total_seconds() for left, right in zip(times, times[1:])
    ]
    if any(interval <= 0 for interval in intervals):
        return unavailable_metrics(
            "nonincreasing_timestamps", observation_count=len(observations)
        )
    for observation in observations:
        flow = observation.get("net_external_cash_flow")
        try:
            if flow is None or not math.isfinite(float(flow)) or float(flow) != 0:
                return unavailable_metrics(
                    "unsupported_external_cash_flows",
                    observation_count=len(observations),
                )
        except (TypeError, ValueError, OverflowError):
            return unavailable_metrics(
                "unsupported_external_cash_flows", observation_count=len(observations)
            )
    metrics.update(
        {
            "first_observed_at": times[0].isoformat(),
            "last_observed_at": times[-1].isoformat(),
            "observations_sha256": hashlib.sha256(
                json.dumps(
                    [
                        {
                            "captured_at": timestamp.isoformat(),
                            "equity": equity,
                            "net_external_cash_flow": 0.0,
                        }
                        for timestamp, equity in zip(times, values)
                    ],
                    sort_keys=True,
                    allow_nan=False,
                ).encode()
            ).hexdigest(),
        }
    )
    if len(values) < 2:
        return metrics
    peak = values[0]
    drawdown = 0.0
    for equity in values[1:]:
        peak = max(peak, equity)
        drawdown = min(drawdown, equity / peak - 1)
    metrics.update(
        max_drawdown=drawdown, drawdown_status="measured", drawdown_reason=None
    )
    if len(intervals) < 2:
        metrics["sharpe_reason"] = "insufficient_returns"
        return metrics
    # Cap timestamp jitter at one second and 1% of the observed interval.
    # This must not admit substantially different subsecond intervals.
    cadence = intervals[0]
    tolerance = min(1.0, cadence * 0.01)
    if any(
        not math.isclose(interval, cadence, rel_tol=0, abs_tol=tolerance)
        for interval in intervals
    ):
        metrics["sharpe_reason"] = "irregular_observation_cadence"
        return metrics
    metrics["cadence_seconds"] = cadence
    metrics["cadence_tolerance_seconds"] = tolerance
    if any(equity <= 0 for equity in values[:-1]):
        metrics["sharpe_reason"] = "nonpositive_return_denominator"
        return metrics
    returns = [right / left - 1 for left, right in zip(values, values[1:])]
    if any(not math.isfinite(value) for value in returns):
        metrics["sharpe_reason"] = "nonfinite_return_statistic"
        return metrics
    deviation = stdev(returns)
    if math.isclose(deviation, 0, abs_tol=1e-12):
        metrics["sharpe_reason"] = "zero_return_variance"
        return metrics
    ratio = mean(returns) / deviation
    if not math.isfinite(ratio):
        metrics["sharpe_reason"] = "nonfinite_return_statistic"
        return metrics
    metrics.update(sharpe_ratio=ratio, sharpe_status="measured", sharpe_reason=None)
    return metrics


def result_metrics(result: Any) -> dict[str, Any]:
    """Use one availability contract in reports, API views and graph consumers."""
    metadata = getattr(result, "performance_metrics_json", None)
    if not isinstance(metadata, dict) or metadata.get("version") != 1:
        return unavailable_metrics("legacy_unverified_metrics")
    return dict(metadata)
