"""Availability-aware, deterministic scenarios and one-shot advisory watches.

The existing ScenarioAnalysis store retains immutable inputs/results; ActiveWatcher
and the opt-in push queue own alert state and delivery. No model or broker call is
part of this path. A historical scenario is exposure framing, not a forecast.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import math
import statistics
from datetime import UTC, datetime, timedelta
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import desc, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from investos.config import settings
from investos.models.entity import Security
from investos.models.notification import PushNotificationDelivery, PushNotificationEvent
from investos.models.portfolio import Position
from investos.models.quant import ScenarioAnalysis
from investos.models.watcher import ActiveWatcher
from investos.schemas.portfolio_risk import (
    PortfolioRiskAssessment,
    PortfolioRiskState,
    RiskWatchPolicy,
    RiskWatchRequest,
)
from investos.services.market_data import MarketDataService
from investos.services.push_notification import PushNotificationService
from investos.services.runtime_settings import RuntimeSettingsStore

VERSION = "portfolio-daily-stress-v1"
SCENARIO_NAME = "Portfolio daily stress (v1)"
CONDITION = "portfolio_loss_budget"
MIN_RETURNS = 40
LOOKBACK = 60


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def clock(value: str) -> datetime:
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("Risk packet timestamps must include a timezone")
    return result.astimezone(UTC)


def positive(value: object) -> float | None:
    number = MarketDataService._finite_number(value)
    return number if number is not None and number > 0 else None


def freeze_packet(packet: dict) -> dict:
    """Keep decision-eligible observations, not future derived snapshot features."""
    frozen = copy.deepcopy(packet)
    cutoff = clock(packet["cutoff"])
    metadata = {
        "ticker",
        "provider",
        "reported_symbol",
        "reported_exchange",
        "exchange_timezone",
        "as_of_kind",
        "retrieved_at",
        "currency",
        "price_basis",
        "historical_availability_verified",
        "status",
    }
    for ticker, snapshot in frozen["snapshots"].items():
        acquired = (
            bool(snapshot.get("retrieved_at"))
            and clock(snapshot["retrieved_at"]) <= cutoff
        )
        observations = (
            [
                {
                    **bar,
                    **{
                        key: clock(bar[key])
                        for key in ("timestamp", "completed_at", "available_at")
                    },
                }
                for bar in snapshot.get("daily_observations", [])
            ]
            if acquired
            else []
        )
        features = MarketDataService.build_signal_snapshot(
            ticker=ticker, observations=observations, cutoff=cutoff
        )
        frozen["snapshots"][ticker] = {
            **{key: value for key, value in snapshot.items() if key in metadata},
            **features,
        }
    return frozen


def evaluate_packet(packet: dict, *, max_age_hours: int = 96) -> dict:
    """Replay only packet observations available by its cutoff, without I/O.

    Reweight simultaneous daily adjusted returns to current completed-close
    weights. This preserves observed co-movement; it does not infer causality,
    reconstruct historical holdings, estimate a calibrated tail, or convert deals.
    """
    if packet.get("version") != VERSION:
        raise ValueError("Unsupported risk packet version")
    cutoff = clock(packet["cutoff"])
    snapshots = packet.get("snapshots", {})
    issues: list[str] = []
    if (
        packet.get("portfolio_captured_at")
        and clock(packet["portfolio_captured_at"]) > cutoff
    ):
        issues.append("Portfolio state was captured after the cutoff.")
    eligible = []
    excluded = []
    total_positions = len(packet.get("positions", []))
    if not total_positions:
        issues.append("No invested holdings in the captured portfolio.")
    for position in packet.get("positions", []):
        ticker = position["ticker"]
        snapshot = snapshots.get(ticker, {})
        bars = []
        reason = None
        if (
            not positive(position.get("quantity"))
            or position.get("direction") != "long"
            or position.get("is_nonlinear") is not False
            or position.get("instrument_type") not in {"common_stock", "adr", "etf"}
            or position.get("multiplier") != 1
        ):
            reason = "unsupported quantity or instrument payoff"
        elif snapshot.get("reported_symbol") != ticker:
            reason = "missing or mismatched provider symbol"
        elif snapshot.get("currency") != "USD":
            reason = "missing or unsupported quote currency"
        elif (
            not snapshot.get("retrieved_at") or clock(snapshot["retrieved_at"]) > cutoff
        ):
            reason = "not captured by cutoff"
        elif snapshot.get("price_basis") != "provider_adjusted_current_revision":
            reason = "unverified return price basis"
        if reason is None:
            for bar in snapshot.get("daily_observations", []):
                if (
                    all(
                        bar.get(key) and clock(bar[key]) <= cutoff
                        for key in ("timestamp", "completed_at", "available_at")
                    )
                    and positive(bar.get("close"))
                    and positive(bar.get("adjusted_close"))
                ):
                    bars.append(bar)
            bars.sort(key=lambda bar: clock(bar["timestamp"]))
            dates = [clock(bar["timestamp"]).date().isoformat() for bar in bars]
            if len(dates) != len(set(dates)):
                reason = "ambiguous duplicate daily observations"
            elif not bars:
                reason = "no completed available bars"
            elif cutoff - clock(bars[-1]["completed_at"]) > timedelta(
                hours=max_age_hours
            ):
                reason = "stale completed close"
        if reason:
            excluded.append({"ticker": ticker, "reason": reason})
            continue
        returns = {}
        tape = MarketDataService.build_signal_snapshot(
            ticker=ticker,
            observations=[
                {
                    **bar,
                    **{
                        key: clock(bar[key])
                        for key in ("timestamp", "completed_at", "available_at")
                    },
                }
                for bar in bars
            ],
            cutoff=cutoff,
        )
        # Include the preceding date in the key: a missing session must not turn
        # a two-session change into a matching one-session observation.
        for previous, current in zip(bars, bars[1:]):
            key = (
                clock(previous["timestamp"]).date().isoformat(),
                clock(current["timestamp"]).date().isoformat(),
            )
            returns[key] = current["adjusted_close"] / previous["adjusted_close"] - 1
        eligible.append(
            {
                "ticker": ticker,
                "security_id": position["security_id"],
                "value": position["quantity"] * bars[-1]["close"],
                "returns": returns,
                "last_day": clock(bars[-1]["timestamp"]).date().isoformat(),
                "signal_ref": tape.get("signal_ref"),
                "return_1d_pct": tape.get("return_1d_pct"),
                "volume_ratio": tape.get("latest_volume_vs_prior_20d"),
            }
        )
    value = sum(item["value"] for item in eligible)
    common = (
        sorted(set.intersection(*(set(item["returns"]) for item in eligible)))
        if eligible
        else []
    )
    common = common[-LOOKBACK:]
    if excluded:
        issues.append(
            "Some holdings cannot be assessed; their missing exposure is not zero risk."
        )
    if len({item["last_day"] for item in eligible}) > 1:
        issues.append("Holdings have different latest session dates.")
    if len(common) < MIN_RETURNS:
        issues.append(
            f"Need {MIN_RETURNS} matching daily returns; found {len(common)}."
        )
    complete = bool(eligible) and not issues and total_positions == len(eligible)
    result = {
        "version": VERSION,
        "cutoff": packet["cutoff"],
        "method": {
            "lookback_returns": LOOKBACK,
            "minimum_matching_returns": MIN_RETURNS,
            "tail_fraction": 0.05,
            "max_age_hours": max_age_hours,
        },
        "status": "ready" if complete else "insufficient_data",
        "currency": "USD",
        "covered_positions": len(eligible),
        "total_positions": total_positions,
        "covered_value": round(value, 2),
        "matched_returns": len(common),
        "latest_session_date": (
            eligible[0]["last_day"]
            if eligible and len({item["last_day"] for item in eligible}) == 1
            else None
        ),
        "excluded": excluded,
        "issues": issues,
        "historical_stress_loss": None,
        "covered_stress_loss": None,
        "covered_worst_observed_loss": None,
        "worst_observed_loss": None,
        "worst_session": None,
        "median_pair_correlation": None,
        "positions": [],
        "limitations": [
            "Completed daily closes; not intraday or overnight crash prediction.",
            "Current holdings reweighted over up to 60 matching observed returns; cash excluded.",
            "Current provider-adjusted history can be revised; not proof of historical availability.",
            "Small-sample stress can underestimate future loss; no confidence or probability guarantee.",
            "ETF look-through, FX, options, contingent deals and news catalysts are not modeled.",
        ],
    }
    if value > 0:
        result["positions"] = [
            {
                key: item[key]
                for key in (
                    "ticker",
                    "security_id",
                    "signal_ref",
                    "return_1d_pct",
                    "volume_ratio",
                )
            }
            | {"weight_pct": round(item["value"] / value * 100, 3)}
            for item in eligible
        ]
    if (
        not eligible
        or len(common) < MIN_RETURNS
        or len({item["last_day"] for item in eligible}) != 1
    ):
        return result
    pnl = [
        sum(item["value"] * item["returns"][day] for item in eligible) for day in common
    ]
    tail = sorted(pnl)[: max(1, math.ceil(len(pnl) * 0.05))]
    worst_index = pnl.index(min(pnl))
    worst_day = common[worst_index]
    result.update(
        {
            "historical_stress_loss": round(max(0, -statistics.mean(tail)), 2),
            "worst_observed_loss": round(max(0, -min(pnl)), 2),
            "worst_session": list(worst_day),
        }
    )
    result["covered_stress_loss"] = result["historical_stress_loss"]
    result["covered_worst_observed_loss"] = result["worst_observed_loss"]
    if not complete:
        result["historical_stress_loss"] = None
        result["worst_observed_loss"] = None
    for row, item in zip(result["positions"], eligible):
        row["worst_session_impact"] = round(
            item["value"] * item["returns"][worst_day], 2
        )
    correlations = []
    for index, left in enumerate(eligible):
        for right in eligible[index + 1 :]:
            a = [left["returns"][day] for day in common]
            b = [right["returns"][day] for day in common]
            if statistics.pstdev(a) > 1e-12 and statistics.pstdev(b) > 1e-12:
                correlations.append(statistics.correlation(a, b))
    if correlations:
        result["median_pair_correlation"] = round(statistics.median(correlations), 3)
    return result


def budget_decision(result: dict, policy: RiskWatchPolicy) -> dict:
    if result["status"] != "ready":
        return {
            "state": "unavailable",
            "detail": "Coverage is insufficient to evaluate the loss budget.",
        }
    loss = result["historical_stress_loss"]
    breached = loss >= policy.loss_budget
    return {
        "state": "breached" if breached else "within_sample_budget",
        "loss_budget": policy.loss_budget,
        "stress_to_budget_ratio": round(loss / policy.loss_budget, 3),
        "uniform_reduction_to_budget_pct": (
            round(max(0, 1 - policy.loss_budget / loss) * 100, 2) if loss else 0
        ),
        "detail": (
            f"Observed daily tail stress ${loss:,.2f} versus your ${policy.loss_budget:,.2f} budget. "
            "Review the contributors, verify current prices and catalysts, and consider holding, "
            "reducing overlapping exposure, or testing a smaller position in shadow. "
            "A proportional reduction is scenario arithmetic, not an instruction or a guarantee."
        ),
    }


class PortfolioRiskService:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def latest(self) -> ScenarioAnalysis | None:
        return await self.session.scalar(
            select(ScenarioAnalysis)
            .where(ScenarioAnalysis.name == SCENARIO_NAME)
            .order_by(desc(ScenarioAnalysis.computed_at))
            .limit(1)
        )

    async def snapshots_at(self, tickers: list[str], cutoff: datetime) -> dict:
        """Reuse an actual earlier capture without pretending a new fetch is old."""
        row = await self.session.scalar(
            select(ScenarioAnalysis)
            .where(
                ScenarioAnalysis.name == SCENARIO_NAME,
                ScenarioAnalysis.computed_at <= cutoff,
            )
            .order_by(desc(ScenarioAnalysis.computed_at))
            .limit(1)
        )
        if row is None:
            return {}
        packet = row.shock_parameters_json["packet"]
        snapshots = packet["snapshots"]
        return {
            ticker: {
                **copy.deepcopy(snapshots[ticker]),
                "capture_ref": f"risk-packet:{row.id}",
                "capture_hash": row.shock_parameters_json["packet_hash"],
                "age_hours": round(
                    (cutoff - clock(snapshots[ticker]["completed_at"])).total_seconds()
                    / 3600,
                    2,
                ),
            }
            for ticker in {ticker.upper() for ticker in tickers}
            if ticker in snapshots
            and snapshots[ticker].get("completed_at")
            and clock(snapshots[ticker]["retrieved_at"]) <= cutoff
            and clock(snapshots[ticker]["completed_at"]) <= cutoff
        }

    async def state(self) -> PortfolioRiskState:
        from investos.services.review import ReviewService

        row = await self.latest()
        watcher = await self.session.scalar(
            select(ActiveWatcher)
            .where(ActiveWatcher.condition_type == CONDITION)
            .order_by(desc(ActiveWatcher.created_at))
            .limit(1)
        )
        deliveries = []
        if watcher:
            rows = (
                (
                    await self.session.execute(
                        select(PushNotificationDelivery)
                        .join(
                            PushNotificationEvent,
                            PushNotificationDelivery.event_id
                            == PushNotificationEvent.id,
                        )
                        .where(
                            PushNotificationEvent.event_key
                            == f"watcher:{watcher.id}:triggered"
                        )
                    )
                )
                .scalars()
                .all()
            )
            deliveries = [
                {
                    "status": item.status,
                    "queued_at": item.created_at,
                    "delivered_at": item.delivered_at,
                }
                for item in rows
            ]
        return PortfolioRiskState(
            portfolio_data_review_count=await ReviewService(self.session).queue_count(
                "portfolio_data"
            ),
            checked_at=datetime.now(UTC),
            automation_enabled=settings.AUTOMATION_ENABLED,
            market_data_enabled=RuntimeSettingsStore.load().market_data.enabled,
            last_refresh_at=(
                None
                if row is None
                else row.shock_parameters_json.get("last_checked_at", row.computed_at)
            ),
            refresh_interval_seconds=max(
                300, RuntimeSettingsStore.load().market_data.refresh_interval_seconds
            ),
            assessment=(
                None
                if row is None
                else PortfolioRiskAssessment(
                    id=row.id,
                    detected_at=row.computed_at,
                    packet_hash=row.shock_parameters_json["packet_hash"],
                    result=row.portfolio_impact_json,
                )
            ),
            watch=(
                None
                if watcher is None
                else {
                    "id": str(watcher.id),
                    "status": watcher.status,
                    "enabled": watcher.is_active,
                    "policy": watcher.condition_params_json,
                    "last_checked_at": watcher.last_checked_at,
                    "triggered_at": watcher.triggered_at,
                    "detail": watcher.trigger_detail,
                    "assessment": watcher.action_taken_json,
                }
            ),
            delivery=deliveries,
        )

    async def configure(self, payload: RiskWatchRequest) -> PortfolioRiskState:
        # Serialize configuration with evaluation, including concurrent API saves.
        await self.session.execute(text("SELECT pg_advisory_xact_lock(149149)"))
        active = (
            await self.session.scalars(
                select(ActiveWatcher).where(
                    ActiveWatcher.condition_type == CONDITION,
                    ActiveWatcher.is_active.is_(True),
                )
            )
        ).all()
        policy = RiskWatchPolicy.model_validate(
            payload.model_dump(exclude={"enabled"})
        ).model_dump()
        if (
            payload.enabled
            and len(active) == 1
            and active[0].condition_params_json == policy
        ):
            await self.session.commit()
            return await self.state()
        for watcher in active:
            watcher.is_active = False
            watcher.status = "cancelled"
        if payload.enabled:
            self.session.add(
                ActiveWatcher(
                    source="owner",
                    condition_type=CONDITION,
                    condition_params_json=policy,
                    objective="Review portfolio daily loss-budget exposure",
                    adjustment_plan="Inspect the frozen risk assessment before deciding whether to hold or change exposure.",
                    status="pending",
                    is_active=True,
                )
            )
        await self.session.commit()
        return await self.state()

    async def refresh(self) -> PortfolioRiskState:
        rows = (
            await self.session.execute(
                select(Position, Security)
                .join(Security, Position.security_id == Security.id)
                .where(Position.list_type == "holding", Position.quantity != 0)
                .order_by(Security.id)
            )
        ).all()
        runtime = RuntimeSettingsStore.load()
        portfolio_captured_at = datetime.now(UTC).isoformat()
        snapshots = {}
        if (
            runtime.market_data.enabled
            and runtime.market_data.provider == "yahoo_finance"
        ):
            try:
                async with asyncio.timeout(60):
                    snapshots = await MarketDataService(
                        self.session
                    ).fetch_signal_snapshots([security.ticker for _, security in rows])
            except TimeoutError:
                snapshots = {}
        for snapshot in snapshots.values():
            if snapshot.get("daily_observations"):
                snapshot["daily_observations"] = snapshot["daily_observations"][
                    -LOOKBACK - 1 :
                ]
        cutoff = datetime.now(UTC)
        packet = {
            "version": VERSION,
            "cutoff": cutoff.isoformat(),
            "portfolio_captured_at": portfolio_captured_at,
            "positions": [
                {
                    "security_id": str(s.id),
                    "ticker": s.ticker.upper(),
                    "exchange": s.exchange,
                    "isin": s.isin,
                    "share_class": s.share_class,
                    "asset_class": s.asset_class,
                    "quantity": float(p.quantity),
                    "direction": p.direction,
                    "is_nonlinear": p.is_nonlinear,
                    "instrument_type": s.instrument_type,
                    "multiplier": 1.0 if s.multiplier is None else float(s.multiplier),
                }
                for p, s in rows
            ],
            "snapshots": snapshots,
        }
        return await self.capture(packet)

    async def capture(self, packet: dict) -> PortfolioRiskState:
        packet = freeze_packet(packet)
        await self.session.execute(text("SELECT pg_advisory_xact_lock(149149)"))
        watchers = (
            await self.session.scalars(
                select(ActiveWatcher).where(
                    ActiveWatcher.condition_type == CONDITION,
                    ActiveWatcher.is_active.is_(True),
                    ActiveWatcher.status == "pending",
                )
            )
        ).all()
        max_age = min(
            [
                RiskWatchPolicy.model_validate(w.condition_params_json).max_age_hours
                for w in watchers
            ]
            or [96]
        )
        result = evaluate_packet(packet, max_age_hours=max_age)
        packet_hash = digest(packet)
        # Daily data is polled more often than it changes. Keep the first exact
        # capture of equivalent inputs, not another full packet every five minutes.
        economic_inputs = {
            "sources": {
                ticker: {
                    key: value
                    for key, value in snapshot.items()
                    if key
                    in {
                        "provider",
                        "reported_symbol",
                        "reported_exchange",
                        "exchange_timezone",
                        "currency",
                        "price_basis",
                        "historical_availability_verified",
                        "status",
                    }
                }
                for ticker, snapshot in packet["snapshots"].items()
            },
            "positions": packet["positions"],
            "result": {key: value for key, value in result.items() if key != "cutoff"},
            "bars": {
                ticker: [
                    {key: value for key, value in bar.items() if key != "available_at"}
                    for bar in snap.get("daily_observations", [])
                ]
                for ticker, snap in packet["snapshots"].items()
            },
        }
        input_hash = digest(economic_inputs)
        prior = await self.latest()
        # Persist the complete packet, including acquisition clocks. Every capture
        # has an immutable identity and an exact content hash; retry is idempotent.
        record_id = uuid5(NAMESPACE_URL, f"prophet:{VERSION}:{packet_hash}")
        now = datetime.now(UTC)
        if (
            prior is not None
            and prior.shock_parameters_json.get("input_hash") == input_hash
        ):
            record_id = prior.id
            packet_hash = prior.shock_parameters_json["packet_hash"]
            prior.shock_parameters_json = {
                **prior.shock_parameters_json,
                "last_checked_at": now.isoformat(),
            }
        else:
            await self.session.execute(
                insert(ScenarioAnalysis)
                .values(
                    id=record_id,
                    name=SCENARIO_NAME,
                    scenario_description="Current holdings under simultaneous observed daily returns; not a forecast.",
                    shock_parameters_json={
                        "packet_hash": packet_hash,
                        "input_hash": input_hash,
                        "packet": packet,
                        "last_checked_at": now.isoformat(),
                    },
                    portfolio_impact_json=result,
                    total_portfolio_impact=-(result["historical_stress_loss"] or 0),
                    computed_at=now,
                )
                .on_conflict_do_nothing(index_elements=[ScenarioAnalysis.id])
            )
        for watcher in watchers:
            policy = RiskWatchPolicy.model_validate(watcher.condition_params_json)
            decision = budget_decision(result, policy)
            watcher.last_checked_at = now
            watcher.trigger_detail = decision["detail"]
            watcher.action_taken_json = {
                "assessment_id": str(record_id),
                "packet_hash": packet_hash,
                "decision": decision,
                "evaluated_at": now.isoformat(),
            }
            if decision["state"] == "breached":
                watcher.status = "triggered"
                watcher.is_active = False
                watcher.triggered_at = now
                await PushNotificationService(self.session).enqueue_watch_transition(
                    watcher
                )
        await self.session.commit()
        return await self.state()

    async def packet(self, assessment_id: UUID) -> dict | None:
        row = await self.session.get(ScenarioAnalysis, assessment_id)
        if row is None or row.name != SCENARIO_NAME:
            return None
        return row.shock_parameters_json
