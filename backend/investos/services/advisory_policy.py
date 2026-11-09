"""Fixed, paper-only policy for a prospective daily advisory trial."""

from __future__ import annotations

import math
from datetime import timedelta

from investos.services.portfolio_risk import clock, evaluate_packet, positive
from investos.services.risk import RiskService
from investos.services.risk_benchmark import paper_trim


def declared_policy(benchmark_ticker: str) -> dict:
    # Research protocol choices, frozen at declaration and never fitted to outcomes.
    return {
        "version": "adverse-source-and-breadth-v1",
        "benchmark_ticker": benchmark_ticker,
        "duration_days": 30,
        "outcome_grace_days": 7,
        "source_max_age_hours": 72,
        "minimum_claim_confidence": 0.7,
        "minimum_weak_holdings": 2,
        "minimum_weak_weight_pct": 25,
        "weak_return_pct": -1,
        "adverse_outcome_pct": -2,
        "response_delay_minutes": 15,
        "reduction_fraction": 0.25,
        "cost_bps_each_way": 10,
    }


def session_bars(snapshot: dict) -> dict[str, dict]:
    bars = snapshot.get("daily_observations", [])
    by_day = {clock(bar["timestamp"]).date().isoformat(): bar for bar in bars}
    return by_day if len(by_day) == len(bars) else {}


def decide(
    packet: dict,
    benchmark: dict,
    sources: list[dict],
    policy: dict,
    alerted_sources: set[str],
) -> dict:
    result = evaluate_packet(packet)
    cutoff = clock(packet["cutoff"])
    benchmark_bars = session_bars(benchmark)
    days = sorted(benchmark_bars)
    day = days[-1] if days else None
    decision = {
        "status": "unavailable",
        "session": day,
        "benchmark_regime": None,
        "candidate_alert": False,
        "benchmark_alert": False,
        "source_hash": None,
        "weak_weight_pct": None,
        "affected_holdings": [],
        "coverage": {
            "covered_positions": result["covered_positions"],
            "total_positions": result["total_positions"],
            "matching_returns": result["matched_returns"],
            "issues": result["issues"],
        },
        "stress_loss": result["historical_stress_loss"],
        "reference_value": result["covered_value"],
    }
    if result["status"] != "ready" or result["latest_session_date"] != day:
        return {
            **decision,
            "reason": "Portfolio and benchmark closes do not have complete matching coverage.",
        }
    if (
        len(days) < 2
        or benchmark.get("reported_symbol") != policy["benchmark_ticker"]
        or benchmark.get("currency") != "USD"
    ):
        return {
            **decision,
            "reason": "Benchmark identity or session history is unavailable.",
        }
    reference = next(
        (
            bar["close"]
            for bar in reversed(list(benchmark_bars.values()))
            if clock(bar["timestamp"]) <= cutoff - timedelta(days=30)
        ),
        None,
    )
    if not reference:
        return {
            **decision,
            "reason": "The benchmark's 30-day reference close is unavailable.",
        }
    for row in result["positions"]:
        bars = session_bars(packet["snapshots"][row["ticker"]])
        if sorted(bars)[-2:] != days[-2:]:
            return {
                **decision,
                "reason": "Latest returns do not span the same sessions.",
            }
    regime, _ = RiskService.classify_benchmark_return(
        benchmark_bars[day]["close"] / reference - 1
    )
    decision.update(
        status="ready",
        benchmark_regime=regime,
        benchmark_alert=regime in {"risk_off", "crisis"},
        reason="No new attributable source met the shared weakness rule.",
    )
    # Count distinct issuer exposures. Multiple share classes of one issuer
    # contribute their weights without counting as independent weak issuers.
    by_id = {row["security_id"]: row for row in packet["positions"]}
    for source in sources:
        if (
            source["content_hash"] in alerted_sources
            or clock(source["published_at"]) > cutoff
            or clock(source["retrieved_at"]) > cutoff
            or clock(source["version_observed_at"]) > cutoff
            or any(clock(claim["available_at"]) > cutoff for claim in source["claims"])
        ):
            continue
        affected = set(source["security_ids"])
        weak = [
            row
            for row in result["positions"]
            if row["security_id"] in affected
            and row["return_1d_pct"] is not None
            and row["return_1d_pct"] <= policy["weak_return_pct"]
        ]
        issuers = {by_id[row["security_id"]]["entity_id"] for row in weak}
        weight = sum(row["weight_pct"] for row in weak)
        if (
            len(issuers) >= policy["minimum_weak_holdings"]
            and weight >= policy["minimum_weak_weight_pct"]
        ):
            return {
                **decision,
                "candidate_alert": True,
                "source_hash": source["content_hash"],
                "weak_weight_pct": round(weight, 3),
                "affected_holdings": weak,
                "reason": "One dated adverse source and shared weakness met the declared rule.",
            }
    return decision


def settle(case: dict, packet: dict, benchmark_bar: dict, policy: dict) -> dict:
    """Score the next session on decision-time shares and later captured marks."""
    decision = case["decision"]
    day = clock(benchmark_bar["timestamp"]).date().isoformat()
    unavailable = {"status": "unavailable", "session": day}
    if decision["status"] != "ready":
        return {**unavailable, "reason": "Decision coverage was incomplete."}
    opening = benchmark_bar.get("session_open_at")
    closing = benchmark_bar["completed_at"]
    if not opening or clock(opening) >= clock(closing):
        return {
            **unavailable,
            "reason": "The next session's regular opening time was not captured.",
        }
    current = {row["security_id"]: row for row in packet["positions"]}
    initial = fill_value = final_value = 0.0
    marks = {}
    original = case["market_packet"]
    for position in original["positions"]:
        security_id = position["security_id"]
        ticker = position["ticker"]
        latest = current.get(security_id)
        if not latest or any(
            latest.get(key) != position.get(key)
            for key in (
                "ticker",
                "entity_id",
                "exchange",
                "isin",
                "share_class",
                "instrument_type",
                "multiplier",
            )
        ):
            return {
                **unavailable,
                "reason": "A frozen security could not be matched to the later capture.",
            }
        original_snapshot = original["snapshots"][ticker]
        snapshot = packet["snapshots"].get(ticker, {})
        if any(
            snapshot.get(key) != original_snapshot.get(key)
            for key in (
                "provider",
                "reported_symbol",
                "reported_exchange",
                "currency",
                "price_basis",
            )
        ):
            return {**unavailable, "reason": "The later price source or basis differs."}
        before = session_bars(original_snapshot)[decision["session"]]
        bars = session_bars(snapshot)
        previous = bars.get(decision["session"], {})
        bar = bars.get(day, {})
        if (
            bar.get("session_open_at") != opening
            or bar.get("completed_at") != closing
            or not positive(bar.get("open"))
            or not positive(bar.get("adjusted_close"))
            or not positive(previous.get("adjusted_close"))
        ):
            return {
                **unavailable,
                "reason": "Synchronized open and close marks are incomplete.",
            }
        if previous["close"] != before["close"] or not math.isclose(
            previous["adjusted_close"] / previous["close"],
            bar["adjusted_close"] / bar["close"],
            rel_tol=1e-5,
        ):
            return {
                **unavailable,
                "reason": "A price revision or corporate action changed the share basis.",
            }
        quantity = position["quantity"]
        initial += quantity * before["close"]
        fill_value += quantity * bar["open"]
        final_value += quantity * bar["close"]
        marks[security_id] = {"previous": previous, "outcome": bar}
    eligible_at = clock(case["detected_at"]) + timedelta(
        minutes=policy["response_delay_minutes"]
    )
    timely = eligible_at <= clock(opening)
    hold_return = 100 * (final_value / initial - 1)
    adverse = hold_return <= policy["adverse_outcome_pct"]
    comparison = {}
    for name, alert in (
        ("candidate", decision["candidate_alert"]),
        ("benchmark", decision["benchmark_alert"]),
    ):
        acted = alert and timely
        value, turnover, costs = (
            paper_trim(
                fill_value=fill_value,
                final_value=final_value,
                fraction=policy["reduction_fraction"],
                cost_bps_each_way=policy["cost_bps_each_way"],
            )
            if acted
            else (final_value, 0.0, 0.0)
        )
        comparison[name] = {
            "alert": alert,
            "acted": acted,
            "missed_warning": adverse and not acted,
            "unnecessary_alert": alert and not adverse,
            "advantage_bps": round(10000 * (value - final_value) / initial, 4),
            "lost_upside_bps": round(10000 * max(0, final_value - value) / initial, 4),
            "turnover_pct": round(100 * turnover / initial, 4),
            "cost_bps": round(10000 * costs / initial, 4),
        }
    return {
        "status": "scored",
        "session": day,
        "marks": marks,
        "benchmark_mark": benchmark_bar,
        "paper_eligible_at": eligible_at.isoformat(),
        "paper_open_at": opening,
        "paper_reentry_at": closing,
        "timely": timely,
        "adverse": adverse,
        "hold_return_pct": round(hold_return, 4),
        "opening_gap_pct": round(100 * (fill_value / initial - 1), 4),
        **comparison,
    }
