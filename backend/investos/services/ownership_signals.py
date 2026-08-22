from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, time
from typing import Any
from uuid import UUID

from sqlalchemy import desc, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from investos.models.entity import Security
from investos.models.evidence import RawEvidence, SourceItem
from investos.models.portfolio import Position
from investos.models.review import ReviewQueueItem
from investos.models.source import Source

DISCLOSURE_SOURCE_TYPES = frozenset({"filing", "ownership_tracker"})
DISCLOSURE_ITEM_TYPES = frozenset(
    {
        "insider_disclosure",
        "ownership_disclosure",
        "institutional_flow",
        "congressional_trade_disclosure",
    }
)

_AMOUNT_RE = re.compile(r"[-+]?\$?\d[\d,]*(?:\.\d+)?")


@dataclass(frozen=True)
class OwnershipSignal:
    evidence_id: UUID | None
    title: str
    ticker: str | None
    issuer: str | None
    source_name: str | None
    source_type: str | None
    source_kind: str
    actor_name: str | None
    actor_type: str | None
    actor_role: str | None
    direction: str
    transaction_type: str | None
    transaction_value: float | None
    transaction_value_label: str | None
    shares: float | None
    price: float | None
    transaction_date: datetime | None
    disclosure_date: datetime | None
    disclosure_lag_days: float | None
    portfolio_weight_pct: float | None
    is_portfolio_linked: bool
    url: str | None
    value_score: float
    timeliness_score: float
    review_priority: float
    should_surface: bool
    review_trigger_reason: str
    next_test: str
    shadow_prompt: str

    def to_context(self) -> dict[str, Any]:
        payload = asdict(self)
        for key in ("transaction_date", "disclosure_date"):
            value = payload.get(key)
            if isinstance(value, datetime):
                payload[key] = value.isoformat()
        return payload


class OwnershipSignalService:
    """Normalize ownership/insider/political-flow disclosures into reviewable signals."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def ingest_disclosure(
        self,
        *,
        source_name: str,
        source_type: str = "ownership_tracker",
        source_url: str | None = None,
        source_description: str | None = None,
        source_item_type: str = "ownership_disclosure",
        title: str | None = None,
        url: str | None = None,
        external_id: str | None = None,
        author: str | None = None,
        metadata: dict[str, Any] | None = None,
        summary: str | None = None,
        event_time: datetime | None = None,
        public_time: datetime | None = None,
        eligible_action_time: datetime | None = None,
    ) -> RawEvidence:
        clean_source_type = self._validate_source_type(source_type)
        clean_item_type = self._validate_item_type(source_item_type)
        clean_source_name = self._required_text(source_name, field="source_name")
        source = await self._get_or_create_source(
            name=clean_source_name,
            source_type=clean_source_type,
            url=source_url,
            description=source_description,
        )

        if external_id:
            existing = (
                await self.session.execute(
                    select(RawEvidence)
                    .where(
                        RawEvidence.source_id == source.id,
                        RawEvidence.external_id == external_id,
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            if existing is not None:
                return existing

        metadata_payload = dict(metadata or {})
        metadata_payload.setdefault("ingested_by", "ownership_signal_service")
        metadata_payload.setdefault("source_of_source", clean_source_type)
        disclosure_public_time = public_time or self._date(
            self._first(
                metadata_payload,
                "disclosure_date",
                "filing_date",
                "reported_at",
                "public_time",
            )
        )
        disclosure_event_time = event_time or self._date(
            self._first(
                metadata_payload,
                "transaction_date",
                "trade_date",
                "executed_at",
                "event_time",
            )
        )

        evidence = RawEvidence(
            source_id=source.id,
            source_item_type=clean_item_type,
            external_id=external_id,
            title=title or self._title_from_metadata(clean_item_type, metadata_payload),
            raw_content_ref=None,
            url=url,
            author=author,
            metadata_json=metadata_payload,
            is_processed=bool(summary),
            event_time=disclosure_event_time,
            public_time=disclosure_public_time,
            eligible_action_time=eligible_action_time or disclosure_public_time,
        )
        self.session.add(evidence)
        await self.session.flush()
        if summary:
            self.session.add(
                SourceItem(
                    raw_evidence_id=evidence.id,
                    source_id=source.id,
                    extracted_text=summary,
                    summary=summary,
                    processing_status="processed",
                )
            )
        await self.session.commit()
        await self.session.refresh(evidence)
        await self._store_market_setup_signal(evidence, source)
        return evidence

    async def queue_review_items(self, *, limit: int = 75) -> int:
        portfolio_weights = await self.portfolio_weights_by_ticker()
        rows = (
            await self.session.execute(
                select(RawEvidence, Source)
                .join(Source, RawEvidence.source_id == Source.id)
                .where(
                    or_(
                        Source.source_type.in_(DISCLOSURE_SOURCE_TYPES),
                        RawEvidence.source_item_type.in_(DISCLOSURE_ITEM_TYPES),
                    )
                )
                .order_by(
                    desc(RawEvidence.public_time),
                    desc(RawEvidence.event_time),
                    desc(RawEvidence.created_at),
                )
                .limit(limit)
            )
        ).all()

        queued = 0
        for evidence, source in rows:
            signal = self.analyze_signal(
                evidence,
                source,
                portfolio_weights=portfolio_weights,
            )
            if signal is None or not signal.should_surface:
                continue
            self.session.add(
                ReviewQueueItem(
                    item_type="raw_evidence",
                    item_id=evidence.id,
                    priority_score=signal.review_priority,
                    size_factor=float(signal.portfolio_weight_pct or 0.0),
                    evidence_change_factor=signal.value_score,
                    contradiction_pressure=0.0,
                    thesis_drift=0.0,
                    catalyst_proximity=signal.timeliness_score,
                    coverage_weakness=0.0,
                    trigger_reason=signal.review_trigger_reason,
                )
            )
            queued += 1
        return queued

    async def portfolio_weights_by_ticker(self) -> dict[str, float]:
        rows = (
            await self.session.execute(
                select(Security.ticker, Position.weight_pct)
                .join(Position, Position.security_id == Security.id)
                .where(Position.list_type.in_(["holding", "watchlist", "considering"]))
            )
        ).all()
        weights: dict[str, float] = {}
        for ticker, weight in rows:
            if ticker:
                weights[str(ticker).upper()] = float(weight or 0.0)
        return weights

    async def _store_market_setup_signal(
        self, evidence: RawEvidence, source: Source
    ) -> None:
        signal = self.analyze_signal(
            evidence,
            source,
            portfolio_weights=await self.portfolio_weights_by_ticker(),
        )
        if signal is None:
            return
        from investos.services.market_setup import MarketSetupSignalService

        await MarketSetupSignalService(self.session).create_signal(
            signal_name=f"{signal.source_kind}: {signal.direction} {signal.ticker or signal.issuer or 'security'}",
            signal_family="ownership_or_institutional_flow",
            ticker=signal.ticker,
            raw_evidence_id=evidence.id,
            setup_context=signal.review_trigger_reason,
            actual_context=None,
            value_text=signal.transaction_value_label,
            numeric_value=signal.transaction_value,
            unit="USD" if signal.transaction_value is not None else None,
            as_of=signal.disclosure_date,
            event_time=signal.transaction_date,
            public_time=signal.disclosure_date,
            eligible_action_time=signal.disclosure_date,
            direction=signal.direction,
            confidence=0.65 if signal.is_portfolio_linked else 0.5,
            investment_relevance=(
                "Ownership, insider, political, or institutional-flow signals are timing evidence only; "
                "they matter when later mapped to a concrete thesis mechanism and outcome-scored."
            ),
            next_test=signal.next_test,
            source_kind=signal.source_kind,
            metadata={
                "source_of_source": signal.source_type,
                "actor_name": signal.actor_name,
                "actor_type": signal.actor_type,
                "actor_role": signal.actor_role,
                "disclosure_lag_days": signal.disclosure_lag_days,
                "portfolio_weight_pct": signal.portfolio_weight_pct,
                "portfolio_relevant": signal.is_portfolio_linked,
            },
        )

    @classmethod
    def is_ownership_signal(cls, evidence: Any, source: Any | None = None) -> bool:
        source_type = str(getattr(source, "source_type", "") or "").strip()
        item_type = str(getattr(evidence, "source_item_type", "") or "").strip()
        return (
            source_type in DISCLOSURE_SOURCE_TYPES or item_type in DISCLOSURE_ITEM_TYPES
        )

    @staticmethod
    def _validate_source_type(source_type: str) -> str:
        clean = (source_type or "").strip()
        if clean not in DISCLOSURE_SOURCE_TYPES:
            raise ValueError("source_type_must_be_filing_or_ownership_tracker")
        return clean

    @staticmethod
    def _validate_item_type(source_item_type: str) -> str:
        clean = (source_item_type or "").strip()
        if clean not in DISCLOSURE_ITEM_TYPES:
            raise ValueError("source_item_type_must_be_disclosure_type")
        return clean

    @classmethod
    def _title_from_metadata(
        cls, source_item_type: str, metadata: dict[str, Any]
    ) -> str:
        ticker = cls._upper(
            cls._first(metadata, "ticker", "symbol", "issuer_ticker", "security_ticker")
        )
        issuer = cls._text(
            cls._first(
                metadata, "issuer", "issuer_name", "company", "entity", "entity_name"
            )
        )
        actor = cls._text(
            cls._first(
                metadata,
                "actor_name",
                "insider_name",
                "politician_name",
                "filer_name",
                "owner_name",
                "fund_name",
                "reporting_owner",
                "reporter",
                "owner",
            )
        )
        subject = ticker or issuer or "unknown security"
        kind = source_item_type.replace("_", " ")
        return f"{subject} {kind}" if not actor else f"{subject} {kind} from {actor}"

    @staticmethod
    def _required_text(value: str | None, *, field: str) -> str:
        clean = " ".join((value or "").split())
        if not clean:
            raise ValueError(f"{field}_required")
        return clean

    async def _get_or_create_source(
        self,
        *,
        name: str,
        source_type: str,
        url: str | None,
        description: str | None,
    ) -> Source:
        clean_url = self._text(url)
        stmt = (
            select(Source)
            .where(Source.name == name, Source.source_type == source_type)
            .limit(1)
        )
        if clean_url:
            stmt = stmt.where(Source.url == clean_url)
        source = (await self.session.execute(stmt)).scalar_one_or_none()
        if source is not None:
            return source
        source = Source(
            name=name,
            source_type=source_type,
            url=clean_url,
            description=self._text(description),
            is_trusted=False,
        )
        self.session.add(source)
        await self.session.flush()
        return source

    @classmethod
    def analyze_signal(
        cls,
        evidence: Any,
        source: Any | None = None,
        *,
        portfolio_weights: dict[str, float] | None = None,
    ) -> OwnershipSignal | None:
        if not cls.is_ownership_signal(evidence, source):
            return None

        metadata = dict(getattr(evidence, "metadata_json", None) or {})
        portfolio_weights = {
            str(k).upper(): float(v or 0.0)
            for k, v in (portfolio_weights or {}).items()
        }
        ticker = cls._upper(
            cls._first(metadata, "ticker", "symbol", "issuer_ticker", "security_ticker")
        )
        issuer = cls._text(
            cls._first(
                metadata, "issuer", "issuer_name", "company", "entity", "entity_name"
            )
        )
        actor_name = cls._text(
            cls._first(
                metadata,
                "actor_name",
                "insider_name",
                "politician_name",
                "filer_name",
                "owner_name",
                "fund_name",
                "reporting_owner",
                "reporter",
                "owner",
            )
        )
        actor_type = cls._text(
            cls._first(metadata, "actor_type", "filer_type", "owner_type")
        )
        actor_role = cls._text(
            cls._first(metadata, "actor_role", "role", "title", "office", "position")
        )
        transaction_type = cls._text(
            cls._first(
                metadata,
                "transaction_type",
                "transaction",
                "action",
                "trade_type",
                "direction",
                "acquisition_disposition",
            )
        )
        direction = cls._direction(transaction_type)
        transaction_value_raw = cls._first(
            metadata,
            "transaction_value",
            "reported_value",
            "dollar_value",
            "amount",
            "value",
            "notional",
            "market_value",
        )
        transaction_value = cls._amount(transaction_value_raw)
        shares = cls._amount(cls._first(metadata, "shares", "quantity", "units"))
        price = cls._amount(
            cls._first(metadata, "price", "transaction_price", "avg_price")
        )
        transaction_date = cls._date(
            cls._first(
                metadata, "transaction_date", "trade_date", "executed_at", "event_time"
            )
            or getattr(evidence, "event_time", None)
        )
        disclosure_date = cls._date(
            cls._first(
                metadata, "disclosure_date", "filing_date", "reported_at", "public_time"
            )
            or getattr(evidence, "public_time", None)
            or getattr(evidence, "created_at", None)
        )
        disclosure_lag_days = cls._lag_days(
            cls._amount(cls._first(metadata, "disclosure_lag_days", "lag_days")),
            transaction_date,
            disclosure_date,
        )

        metadata_weight = cls._amount(
            cls._first(
                metadata,
                "portfolio_weight_pct",
                "weight_pct",
                "portfolio_weight",
                "position_weight_pct",
            )
        )
        portfolio_weight = metadata_weight
        if ticker and ticker in portfolio_weights:
            portfolio_weight = max(
                float(portfolio_weights[ticker]), float(metadata_weight or 0.0)
            )
        is_portfolio_linked = bool(
            cls._bool(
                cls._first(
                    metadata,
                    "direct_portfolio_link",
                    "portfolio_linked",
                    "is_portfolio_linked",
                )
            )
            or (ticker is not None and ticker in portfolio_weights)
            or float(portfolio_weight or 0.0) > 0.0
        )

        source_type = str(getattr(source, "source_type", "") or "").strip() or None
        item_type = str(getattr(evidence, "source_item_type", "") or "").strip()
        source_kind = cls._source_kind(
            item_type=item_type, source_type=source_type, actor_type=actor_type
        )
        value_score = cls._value_score(transaction_value)
        timeliness_score = cls._timeliness_score(disclosure_lag_days)
        review_priority = cls._priority(
            source_kind=source_kind,
            direction=direction,
            value_score=value_score,
            timeliness_score=timeliness_score,
            portfolio_weight_pct=portfolio_weight,
            is_portfolio_linked=is_portfolio_linked,
            ticker=ticker,
        )
        should_surface = is_portfolio_linked or (
            ticker is not None and review_priority >= 45.0
        )

        title = cls._text(getattr(evidence, "title", None)) or "Ownership disclosure"
        url = cls._text(
            cls._first(metadata, "source_url", "filing_url", "disclosure_url", "url")
        ) or getattr(evidence, "url", None)
        review_trigger_reason = cls._review_trigger_reason(
            title=title,
            ticker=ticker,
            issuer=issuer,
            source_kind=source_kind,
            actor_name=actor_name,
            direction=direction,
            transaction_value=transaction_value,
            disclosure_lag_days=disclosure_lag_days,
            is_portfolio_linked=is_portfolio_linked,
            portfolio_weight_pct=portfolio_weight,
        )
        next_test = cls._next_test(
            ticker=ticker,
            issuer=issuer,
            source_kind=source_kind,
            direction=direction,
            disclosure_lag_days=disclosure_lag_days,
        )
        shadow_prompt = cls._shadow_prompt(
            ticker=ticker,
            issuer=issuer,
            source_kind=source_kind,
            actor_name=actor_name,
            direction=direction,
            transaction_value=transaction_value,
            disclosure_lag_days=disclosure_lag_days,
        )

        return OwnershipSignal(
            evidence_id=getattr(evidence, "id", None),
            title=title,
            ticker=ticker,
            issuer=issuer,
            source_name=getattr(source, "name", None),
            source_type=source_type,
            source_kind=source_kind,
            actor_name=actor_name,
            actor_type=actor_type,
            actor_role=actor_role,
            direction=direction,
            transaction_type=transaction_type,
            transaction_value=transaction_value,
            transaction_value_label=cls._text(transaction_value_raw),
            shares=shares,
            price=price,
            transaction_date=transaction_date,
            disclosure_date=disclosure_date,
            disclosure_lag_days=disclosure_lag_days,
            portfolio_weight_pct=portfolio_weight,
            is_portfolio_linked=is_portfolio_linked,
            url=url,
            value_score=value_score,
            timeliness_score=timeliness_score,
            review_priority=review_priority,
            should_surface=should_surface,
            review_trigger_reason=review_trigger_reason,
            next_test=next_test,
            shadow_prompt=shadow_prompt,
        )

    @staticmethod
    def _first(metadata: dict[str, Any], *keys: str) -> Any:
        for key in keys:
            value = metadata.get(key)
            if value not in (None, ""):
                return value
        return None

    @staticmethod
    def _text(value: Any) -> str | None:
        if value is None:
            return None
        text = " ".join(str(value).split())
        return text or None

    @classmethod
    def _upper(cls, value: Any) -> str | None:
        text = cls._text(value)
        return text.upper() if text else None

    @staticmethod
    def _bool(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if value is None:
            return False
        return str(value).strip().casefold() in {
            "1",
            "true",
            "yes",
            "y",
            "direct",
            "portfolio",
        }

    @staticmethod
    def _amount(value: Any) -> float | None:
        if value is None or value == "":
            return None
        if isinstance(value, int | float):
            if math.isfinite(float(value)):
                return float(value)
            return None
        text = str(value).replace(",", "")
        matches = _AMOUNT_RE.findall(text)
        if not matches:
            return None
        try:
            return float(matches[0].replace("$", ""))
        except ValueError:
            return None

    @staticmethod
    def _date(value: Any) -> datetime | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=UTC)
        if isinstance(value, date):
            return datetime.combine(value, time.min, tzinfo=UTC)
        text = str(value).strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            try:
                parsed = datetime.fromisoformat(text[:10])
            except ValueError:
                return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)

    @staticmethod
    def _lag_days(
        explicit_lag: float | None,
        transaction_date: datetime | None,
        disclosure_date: datetime | None,
    ) -> float | None:
        if explicit_lag is not None:
            return max(0.0, explicit_lag)
        if transaction_date is None or disclosure_date is None:
            return None
        return max(0.0, (disclosure_date - transaction_date).total_seconds() / 86400.0)

    @staticmethod
    def _direction(transaction_type: str | None) -> str:
        text = (transaction_type or "").casefold()
        if any(
            token in text
            for token in (
                "buy",
                "purchase",
                "acquire",
                "acquisition",
                "add",
                "increase",
            )
        ):
            return "buy"
        if any(
            token in text
            for token in (
                "sell",
                "sale",
                "sold",
                "dispose",
                "disposition",
                "reduce",
                "decrease",
            )
        ):
            return "sell"
        if "exercise" in text:
            return "exercise"
        if "grant" in text or "award" in text:
            return "grant"
        if "hold" in text or "ownership" in text:
            return "ownership update"
        return "unknown"

    @staticmethod
    def _source_kind(
        *, item_type: str, source_type: str | None, actor_type: str | None
    ) -> str:
        if item_type == "congressional_trade_disclosure":
            return "political disclosure"
        if item_type == "insider_disclosure":
            return "corporate insider disclosure"
        if item_type == "institutional_flow":
            return "institutional flow disclosure"
        if item_type == "ownership_disclosure":
            return "ownership disclosure"
        actor = (actor_type or "").casefold()
        if "congress" in actor or "politic" in actor or "government" in actor:
            return "political disclosure"
        if "insider" in actor or "executive" in actor or "director" in actor:
            return "corporate insider disclosure"
        if "institution" in actor or "fund" in actor:
            return "institutional flow disclosure"
        if source_type == "filing":
            return "filing disclosure"
        return "ownership tracker disclosure"

    @staticmethod
    def _value_score(transaction_value: float | None) -> float:
        if transaction_value is None or transaction_value <= 0:
            return 3.0
        if transaction_value >= 10_000_000:
            return 20.0
        if transaction_value >= 1_000_000:
            return 16.0
        if transaction_value >= 100_000:
            return 11.0
        if transaction_value >= 10_000:
            return 7.0
        return 4.0

    @staticmethod
    def _timeliness_score(disclosure_lag_days: float | None) -> float:
        if disclosure_lag_days is None:
            return 4.0
        if disclosure_lag_days <= 2:
            return 10.0
        if disclosure_lag_days <= 7:
            return 8.0
        if disclosure_lag_days <= 30:
            return 6.0
        if disclosure_lag_days <= 90:
            return 3.0
        return 1.0

    @staticmethod
    def _priority(
        *,
        source_kind: str,
        direction: str,
        value_score: float,
        timeliness_score: float,
        portfolio_weight_pct: float | None,
        is_portfolio_linked: bool,
        ticker: str | None,
    ) -> float:
        source_weight = {
            "corporate insider disclosure": 10.0,
            "political disclosure": 7.0,
            "institutional flow disclosure": 6.0,
            "ownership disclosure": 5.0,
            "filing disclosure": 5.0,
        }.get(source_kind, 4.0)
        direction_weight = 4.0 if direction in {"buy", "sell"} else 1.0
        portfolio_weight = min(34.0, float(portfolio_weight_pct or 0.0) * 1.35)
        linked_weight = 16.0 if is_portfolio_linked else 0.0
        ticker_weight = 4.0 if ticker else -4.0
        return min(
            100.0,
            18.0
            + source_weight
            + value_score
            + timeliness_score
            + direction_weight
            + portfolio_weight
            + linked_weight
            + ticker_weight,
        )

    @classmethod
    def _review_trigger_reason(
        cls,
        *,
        title: str,
        ticker: str | None,
        issuer: str | None,
        source_kind: str,
        actor_name: str | None,
        direction: str,
        transaction_value: float | None,
        disclosure_lag_days: float | None,
        is_portfolio_linked: bool,
        portfolio_weight_pct: float | None,
    ) -> str:
        subject = ticker or issuer or "the named security"
        actor = f" by {actor_name}" if actor_name else ""
        amount = f" around ${transaction_value:,.0f}" if transaction_value else ""
        lag = cls._lag_phrase(disclosure_lag_days)
        portfolio = (
            f" Portfolio-linked at roughly {portfolio_weight_pct:.0f}% weight."
            if is_portfolio_linked and portfolio_weight_pct
            else " Portfolio-linked." if is_portfolio_linked else ""
        )
        return (
            f"{source_kind.title()} signal for {subject}: {direction}{amount}{actor}, {lag}. "
            f"Verify the disclosure and test whether it changes the thesis mechanism. {title}.{portfolio}"
        )

    @classmethod
    def _next_test(
        cls,
        *,
        ticker: str | None,
        issuer: str | None,
        source_kind: str,
        direction: str,
        disclosure_lag_days: float | None,
    ) -> str:
        subject = ticker or issuer or "the security"
        lag = cls._lag_phrase(disclosure_lag_days)
        return (
            f"Check the original {source_kind}, normalize the trade date versus disclosure date ({lag}), "
            f"then map the {direction} signal for {subject} to a concrete driver: demand, supply, margins, "
            "financing, regulation, valuation, or timing. Do not treat it as a trade instruction until a later outcome is scored."
        )

    @classmethod
    def _shadow_prompt(
        cls,
        *,
        ticker: str | None,
        issuer: str | None,
        source_kind: str,
        actor_name: str | None,
        direction: str,
        transaction_value: float | None,
        disclosure_lag_days: float | None,
    ) -> str:
        subject = ticker or issuer or "the security"
        actor = actor_name or "the disclosed actor"
        amount = (
            f" with reported value around ${transaction_value:,.0f}"
            if transaction_value
            else ""
        )
        return (
            f"Shadow-test whether a {source_kind} from {actor} showing {direction} exposure to {subject}{amount} "
            f"would have improved decisions after accounting for {cls._lag_phrase(disclosure_lag_days)} and subsequent price/thesis outcomes."
        )

    @staticmethod
    def _lag_phrase(disclosure_lag_days: float | None) -> str:
        if disclosure_lag_days is None:
            return "with unknown disclosure lag"
        if disclosure_lag_days < 1:
            return "disclosed the same day"
        return f"disclosed after about {disclosure_lag_days:.0f} day{'s' if round(disclosure_lag_days) != 1 else ''}"
