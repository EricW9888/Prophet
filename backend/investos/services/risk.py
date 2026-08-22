from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import delete, desc, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from investos.models.benchmark import Benchmark
from investos.models.entity import Entity, Security
from investos.models.portfolio import Position, Transaction
from investos.models.quant import (
    AttributionResult,
    FactorExposure,
    RegimeState,
    ScenarioAnalysis,
)
from investos.schemas.benchmark import BenchmarkCreate, BenchmarkResponse
from investos.schemas.risk import (
    ExposureItemResponse,
    PerformanceAttributionItemResponse,
    PerformanceAttributionResponse,
    RegimeStateResponse,
    RiskSummaryResponse,
    ScenarioSummaryResponse,
)
from investos.services.market_data import MarketDataService
from investos.services.portfolio import PortfolioService
from investos.services.runtime_settings import RuntimeSettingsStore


def _to_float(value) -> float:
    if value is None:
        return 0.0
    if isinstance(value, Decimal):
        return float(value)
    return float(value)


class BenchmarkService:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def list_benchmarks(self) -> list[BenchmarkResponse]:
        rows = (
            (
                await self.session.execute(
                    select(Benchmark).order_by(Benchmark.name.asc())
                )
            )
            .scalars()
            .all()
        )
        return [self._serialize(item) for item in rows]

    async def create_benchmark(self, payload: BenchmarkCreate) -> BenchmarkResponse:
        ticker = (payload.ticker or "").strip().upper()
        existing = None
        if ticker:
            existing = (
                (
                    await self.session.execute(
                        select(Benchmark)
                        .where(Benchmark.ticker == ticker)
                        .order_by(desc(Benchmark.created_at))
                        .limit(1)
                    )
                )
                .scalars()
                .first()
            )
        if existing is not None:
            return self._serialize(existing)

        benchmark = Benchmark(
            ticker=ticker or None,
            name=(payload.name or ticker or "Custom benchmark").strip(),
            description=payload.description.strip() if payload.description else None,
            benchmark_type=payload.benchmark_type.strip() or "broad_market",
        )
        self.session.add(benchmark)
        await self.session.commit()
        await self.session.refresh(benchmark)
        return self._serialize(benchmark)

    async def ensure_default_benchmark(self) -> Benchmark:
        ticker = (
            RuntimeSettingsStore.load()
            .portfolio.default_benchmark_ticker.strip()
            .upper()
        )
        benchmark = (
            (
                await self.session.execute(
                    select(Benchmark)
                    .where(Benchmark.ticker == ticker)
                    .order_by(desc(Benchmark.created_at))
                    .limit(1)
                )
            )
            .scalars()
            .first()
        )
        if benchmark is not None:
            return benchmark

        benchmark = Benchmark(
            ticker=ticker,
            name=ticker,
            description="Auto-created default benchmark.",
            benchmark_type="broad_market",
        )
        self.session.add(benchmark)
        await self.session.flush()
        return benchmark

    def _serialize(self, benchmark: Benchmark) -> BenchmarkResponse:
        return BenchmarkResponse(
            id=benchmark.id,
            ticker=benchmark.ticker,
            name=benchmark.name,
            description=benchmark.description,
            benchmark_type=benchmark.benchmark_type,
            created_at=benchmark.created_at,
        )


class RiskService:
    AUTO_SCENARIO_PREFIX = "AUTO:"
    PERFORMANCE_ACTIONS = {"buy", "sell", "dividend", "split"}

    def __init__(self, session: AsyncSession):
        self.session = session
        self.market_data = MarketDataService(session)

    async def get_summary(self, *, refresh: bool = False) -> RiskSummaryResponse:
        if refresh:
            return await self.refresh_summary()

        cached = await self._cached_summary()
        if cached is not None:
            return cached
        return await self.refresh_summary()

    async def get_performance_attribution(
        self,
        *,
        window_days: int = 21,
    ) -> PerformanceAttributionResponse:
        """Explain invested-holdings performance with dated prices and actual cash flows.

        This is Modified Dietz attribution over the requested calendar window. It
        deliberately excludes securities whose quantity cannot be reversed from
        the supported transaction history instead of forcing a plausible-looking
        contribution through an unknown corporate action.
        """
        window_days = max(1, min(int(window_days or 21), 1825))
        as_of = datetime.now(UTC)
        period_start = as_of - timedelta(days=window_days)
        benchmark = await BenchmarkService(self.session).ensure_default_benchmark()

        position_rows = (
            await self.session.execute(
                select(Position, Security, Entity)
                .join(Security, Position.security_id == Security.id)
                .join(Entity, Security.entity_id == Entity.id)
                .where(Position.list_type.in_(("holding", "closed")))
            )
        ).all()
        position_ids = [position.id for position, _, _ in position_rows]
        transactions = []
        if position_ids:
            transactions = list(
                (
                    await self.session.execute(
                        select(Transaction)
                        .where(
                            Transaction.position_id.in_(position_ids),
                            Transaction.executed_at >= period_start,
                            Transaction.executed_at <= as_of,
                            PortfolioService.active_transaction_clause(),
                        )
                        .order_by(Transaction.executed_at.asc(), Transaction.id.asc())
                    )
                )
                .scalars()
                .all()
            )

        position_to_security = {
            position.id: security.id for position, security, _ in position_rows
        }
        transactions_by_security: dict[object, list[Transaction]] = defaultdict(list)
        for transaction in transactions:
            security_id = position_to_security.get(transaction.position_id)
            if security_id is not None:
                transactions_by_security[security_id].append(transaction)

        positions_by_security: dict[object, list[tuple[Position, Security, Entity]]] = (
            defaultdict(list)
        )
        for row in position_rows:
            positions_by_security[row[1].id].append(row)

        relevant: list[tuple[object, list[tuple[Position, Security, Entity]]]] = []
        for security_id, rows in positions_by_security.items():
            end_quantity = sum(_to_float(position.quantity) for position, _, _ in rows)
            if end_quantity > 0 or transactions_by_security.get(security_id):
                relevant.append((security_id, rows))

        tickers = sorted({rows[0][1].ticker.upper() for _, rows in relevant})
        chart_by_ticker = await self._fetch_chart_batch(
            tickers,
            period_start=period_start - timedelta(days=7),
            period_end=as_of + timedelta(days=1),
        )

        items: list[PerformanceAttributionItemResponse] = []
        unavailable: list[str] = []
        denominator_total = 0.0
        pending_contributions: list[tuple[dict[str, object], float]] = []

        for security_id, rows in relevant:
            security = rows[0][1]
            entity = rows[0][2]
            ticker = security.ticker.upper()
            chart = chart_by_ticker.get(ticker) or {}
            series = list(chart.get("series") or [])
            dated_series = [item for item in series if item[0] >= period_start]
            security_transactions = transactions_by_security.get(security_id, [])
            unsupported_actions = sorted(
                {
                    str(transaction.action or "unknown").lower()
                    for transaction in security_transactions
                    if str(transaction.action or "").lower()
                    not in self.PERFORMANCE_ACTIONS
                }
            )
            if unsupported_actions or not dated_series:
                unavailable.append(ticker)
                continue

            end_quantity = sum(_to_float(position.quantity) for position, _, _ in rows)
            start_quantity = self._reverse_start_quantity(
                end_quantity, security_transactions
            )
            if start_quantity < -1e-8:
                unavailable.append(ticker)
                continue
            start_quantity = max(0.0, start_quantity)
            start_price_time, start_price = dated_series[0]
            end_price_time, end_price = dated_series[-1]
            values = self._modified_dietz_values(
                start_quantity=start_quantity,
                end_quantity=end_quantity,
                start_price=float(start_price),
                end_price=float(end_price),
                transactions=security_transactions,
                period_start=period_start,
                period_end=as_of,
            )
            denominator_total += values["denominator"]
            pending_contributions.append(
                (
                    {
                        "ticker": ticker,
                        "name": entity.name,
                        "sector": (entity.sector or "Unclassified").strip(),
                        "start_quantity": start_quantity,
                        "end_quantity": end_quantity,
                        "start_price": float(start_price),
                        "end_price": float(end_price),
                        "start_price_time": start_price_time,
                        "end_price_time": end_price_time,
                        "beginning_value": values["beginning_value"],
                        "ending_value": values["ending_value"],
                        "net_flow": values["net_flow"],
                        "gain": values["gain"],
                        "return_pct": values["return_pct"],
                        "capital_return_pct": values["capital_return_pct"],
                        "transaction_count": len(security_transactions),
                    },
                    values["gain"],
                )
            )

        for payload, gain in pending_contributions:
            payload["contribution_pct"] = (
                (gain / denominator_total) * 100 if denominator_total > 0 else 0.0
            )
            items.append(PerformanceAttributionItemResponse(**payload))
        items.sort(key=lambda item: item.gain)

        total_beginning_value = sum(item.beginning_value for item in items)
        total_ending_value = sum(item.ending_value for item in items)
        net_flow = sum(item.net_flow for item in items)
        gain = sum(item.gain for item in items)
        return_pct = (gain / denominator_total) * 100 if denominator_total > 0 else None

        benchmark_return_pct = None
        if benchmark.ticker:
            benchmark_chart = (
                await self._fetch_chart_batch(
                    [benchmark.ticker.upper()],
                    period_start=period_start - timedelta(days=7),
                    period_end=as_of + timedelta(days=1),
                )
            ).get(benchmark.ticker.upper()) or {}
            benchmark_series = [
                item
                for item in list(benchmark_chart.get("series") or [])
                if item[0] >= period_start
            ]
            if len(benchmark_series) >= 2 and benchmark_series[0][1]:
                benchmark_return_pct = (
                    (benchmark_series[-1][1] / benchmark_series[0][1]) - 1.0
                ) * 100

        total_positions = len(relevant)
        covered_positions = len(items)
        response = PerformanceAttributionResponse(
            as_of=as_of,
            period_start=period_start,
            window_days=window_days,
            method=(
                "Modified Dietz on invested holdings using dated daily closes and settled "
                "buy, sell, dividend, and split transactions. Cash and unsupported corporate "
                "actions are excluded."
            ),
            total_beginning_value=total_beginning_value,
            total_ending_value=total_ending_value,
            net_flow=net_flow,
            gain=gain,
            return_pct=return_pct,
            benchmark_ticker=benchmark.ticker,
            benchmark_return_pct=benchmark_return_pct,
            active_return_pct=(
                return_pct - benchmark_return_pct
                if return_pct is not None and benchmark_return_pct is not None
                else None
            ),
            covered_positions=covered_positions,
            total_positions=total_positions,
            coverage_pct=(
                (covered_positions / total_positions) * 100 if total_positions else 0.0
            ),
            unavailable_tickers=sorted(set(unavailable)),
            items=items,
        )
        await self._persist_performance_attribution(response)
        await self.session.commit()
        return response

    async def get_cached_performance_attribution(
        self,
        *,
        window_days: int = 21,
    ) -> PerformanceAttributionResponse | None:
        rows = (
            (
                await self.session.execute(
                    select(AttributionResult)
                    .where(
                        AttributionResult.portfolio_wide.is_(True),
                        AttributionResult.factor_contributions_json["kind"].astext
                        == "modified_dietz",
                    )
                    .order_by(desc(AttributionResult.computed_at))
                    .limit(12)
                )
            )
            .scalars()
            .all()
        )
        for row in rows:
            details = row.factor_contributions_json or {}
            if int(details.get("window_days") or 0) != int(window_days):
                continue
            payload = details.get("response")
            if isinstance(payload, dict):
                return PerformanceAttributionResponse.model_validate(payload)
        return None

    async def refresh_summary(self) -> RiskSummaryResponse:
        benchmark = await BenchmarkService(self.session).ensure_default_benchmark()
        as_of = datetime.now(UTC)

        rows = (
            await self.session.execute(
                select(Position, Security, Entity)
                .join(Security, Position.security_id == Security.id)
                .join(Entity, Security.entity_id == Entity.id)
                .where(Position.list_type == "holding", Position.quantity > 0)
            )
        ).all()
        if not rows:
            await self.session.commit()
            return RiskSummaryResponse(
                as_of=as_of,
                active_benchmark=BenchmarkResponse(
                    id=benchmark.id,
                    ticker=benchmark.ticker,
                    name=benchmark.name,
                    description=benchmark.description,
                    benchmark_type=benchmark.benchmark_type,
                    created_at=benchmark.created_at,
                ),
            )

        total_market_value = sum(
            _to_float(position.market_value) for position, _, _ in rows
        )
        total_cost_basis = sum(
            _to_float(position.quantity) * _to_float(position.avg_cost_basis)
            for position, _, _ in rows
        )
        measurement_start = await self._measurement_start(rows)

        position_exposures: list[ExposureItemResponse] = []
        sector_totals: dict[str, float] = defaultdict(float)
        asset_class_totals: dict[str, float] = defaultdict(float)
        top_sector = None
        top_sector_weight_pct = 0.0
        top_holding = None
        top_holding_weight_pct = 0.0
        concentration_hhi = 0.0

        for position, security, entity in rows:
            weight_pct = _to_float(position.weight_pct)
            if weight_pct <= 0 and total_market_value > 0:
                weight_pct = (
                    _to_float(position.market_value) / total_market_value
                ) * 100
            ticker = security.ticker.upper()
            position_exposures.append(
                ExposureItemResponse(
                    label=ticker,
                    weight_pct=weight_pct,
                    detail=entity.name,
                )
            )
            sector = (entity.sector or "Unclassified").strip()
            sector_totals[sector] += weight_pct
            asset_class = (security.asset_class or "unknown").strip()
            asset_class_totals[asset_class] += weight_pct
            concentration_hhi += weight_pct * weight_pct
            if weight_pct > top_holding_weight_pct:
                top_holding_weight_pct = weight_pct
                top_holding = ticker

        sector_exposures = sorted(
            [
                ExposureItemResponse(label=label, weight_pct=weight, detail="sector")
                for label, weight in sector_totals.items()
            ],
            key=lambda item: item.weight_pct,
            reverse=True,
        )
        asset_class_exposures = sorted(
            [
                ExposureItemResponse(
                    label=label, weight_pct=weight, detail="asset_class"
                )
                for label, weight in asset_class_totals.items()
            ],
            key=lambda item: item.weight_pct,
            reverse=True,
        )
        top_positions = sorted(
            position_exposures, key=lambda item: item.weight_pct, reverse=True
        )[:5]
        if sector_exposures:
            top_sector = sector_exposures[0].label
            top_sector_weight_pct = sector_exposures[0].weight_pct

        portfolio_return_frac = None
        if total_cost_basis > 0:
            portfolio_return_frac = (
                total_market_value - total_cost_basis
            ) / total_cost_basis

        benchmark_current_price = None
        benchmark_return_frac = None
        current_regime = None
        if benchmark.ticker:
            chart = await self.market_data.fetch_chart_series(
                benchmark.ticker,
                period_start=(
                    (measurement_start - timedelta(days=10))
                    if measurement_start
                    else (as_of - timedelta(days=90))
                ),
                period_end=as_of + timedelta(days=1),
            )
            benchmark_current_price = chart["current_price"]
            series = chart["series"]
            if measurement_start and series:
                start_price = self._first_price_on_or_after(series, measurement_start)
                if start_price and benchmark_current_price:
                    benchmark_return_frac = (
                        benchmark_current_price - start_price
                    ) / start_price

            month_snapshot = await self.market_data.fetch_chart_series(
                benchmark.ticker, range_value="3mo"
            )
            current_regime = await self._persist_regime(
                as_of=as_of,
                benchmark_ticker=benchmark.ticker,
                series=month_snapshot["series"],
            )

        active_return_frac = None
        if portfolio_return_frac is not None and benchmark_return_frac is not None:
            active_return_frac = portfolio_return_frac - benchmark_return_frac

        await self._persist_factor_exposures(
            as_of, sector_exposures, asset_class_exposures, top_positions
        )
        await self._persist_attribution(
            benchmark=benchmark,
            as_of=as_of,
            measurement_start=measurement_start or as_of,
            benchmark_current_price=benchmark_current_price,
            portfolio_return_frac=portfolio_return_frac,
            benchmark_return_frac=benchmark_return_frac,
            active_return_frac=active_return_frac,
            top_sector=top_sector,
            top_sector_weight_pct=top_sector_weight_pct,
            top_holding=top_holding,
            top_holding_weight_pct=top_holding_weight_pct,
            concentration_hhi=concentration_hhi,
        )
        scenarios = await self._persist_scenarios(
            as_of=as_of,
            total_market_value=total_market_value,
            top_positions=top_positions,
        )
        await self.session.commit()

        return RiskSummaryResponse(
            as_of=as_of,
            active_benchmark=BenchmarkResponse(
                id=benchmark.id,
                ticker=benchmark.ticker,
                name=benchmark.name,
                description=benchmark.description,
                benchmark_type=benchmark.benchmark_type,
                created_at=benchmark.created_at,
            ),
            benchmark_current_price=benchmark_current_price,
            portfolio_return_pct=self._pct(portfolio_return_frac),
            benchmark_return_pct=self._pct(benchmark_return_frac),
            active_return_pct=self._pct(active_return_frac),
            measurement_start=measurement_start,
            top_sector=top_sector,
            top_sector_weight_pct=top_sector_weight_pct,
            top_holding=top_holding,
            top_holding_weight_pct=top_holding_weight_pct,
            concentration_hhi=concentration_hhi,
            sector_exposures=sector_exposures,
            asset_class_exposures=asset_class_exposures,
            top_positions=top_positions,
            current_regime=current_regime,
            scenarios=scenarios,
        )

    async def _cached_summary(self) -> RiskSummaryResponse | None:
        benchmark = await BenchmarkService(self.session).ensure_default_benchmark()
        attribution = (
            await self.session.execute(
                select(AttributionResult)
                .where(
                    AttributionResult.portfolio_wide.is_(True),
                    or_(
                        AttributionResult.factor_contributions_json["kind"].astext.is_(
                            None
                        ),
                        AttributionResult.factor_contributions_json["kind"].astext
                        != "modified_dietz",
                    ),
                )
                .order_by(desc(AttributionResult.computed_at))
                .limit(1)
            )
        ).scalar_one_or_none()
        if attribution is None:
            return None

        factors = (
            (
                await self.session.execute(
                    select(FactorExposure)
                    .where(FactorExposure.portfolio_wide.is_(True))
                    .order_by(FactorExposure.factor_name.asc())
                )
            )
            .scalars()
            .all()
        )
        scenarios = (
            (
                await self.session.execute(
                    select(ScenarioAnalysis)
                    .where(ScenarioAnalysis.name.startswith(self.AUTO_SCENARIO_PREFIX))
                    .order_by(desc(ScenarioAnalysis.computed_at))
                    .limit(3)
                )
            )
            .scalars()
            .all()
        )
        regime = (
            await self.session.execute(
                select(RegimeState)
                .where(RegimeState.end_date.is_(None))
                .order_by(desc(RegimeState.computed_at))
                .limit(1)
            )
        ).scalar_one_or_none()

        sector_exposures = self._factors_for_prefix(factors, "sector:")
        asset_class_exposures = self._factors_for_prefix(factors, "asset_class:")
        top_positions = self._factors_for_prefix(factors, "single_name:")[:5]
        details = attribution.factor_contributions_json or {}
        return RiskSummaryResponse(
            as_of=attribution.computed_at,
            active_benchmark=BenchmarkResponse(
                id=benchmark.id,
                ticker=benchmark.ticker,
                name=benchmark.name,
                description=benchmark.description,
                benchmark_type=benchmark.benchmark_type,
                created_at=benchmark.created_at,
            ),
            benchmark_current_price=self._nullable_float(
                details.get("benchmark_current_price")
            ),
            portfolio_return_pct=self._pct(attribution.total_return),
            benchmark_return_pct=self._pct(attribution.factor_return),
            active_return_pct=self._pct(attribution.idiosyncratic_return),
            measurement_start=attribution.period_start,
            top_sector=details.get("top_sector"),
            top_sector_weight_pct=float(details.get("top_sector_weight_pct") or 0.0),
            top_holding=details.get("top_holding"),
            top_holding_weight_pct=float(details.get("top_holding_weight_pct") or 0.0),
            concentration_hhi=float(details.get("concentration_hhi") or 0.0),
            sector_exposures=sector_exposures,
            asset_class_exposures=asset_class_exposures,
            top_positions=top_positions,
            current_regime=(
                None
                if regime is None
                else RegimeStateResponse(
                    regime_type=regime.regime_type,
                    confidence=regime.confidence,
                    signal_source=regime.signal_source,
                    start_date=regime.start_date,
                    end_date=regime.end_date,
                )
            ),
            scenarios=[
                ScenarioSummaryResponse(
                    name=item.name,
                    scenario_description=item.scenario_description,
                    total_portfolio_impact=item.total_portfolio_impact,
                    portfolio_impact_json=item.portfolio_impact_json or {},
                    computed_at=item.computed_at,
                )
                for item in scenarios
            ],
        )

    async def _measurement_start(self, rows) -> datetime | None:
        position_ids = [position.id for position, _, _ in rows]
        first_txn = (
            await self.session.execute(
                select(Transaction.executed_at)
                .where(Transaction.position_id.in_(position_ids))
                .order_by(Transaction.executed_at.asc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if first_txn is not None:
            return first_txn
        timestamps = [
            position.added_at
            for position, _, _ in rows
            if position.added_at is not None
        ]
        return min(timestamps) if timestamps else None

    async def _fetch_chart_batch(
        self,
        tickers: list[str],
        *,
        period_start: datetime,
        period_end: datetime,
        concurrency: int = 4,
    ) -> dict[str, dict[str, object]]:
        semaphore = asyncio.Semaphore(max(1, min(int(concurrency or 4), 8)))

        async def fetch_one(ticker: str) -> tuple[str, dict[str, object] | None]:
            async with semaphore:
                try:
                    chart = await self.market_data.fetch_chart_series(
                        ticker,
                        period_start=period_start,
                        period_end=period_end,
                    )
                    return ticker, chart
                except Exception:
                    return ticker, None

        normalized = sorted(
            {ticker.strip().upper() for ticker in tickers if ticker.strip()}
        )
        rows = await asyncio.gather(*(fetch_one(ticker) for ticker in normalized))
        return {ticker: chart for ticker, chart in rows if chart is not None}

    @classmethod
    def _reverse_start_quantity(
        cls,
        end_quantity: float,
        transactions: list[Transaction],
    ) -> float:
        quantity = float(end_quantity)
        for transaction in reversed(transactions):
            action = str(transaction.action or "").lower()
            transaction_quantity = _to_float(transaction.quantity)
            if action == "buy":
                quantity -= transaction_quantity
            elif action == "sell":
                quantity += transaction_quantity
            elif action == "split" and transaction_quantity > 0:
                quantity /= transaction_quantity
        return quantity

    @staticmethod
    def _modified_dietz_values(
        *,
        start_quantity: float,
        end_quantity: float,
        start_price: float,
        end_price: float,
        transactions: list[Transaction],
        period_start: datetime,
        period_end: datetime,
    ) -> dict[str, float | None]:
        beginning_value = start_quantity * start_price
        ending_value = end_quantity * end_price
        period_seconds = max((period_end - period_start).total_seconds(), 1.0)
        net_flow = 0.0
        weighted_flow = 0.0
        contributed_capital = 0.0

        for transaction in transactions:
            action = str(transaction.action or "").lower()
            quantity = _to_float(transaction.quantity)
            price = _to_float(transaction.price)
            if action == "buy":
                flow = quantity * price
                contributed_capital += flow
            elif action == "sell":
                flow = -(quantity * price)
            elif action == "dividend":
                flow = -price
            else:
                flow = 0.0
            net_flow += flow
            remaining_seconds = max(
                0.0,
                min(
                    period_seconds,
                    (period_end - transaction.executed_at).total_seconds(),
                ),
            )
            weighted_flow += flow * (remaining_seconds / period_seconds)

        gain = ending_value - beginning_value - net_flow
        denominator = beginning_value + weighted_flow
        capital_base = beginning_value + contributed_capital
        return_pct = (gain / denominator) * 100 if denominator > 0 else None
        capital_return_pct = (gain / capital_base) * 100 if capital_base > 0 else None
        return {
            "beginning_value": beginning_value,
            "ending_value": ending_value,
            "net_flow": net_flow,
            "weighted_flow": weighted_flow,
            "gain": gain,
            "denominator": denominator,
            "return_pct": return_pct,
            "capital_return_pct": capital_return_pct,
        }

    async def _persist_factor_exposures(
        self,
        as_of: datetime,
        sector_exposures: list[ExposureItemResponse],
        asset_class_exposures: list[ExposureItemResponse],
        top_positions: list[ExposureItemResponse],
    ) -> None:
        await self.session.execute(
            delete(FactorExposure).where(FactorExposure.portfolio_wide.is_(True))
        )
        for prefix, items in (
            ("sector:", sector_exposures),
            ("asset_class:", asset_class_exposures),
            ("single_name:", top_positions),
        ):
            for item in items:
                self.session.add(
                    FactorExposure(
                        position_id=None,
                        portfolio_wide=True,
                        factor_name=f"{prefix}{item.label}",
                        exposure_value=item.weight_pct,
                        as_of_date=as_of,
                    )
                )

    async def _persist_attribution(
        self,
        *,
        benchmark: Benchmark,
        as_of: datetime,
        measurement_start: datetime,
        benchmark_current_price: float | None,
        portfolio_return_frac: float | None,
        benchmark_return_frac: float | None,
        active_return_frac: float | None,
        top_sector: str | None,
        top_sector_weight_pct: float,
        top_holding: str | None,
        top_holding_weight_pct: float,
        concentration_hhi: float,
    ) -> None:
        self.session.add(
            AttributionResult(
                position_id=None,
                portfolio_wide=True,
                period_start=measurement_start,
                period_end=as_of,
                total_return=portfolio_return_frac or 0.0,
                factor_return=benchmark_return_frac or 0.0,
                idiosyncratic_return=active_return_frac or 0.0,
                factor_contributions_json={
                    "kind": "cost_basis_reference",
                    "benchmark_ticker": benchmark.ticker,
                    "benchmark_name": benchmark.name,
                    "benchmark_current_price": benchmark_current_price,
                    "top_sector": top_sector,
                    "top_sector_weight_pct": top_sector_weight_pct,
                    "top_holding": top_holding,
                    "top_holding_weight_pct": top_holding_weight_pct,
                    "concentration_hhi": concentration_hhi,
                },
            )
        )

    async def _persist_performance_attribution(
        self,
        response: PerformanceAttributionResponse,
    ) -> None:
        existing = (
            (
                await self.session.execute(
                    select(AttributionResult).where(
                        AttributionResult.portfolio_wide.is_(True),
                        AttributionResult.factor_contributions_json["kind"].astext
                        == "modified_dietz",
                    )
                )
            )
            .scalars()
            .all()
        )
        for row in existing:
            details = row.factor_contributions_json or {}
            if int(details.get("window_days") or 0) == response.window_days:
                await self.session.delete(row)

        self.session.add(
            AttributionResult(
                position_id=None,
                portfolio_wide=True,
                period_start=response.period_start,
                period_end=response.as_of,
                total_return=(response.return_pct or 0.0) / 100.0,
                factor_return=(response.benchmark_return_pct or 0.0) / 100.0,
                idiosyncratic_return=(response.active_return_pct or 0.0) / 100.0,
                factor_contributions_json={
                    "kind": "modified_dietz",
                    "window_days": response.window_days,
                    "response": response.model_dump(mode="json"),
                },
                computed_at=response.as_of,
            )
        )

    async def _persist_regime(
        self,
        *,
        as_of: datetime,
        benchmark_ticker: str,
        series: list[tuple[datetime, float]],
    ) -> RegimeStateResponse | None:
        if not series:
            return None

        current_price = series[-1][1]
        month_reference = (
            self._latest_price_before(series, as_of - timedelta(days=30))
            or series[0][1]
        )
        month_return = (
            ((current_price - month_reference) / month_reference)
            if month_reference
            else 0.0
        )
        if month_return <= -0.07:
            regime_type = "crisis"
        elif month_return < -0.02:
            regime_type = "risk_off"
        elif month_return >= 0.08:
            regime_type = "euphoria"
        elif month_return > 0.02:
            regime_type = "risk_on"
        else:
            regime_type = "transition"
        confidence = min(0.95, max(0.35, abs(month_return) * 5))

        current = (
            await self.session.execute(
                select(RegimeState)
                .where(RegimeState.end_date.is_(None))
                .order_by(desc(RegimeState.computed_at))
                .limit(1)
            )
        ).scalar_one_or_none()
        if current is not None and current.regime_type == regime_type:
            current.confidence = confidence
            current.signal_source = f"{benchmark_ticker} 30d return"
            current.computed_at = as_of
            return RegimeStateResponse(
                regime_type=current.regime_type,
                confidence=current.confidence,
                signal_source=current.signal_source,
                start_date=current.start_date,
                end_date=current.end_date,
            )

        if current is not None and current.end_date is None:
            current.end_date = as_of

        record = RegimeState(
            regime_type=regime_type,
            confidence=confidence,
            signal_source=f"{benchmark_ticker} 30d return",
            start_date=as_of,
        )
        self.session.add(record)
        await self.session.flush()
        return RegimeStateResponse(
            regime_type=record.regime_type,
            confidence=record.confidence,
            signal_source=record.signal_source,
            start_date=record.start_date,
            end_date=record.end_date,
        )

    async def _persist_scenarios(
        self,
        *,
        as_of: datetime,
        total_market_value: float,
        top_positions: list[ExposureItemResponse],
    ) -> list[ScenarioSummaryResponse]:
        await self.session.execute(
            delete(ScenarioAnalysis).where(
                ScenarioAnalysis.name.startswith(self.AUTO_SCENARIO_PREFIX)
            )
        )

        scenarios: list[ScenarioAnalysis] = []
        scenarios.append(
            ScenarioAnalysis(
                name=f"{self.AUTO_SCENARIO_PREFIX} Broad market down 10%",
                scenario_description="Assume beta-one market shock across the current portfolio.",
                shock_parameters_json={"market_move_pct": -10},
                portfolio_impact_json={
                    "aggregate_value_impact": -(total_market_value * 0.10)
                },
                total_portfolio_impact=-(total_market_value * 0.10),
            )
        )
        scenarios.append(
            ScenarioAnalysis(
                name=f"{self.AUTO_SCENARIO_PREFIX} Broad market up 10%",
                scenario_description="Assume beta-one relief rally across the current portfolio.",
                shock_parameters_json={"market_move_pct": 10},
                portfolio_impact_json={
                    "aggregate_value_impact": total_market_value * 0.10
                },
                total_portfolio_impact=total_market_value * 0.10,
            )
        )
        if top_positions:
            top = top_positions[0]
            impact = -(total_market_value * (top.weight_pct / 100.0) * 0.20)
            scenarios.append(
                ScenarioAnalysis(
                    name=f"{self.AUTO_SCENARIO_PREFIX} {top.label} drawdown",
                    scenario_description="Assume the top holding falls 20% while the rest of the portfolio is unchanged.",
                    shock_parameters_json={"holding": top.label, "move_pct": -20},
                    portfolio_impact_json={top.label: impact},
                    total_portfolio_impact=impact,
                )
            )

        for scenario in scenarios:
            self.session.add(scenario)
        await self.session.flush()

        return [
            ScenarioSummaryResponse(
                name=item.name,
                scenario_description=item.scenario_description,
                total_portfolio_impact=item.total_portfolio_impact,
                portfolio_impact_json=item.portfolio_impact_json or {},
                computed_at=item.computed_at,
            )
            for item in scenarios
        ]

    def _factors_for_prefix(
        self,
        factors: list[FactorExposure],
        prefix: str,
    ) -> list[ExposureItemResponse]:
        items = [
            ExposureItemResponse(
                label=item.factor_name[len(prefix) :],
                weight_pct=item.exposure_value,
                detail=prefix.rstrip(":"),
            )
            for item in factors
            if item.factor_name.startswith(prefix)
        ]
        return sorted(items, key=lambda item: item.weight_pct, reverse=True)

    def _first_price_on_or_after(
        self, series: list[tuple[datetime, float]], target: datetime
    ) -> float | None:
        for timestamp, price in series:
            if timestamp >= target:
                return price
        return series[0][1] if series else None

    def _latest_price_before(
        self, series: list[tuple[datetime, float]], target: datetime
    ) -> float | None:
        candidates = [price for timestamp, price in series if timestamp <= target]
        if candidates:
            return candidates[-1]
        return series[0][1] if series else None

    def _pct(self, value: float | None) -> float | None:
        if value is None:
            return None
        return value * 100

    def _nullable_float(self, value) -> float | None:
        if value is None:
            return None
        return float(value)
