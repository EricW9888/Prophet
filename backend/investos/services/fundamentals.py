from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import and_, desc, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from investos.models.entity import Security
from investos.models.evidence import RawEvidence, SourceItem
from investos.models.fundamental import FundamentalMetric
from investos.models.portfolio import Position
from investos.models.source import Source
from investos.services.graph_edge_state import GraphEdgeStateService

_FAMILY_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "valuation",
        (
            "p/e",
            "pe",
            "forward pe",
            "ev/ebitda",
            "price/sales",
            "multiple",
            "valuation",
        ),
    ),
    (
        "profitability",
        (
            "roe",
            "roic",
            "gross margin",
            "operating margin",
            "free cash flow",
            "fcf",
            "profitability",
        ),
    ),
    ("growth", ("revenue growth", "eps growth", "growth", "cagr", "bookings")),
    (
        "balance_sheet",
        (
            "debt",
            "leverage",
            "liquidity",
            "cash",
            "interest coverage",
            "maturity",
            "refinancing",
        ),
    ),
    ("estimate_revision", ("estimate", "revision", "consensus", "guidance")),
    (
        "sector_kpi",
        (
            "hbm",
            "nand",
            "asp",
            "wafer",
            "capacity",
            "utilization",
            "backlog",
            "unit",
            "subscriber",
        ),
    ),
)


class FundamentalMetricService:
    """Store and retrieve open-ended, source-dated financial metric evidence."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def create_metric(
        self,
        *,
        metric_name: str,
        metric_family: str | None = None,
        subject_type: str | None = None,
        subject_id: UUID | None = None,
        entity_id: UUID | None = None,
        security_id: UUID | None = None,
        ticker: str | None = None,
        raw_evidence_id: UUID | None = None,
        source_item_id: UUID | None = None,
        value_text: str | None = None,
        numeric_value: float | None = None,
        unit: str | None = None,
        currency: str | None = None,
        period_label: str | None = None,
        fiscal_year: int | None = None,
        fiscal_quarter: str | None = None,
        as_of: datetime | None = None,
        event_time: datetime | None = None,
        public_time: datetime | None = None,
        eligible_action_time: datetime | None = None,
        stale_after: datetime | None = None,
        direction: str | None = None,
        confidence: float = 0.5,
        investment_relevance: str | None = None,
        next_test: str | None = None,
        source_kind: str | None = None,
        freshness_status: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> FundamentalMetric:
        clean_name = self._required_text(metric_name, field="metric_name")
        family = self._text(metric_family) or self._family_from_metric(clean_name)
        resolved = await self._resolve_subject(
            subject_type=subject_type,
            subject_id=subject_id,
            entity_id=entity_id,
            security_id=security_id,
            ticker=ticker,
        )
        metadata_payload = dict(metadata or {})
        metadata_payload.setdefault("ingested_by", "fundamental_metric_service")
        metric = FundamentalMetric(
            subject_type=resolved["subject_type"],
            subject_id=resolved["subject_id"],
            entity_id=resolved["entity_id"],
            security_id=resolved["security_id"],
            ticker=resolved["ticker"],
            raw_evidence_id=raw_evidence_id,
            source_item_id=source_item_id,
            metric_name=clean_name,
            metric_family=family,
            value_text=self._text(value_text),
            numeric_value=self._finite(numeric_value),
            unit=self._text(unit),
            currency=self._text(currency),
            period_label=self._text(period_label),
            fiscal_year=self._int(fiscal_year),
            fiscal_quarter=self._text(fiscal_quarter),
            as_of=self._date(as_of) or self._date(public_time),
            event_time=self._date(event_time),
            public_time=self._date(public_time),
            eligible_action_time=self._date(eligible_action_time),
            stale_after=self._date(stale_after)
            or self._default_stale_after(
                family, self._date(as_of) or self._date(public_time)
            ),
            direction=self._text(direction),
            confidence=max(0.0, min(1.0, float(confidence or 0.0))),
            investment_relevance=self._text(investment_relevance),
            next_test=self._text(next_test),
            source_kind=self._text(source_kind),
            freshness_status=self._text(freshness_status) or "current",
            metadata_json=metadata_payload,
        )
        existing = await self._existing_metric(metric)
        if existing is not None:
            return existing
        self.session.add(metric)
        await self.session.flush()
        await self._attach_graph_edges(metric)
        await self.session.commit()
        await self.session.refresh(metric)
        return metric

    async def relevant_metrics(
        self,
        *,
        subject_type: str,
        subject_id: UUID | None,
        position_details: dict[UUID, dict] | None = None,
        limit: int = 12,
    ) -> list[dict[str, Any]]:
        clauses = []
        ticker_filter: set[str] = set()
        entity_filter: set[UUID] = set()
        security_filter: set[UUID] = set()

        if subject_type == "portfolio" and position_details:
            for detail in position_details.values():
                if detail.get("ticker"):
                    ticker_filter.add(str(detail["ticker"]).upper())
                if detail.get("entity_id"):
                    try:
                        entity_filter.add(UUID(str(detail["entity_id"])))
                    except (TypeError, ValueError):
                        pass
                if detail.get("security_id"):
                    try:
                        security_filter.add(UUID(str(detail["security_id"])))
                    except (TypeError, ValueError):
                        pass
            clauses.append(FundamentalMetric.subject_type == "portfolio")
        elif subject_type == "entity" and subject_id is not None:
            clauses.extend(
                [
                    (FundamentalMetric.subject_type == "entity")
                    & (FundamentalMetric.subject_id == subject_id),
                    FundamentalMetric.entity_id == subject_id,
                ]
            )
            securities = (
                (
                    await self.session.execute(
                        select(Security).where(Security.entity_id == subject_id)
                    )
                )
                .scalars()
                .all()
            )
            for security in securities:
                security_filter.add(security.id)
                ticker_filter.add(str(security.ticker).upper())
        elif subject_type == "position" and subject_id is not None:
            clauses.append(
                (FundamentalMetric.subject_type == "position")
                & (FundamentalMetric.subject_id == subject_id)
            )
            row = (
                await self.session.execute(
                    select(Position, Security)
                    .join(Security, Position.security_id == Security.id)
                    .where(Position.id == subject_id)
                    .limit(1)
                )
            ).first()
            if row is not None:
                position, security = row
                security_filter.add(security.id)
                entity_filter.add(security.entity_id)
                ticker_filter.add(str(security.ticker).upper())
        elif subject_id is not None:
            clauses.append(
                (FundamentalMetric.subject_type == subject_type)
                & (FundamentalMetric.subject_id == subject_id)
            )

        if ticker_filter:
            clauses.append(FundamentalMetric.ticker.in_(sorted(ticker_filter)))
        if entity_filter:
            clauses.append(FundamentalMetric.entity_id.in_(entity_filter))
        if security_filter:
            clauses.append(FundamentalMetric.security_id.in_(security_filter))
        if not clauses:
            return []

        rows = (
            (
                await self.session.execute(
                    select(FundamentalMetric)
                    .where(or_(*clauses))
                    .order_by(
                        desc(FundamentalMetric.as_of),
                        desc(FundamentalMetric.public_time),
                        desc(FundamentalMetric.created_at),
                    )
                    .limit(max(1, min(int(limit or 12), 50)))
                )
            )
            .scalars()
            .all()
        )
        return [await self._context_from_metric(metric) for metric in rows]

    async def refresh_freshness(self, *, now: datetime | None = None) -> dict[str, int]:
        current_time = now or datetime.now(UTC)
        stale_result = await self.session.execute(
            update(FundamentalMetric)
            .where(
                FundamentalMetric.stale_after.is_not(None),
                FundamentalMetric.stale_after <= current_time,
                FundamentalMetric.freshness_status != "stale",
            )
            .values(freshness_status="stale")
        )
        restored_result = await self.session.execute(
            update(FundamentalMetric)
            .where(
                FundamentalMetric.stale_after.is_not(None),
                FundamentalMetric.stale_after > current_time,
                FundamentalMetric.freshness_status == "stale",
            )
            .values(freshness_status="current")
        )
        await self.session.commit()
        return {
            "marked_stale": int(stale_result.rowcount or 0),
            "restored_current": int(restored_result.rowcount or 0),
        }

    async def _existing_metric(
        self, metric: FundamentalMetric
    ) -> FundamentalMetric | None:
        source_clauses = []
        if metric.raw_evidence_id is not None:
            source_clauses.append(
                FundamentalMetric.raw_evidence_id == metric.raw_evidence_id
            )
        if metric.source_item_id is not None:
            source_clauses.append(
                FundamentalMetric.source_item_id == metric.source_item_id
            )
        if not source_clauses:
            return None
        if metric.ticker:
            subject_clause = FundamentalMetric.ticker == metric.ticker
        elif metric.entity_id:
            subject_clause = FundamentalMetric.entity_id == metric.entity_id
        elif metric.subject_type and metric.subject_id:
            subject_clause = and_(
                FundamentalMetric.subject_type == metric.subject_type,
                FundamentalMetric.subject_id == metric.subject_id,
            )
        else:
            subject_clause = and_(
                FundamentalMetric.subject_type == metric.subject_type,
                FundamentalMetric.subject_id.is_(None),
                FundamentalMetric.ticker.is_(None),
            )
        stmt = (
            select(FundamentalMetric)
            .where(
                and_(
                    FundamentalMetric.metric_name == metric.metric_name,
                    FundamentalMetric.metric_family == metric.metric_family,
                    FundamentalMetric.period_label == metric.period_label,
                    subject_clause,
                    or_(*source_clauses),
                )
            )
            .order_by(desc(FundamentalMetric.created_at))
            .limit(1)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def _context_from_metric(self, metric: FundamentalMetric) -> dict[str, Any]:
        source_name = None
        source_type = None
        evidence_title = None
        url = None
        raw_id = metric.raw_evidence_id
        if raw_id is None and metric.source_item_id is not None:
            source_item = (
                await self.session.execute(
                    select(SourceItem).where(SourceItem.id == metric.source_item_id)
                )
            ).scalar_one_or_none()
            if source_item is not None:
                raw_id = source_item.raw_evidence_id
        if raw_id is not None:
            row = (
                await self.session.execute(
                    select(RawEvidence, Source)
                    .join(Source, RawEvidence.source_id == Source.id)
                    .where(RawEvidence.id == raw_id)
                    .limit(1)
                )
            ).first()
            if row is not None:
                raw, source = row
                source_name = source.name
                source_type = source.source_type
                evidence_title = raw.title
                url = raw.url
        return {
            "id": str(metric.id),
            "metric_name": metric.metric_name,
            "metric_family": metric.metric_family,
            "ticker": metric.ticker,
            "subject_type": metric.subject_type,
            "subject_id": str(metric.subject_id) if metric.subject_id else None,
            "value_text": metric.value_text,
            "numeric_value": (
                None if metric.numeric_value is None else float(metric.numeric_value)
            ),
            "unit": metric.unit,
            "currency": metric.currency,
            "period_label": metric.period_label,
            "fiscal_year": metric.fiscal_year,
            "fiscal_quarter": metric.fiscal_quarter,
            "as_of": metric.as_of.isoformat() if metric.as_of else None,
            "event_time": metric.event_time.isoformat() if metric.event_time else None,
            "public_time": (
                metric.public_time.isoformat() if metric.public_time else None
            ),
            "eligible_action_time": (
                metric.eligible_action_time.isoformat()
                if metric.eligible_action_time
                else None
            ),
            "stale_after": (
                metric.stale_after.isoformat() if metric.stale_after else None
            ),
            "direction": metric.direction,
            "confidence": float(metric.confidence or 0.0),
            "investment_relevance": metric.investment_relevance,
            "next_test": metric.next_test,
            "source_kind": metric.source_kind,
            "freshness_status": metric.freshness_status,
            "source_name": source_name,
            "source_type": source_type,
            "evidence_title": evidence_title,
            "url": url,
        }

    async def _resolve_subject(
        self,
        *,
        subject_type: str | None,
        subject_id: UUID | None,
        entity_id: UUID | None,
        security_id: UUID | None,
        ticker: str | None,
    ) -> dict[str, Any]:
        clean_ticker = self._upper(ticker)
        resolved_security_id = security_id
        resolved_entity_id = entity_id
        if resolved_security_id is not None:
            security = (
                await self.session.execute(
                    select(Security).where(Security.id == resolved_security_id)
                )
            ).scalar_one_or_none()
            if security is not None:
                resolved_entity_id = resolved_entity_id or security.entity_id
                clean_ticker = clean_ticker or self._upper(security.ticker)
        elif clean_ticker:
            security = (
                await self.session.execute(
                    select(Security)
                    .where(Security.ticker.ilike(clean_ticker))
                    .order_by(desc(Security.is_active))
                    .limit(1)
                )
            ).scalar_one_or_none()
            if security is not None:
                resolved_security_id = security.id
                resolved_entity_id = resolved_entity_id or security.entity_id
        if subject_type == "entity" and subject_id is not None:
            resolved_entity_id = resolved_entity_id or subject_id
        elif subject_type == "security" and subject_id is not None:
            resolved_security_id = resolved_security_id or subject_id
        if subject_type is None and resolved_entity_id is not None:
            subject_type = "entity"
            subject_id = resolved_entity_id
        return {
            "subject_type": self._text(subject_type),
            "subject_id": subject_id,
            "entity_id": resolved_entity_id,
            "security_id": resolved_security_id,
            "ticker": clean_ticker,
        }

    async def _attach_graph_edges(self, metric: FundamentalMetric) -> int:
        targets: list[tuple[str, UUID, str, float]] = []
        if metric.subject_type and metric.subject_id:
            targets.append(
                (metric.subject_type, metric.subject_id, "has_fundamental_metric", 0.9)
            )
        if metric.entity_id:
            targets.append(("entity", metric.entity_id, "has_fundamental_metric", 0.92))
        if metric.security_id:
            targets.append(
                ("security", metric.security_id, "has_fundamental_metric", 0.86)
            )
        if metric.raw_evidence_id:
            targets.append(
                ("raw_evidence", metric.raw_evidence_id, "sourced_from", 0.78)
            )
        if metric.source_item_id:
            targets.append(("source_item", metric.source_item_id, "sourced_from", 0.78))
        if metric.subject_type == "portfolio" or (metric.metadata_json or {}).get(
            "portfolio_relevant"
        ):
            targets.append(
                (
                    "portfolio",
                    UUID("00000000-0000-0000-0000-000000000000"),
                    "informs_portfolio_metric",
                    0.74,
                )
            )
        seen: set[tuple[str, UUID, str]] = set()
        edge_state = GraphEdgeStateService(self.session)
        created_count = 0
        for target_type, target_id, relationship_type, confidence in targets:
            key = (target_type, target_id, relationship_type)
            if key in seen:
                continue
            seen.add(key)
            _, created = await edge_state.ensure_edge(
                source_type="fundamental_metric",
                source_id=metric.id,
                target_type=target_type,
                target_id=target_id,
                relationship_type=relationship_type,
                confidence=confidence,
                reasoning=metric.investment_relevance,
                properties={
                    "origin": "fundamental_metric_service",
                    "metric_family": metric.metric_family,
                    "metric_name": metric.metric_name,
                },
            )
            created_count += int(created)
        return created_count

    @staticmethod
    def _family_from_metric(metric_name: str) -> str:
        compact = metric_name.casefold()
        for family, hints in _FAMILY_HINTS:
            if any(hint in compact for hint in hints):
                return family
        return "fundamental_metric"

    @staticmethod
    def _default_stale_after(
        metric_family: str, as_of: datetime | None
    ) -> datetime | None:
        if as_of is None:
            return None
        if metric_family in {"valuation", "estimate_revision"}:
            return as_of + timedelta(days=45)
        if metric_family in {"balance_sheet", "profitability", "growth"}:
            return as_of + timedelta(days=120)
        return as_of + timedelta(days=180)

    @staticmethod
    def _required_text(value: str | None, *, field: str) -> str:
        text = FundamentalMetricService._text(value)
        if not text:
            raise ValueError(f"{field}_required")
        return text

    @staticmethod
    def _text(value: Any) -> str | None:
        if value is None:
            return None
        text = " ".join(str(value).split()).strip()
        return text or None

    @staticmethod
    def _upper(value: Any) -> str | None:
        text = FundamentalMetricService._text(value)
        return text.upper() if text else None

    @staticmethod
    def _date(value: Any) -> datetime | None:
        if value is None or isinstance(value, datetime):
            return value
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                return None
        return None

    @staticmethod
    def _finite(value: Any) -> float | None:
        if value in (None, ""):
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(number):
            return None
        return number

    @staticmethod
    def _int(value: Any) -> int | None:
        if value in (None, ""):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
