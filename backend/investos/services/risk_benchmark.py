"""Small, deterministic advisory benchmark; never selects or tunes a live policy."""

from datetime import timedelta

from pydantic import BaseModel, ConfigDict, Field

from investos.schemas.portfolio_risk import RiskWatchPolicy
from investos.services.portfolio_risk import (
    budget_decision,
    clock,
    digest,
    evaluate_packet,
)
from investos.services.risk import RiskService


class BenchmarkResponsePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    loss_budget: float = Field(gt=0)
    reduction_fraction: float = Field(gt=0, le=1)
    delivery_seconds: int = Field(ge=0)
    response_seconds: int = Field(ge=0)
    cost_bps_each_way: float = Field(ge=0, le=1000)


def run_benchmark(
    *,
    packet: dict,
    policy: BenchmarkResponsePolicy,
    benchmark_observations: list[dict],
    outcome_quotes: list[dict],
    host_available: bool = True,
) -> dict:
    """Hold vs unchanged monthly rule vs one declared candidate response.

    Outcome quotes are synthetic aggregate-book values on a constant-share basis.
    The first eligible quote is a hypothetical fill after delivery and response
    latency. Cash earns zero; re-entry and both costs occur at the last quote.
    This checks causal accounting, not execution feasibility or forecasting skill.
    """
    result = evaluate_packet(packet)
    if result["status"] != "ready":
        raise ValueError("Benchmark requires a covered frozen packet")
    cutoff = clock(packet["cutoff"])
    series = sorted(
        (clock(bar["timestamp"]), bar["close"])
        for bar in benchmark_observations
        if all(
            clock(bar[key]) <= cutoff
            for key in ("timestamp", "completed_at", "available_at")
        )
        and bar["close"] > 0
    )
    if not series:
        raise ValueError("No pre-cutoff benchmark observations")
    reference = (
        RiskService._latest_price_before(series, cutoff - timedelta(days=30))
        or series[0][1]
    )
    regime, _ = RiskService.classify_benchmark_return(series[-1][1] / reference - 1)
    decision = budget_decision(result, RiskWatchPolicy(loss_budget=policy.loss_budget))
    alerted = host_available and decision["state"] == "breached"
    delivered_at = (
        cutoff + timedelta(seconds=policy.delivery_seconds) if alerted else None
    )
    eligible_at = cutoff + timedelta(
        seconds=policy.delivery_seconds + policy.response_seconds
    )
    quotes = sorted(
        (clock(q["timestamp"]), q["book_value"])
        for q in outcome_quotes
        if clock(q["timestamp"]) >= cutoff
        and q["book_value"] > 0
        and clock(q["available_at"]) <= clock(q["timestamp"])
    )
    if not quotes:
        raise ValueError("No outcome quotes")
    fills = [quote for quote in quotes if quote[0] >= eligible_at]
    initial = result["covered_value"]
    final = quotes[-1][1]
    cost_rate = policy.cost_bps_each_way / 10000

    def response(enabled: bool) -> dict:
        if not enabled or not fills or fills[0][0] >= quotes[-1][0]:
            return {
                "ending_value": round(final, 2),
                "fill_at": None,
                "turnover": 0.0,
                "cost": 0.0,
            }
        fill_time, fill_value = fills[0]
        sale = policy.reduction_fraction * fill_value
        sale_cost = sale * cost_rate
        cash = sale - sale_cost
        # Buy notional plus buy fee cannot exceed available cash.
        buy_notional = cash / (1 + cost_rate)
        buy_cost = cash - buy_notional
        ending = (1 - policy.reduction_fraction) * final + buy_notional
        return {
            "ending_value": round(ending, 2),
            "fill_at": fill_time.isoformat(),
            "reentry_at": quotes[-1][0].isoformat(),
            "turnover": round(sale + buy_notional, 2),
            "cost": round(sale_cost + buy_cost, 2),
        }

    candidate = response(alerted)
    existing = response(host_available and regime in {"risk_off", "crisis"})
    return {
        "packet_hash": digest(packet),
        "policy_hash": digest(policy.model_dump()),
        "detected_at": packet["cutoff"] if host_available else None,
        "delivered_at": delivered_at.isoformat() if delivered_at else None,
        "candidate_alert": alerted,
        "unchanged_benchmark_regime": regime,
        "hold_ending_value": final,
        "hold_loss": round(initial - final, 2),
        "candidate": candidate,
        "unchanged_rule": existing,
        "candidate_loss_avoided": round(candidate["ending_value"] - final, 2),
        "lost_upside": round(max(0, final - candidate["ending_value"]), 2),
        "missed_warning": initial - final > policy.loss_budget and not alerted,
        "unnecessary_alert": alerted and initial - final <= policy.loss_budget,
        "limitations": "Synthetic aggregate quotes; no taxes, spreads, market impact, liquidity or calibrated predictive rate. Policy is not optimized.",
    }
