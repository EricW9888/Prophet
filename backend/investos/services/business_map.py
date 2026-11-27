"""Each company's revenue drivers, read from its latest annual report.

Prophet researches by open question. This map says what a company is: which
segments, products, regions and customers its revenue comes from, and how
much each matters, so research can look down the important routes first.

The figures are the filer's own inline XBRL facts in its latest 10-K on SEC
EDGAR: deterministic, free, and attributable to one filing. Nothing here is
inferred by a model. Companies without a 10-K or without tagged breakdowns
get no map, and the check says so.
"""

from __future__ import annotations

import asyncio
import html
import json
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable
from uuid import UUID

from sqlalchemy import delete, desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from investos.config import settings
from investos.core.url_security import fetch_public_document
from investos.models.business_map import BusinessDriver
from investos.models.entity import Entity, Security
from investos.models.portfolio import Position
from investos.models.profile import Profile

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}"
TICKER_MAP_MAX_AGE = timedelta(days=7)
RECHECK_AFTER = timedelta(days=30)
# SEC fair access: well under ten requests a second.
REQUEST_SPACING_SECONDS = 0.25

REVENUE_CONCEPTS = (
    "us-gaap:Revenues",
    "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
    "us-gaap:RevenueFromContractWithCustomerIncludingAssessedTax",
    "us-gaap:SalesRevenueNet",
)
AXES = {
    "us-gaap:StatementBusinessSegmentsAxis": "segment",
    "srt:ProductOrServiceAxis": "product",
    "srt:StatementGeographicalAxis": "region",
    "srt:MajorCustomersAxis": "customer",
}

_CONTEXT = re.compile(
    r"<xbrli:context\b[^>]*\bid=\"([^\"]+)\"[^>]*>(.*?)</xbrli:context>", re.S
)
_START = re.compile(r"<xbrli:startDate>\s*([0-9-]+)\s*</xbrli:startDate>")
_END = re.compile(r"<xbrli:endDate>\s*([0-9-]+)\s*</xbrli:endDate>")
_MEMBER = re.compile(
    r"<xbrldi:explicitMember\b[^>]*\bdimension=\"([^\"]+)\"[^>]*>\s*([^<\s]+)\s*</xbrldi:explicitMember>"
)
_FACT = re.compile(r"<ix:nonFraction\b([^>]*)>(.*?)</ix:nonFraction>", re.S)
_ATTR = re.compile(r"\b([A-Za-z:]+)=\"([^\"]*)\"")
_TAGS = re.compile(r"<[^>]+>")


@dataclass(frozen=True)
class Driver:
    kind: str
    member: str
    label: str
    revenue: float
    share: float


@dataclass(frozen=True)
class BusinessMap:
    period_start: date
    period_end: date
    total_revenue: float
    drivers: list[Driver]


# Standard members that split revenue by kind rather than by product line.
GENERIC_MEMBERS = {
    "us-gaap:ProductMember": "All products",
    "us-gaap:ServiceMember": "All services",
}


def _readable(label: str) -> str:
    """Taxonomy labels are sometimes all capitals ("UNITED STATES")."""
    if label.isupper() and len(label) > 4:
        return " ".join(
            word if len(word) <= 3 else word.capitalize() for word in label.split()
        )
    return label


def member_label(member: str, labels: dict[str, str] | None = None) -> str:
    """The filer's label for a member, else a readable form of its name."""
    generic = GENERIC_MEMBERS.get(member)
    if generic and (
        not labels
        or labels.get(member, "").lower().rstrip("s") in {"", "product", "service"}
    ):
        return generic
    if labels and member in labels:
        return _readable(labels[member])
    name = member.split(":")[-1]
    name = name[: -len("Member")] if name.endswith("Member") else name
    words = re.sub(
        r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", " ", name
    ).split()
    if not words:
        return member
    rest = [
        word if word.isupper() and len(word) > 1 else word.lower() for word in words[1:]
    ]
    return " ".join([words[0], *rest])


def parse_labels(linkbase: str) -> dict[str, str]:
    """Member QName -> the filer's standard label, from its label linkbase."""
    attrs = lambda raw: dict(_ATTR.findall(raw))  # noqa: E731
    locs = {}
    for raw in re.findall(r"<link:loc\b([^>]*)/?>", linkbase):
        found = attrs(raw)
        href = found.get("xlink:href", "")
        if "#" in href and found.get("xlink:label"):
            element = href.split("#", 1)[1]
            locs[found["xlink:label"]] = element.replace("_", ":", 1)
    texts: dict[str, list[tuple[str, str]]] = {}
    for raw, text in re.findall(
        r"<link:label\b([^>]*)>(.*?)</link:label>", linkbase, re.S
    ):
        found = attrs(raw)
        texts.setdefault(found.get("xlink:label", ""), []).append(
            (found.get("xlink:role", ""), html.unescape(text).strip())
        )
    labels: dict[str, str] = {}
    for raw in re.findall(r"<link:labelArc\b([^>]*)/?>", linkbase):
        found = attrs(raw)
        member = locs.get(found.get("xlink:from", ""))
        candidates = texts.get(found.get("xlink:to", ""), [])
        if not member or not candidates:
            continue
        preferred = next(
            (text for role, text in candidates if role.endswith("/role/label")),
            candidates[0][1],
        )
        labels[member] = re.sub(r"\s*\[Member\]$", "", preferred).strip()
    return labels


def _number(text: str, attrs: dict[str, str]) -> float | None:
    cleaned = html.unescape(_TAGS.sub("", text)).replace(",", "").strip()
    if cleaned in {"-", "—", ""}:
        return None
    try:
        value = float(cleaned)
    except ValueError:
        return None
    value *= 10 ** int(attrs.get("scale") or 0)
    return -value if attrs.get("sign") == "-" else value


def _rollups(members: dict[tuple[str, str], float]) -> set[tuple[str, str]]:
    """Members equal to the sum of two or more smaller members on the same axis.

    Filers often tag a subtotal (for example all product sales) next to its
    parts; counting both would double the axis. Sums are compared within
    0.05% of the candidate, since a filer's subtotal adds up exactly.
    """
    found: set[tuple[str, str]] = set()
    for axis in {axis for axis, _ in members}:
        values = sorted(
            ((value, key) for key, value in members.items() if key[0] == axis),
            reverse=True,
        )
        for index, (value, key) in enumerate(values):
            smaller = [item for item, _ in values[index + 1 :]]
            if len(smaller) < 2:
                continue
            unit = max(value * 0.0005, 1.0)
            target = round(value / unit)
            reachable = {0: 0}
            for part in smaller:
                step = round(part / unit)
                for total_units, count in list(reachable.items()):
                    candidate = total_units + step
                    if (
                        candidate <= target + 1
                        and reachable.get(candidate, -1) < count + 1
                    ):
                        reachable[candidate] = count + 1
            if any(reachable.get(target + delta, 0) >= 2 for delta in (-1, 0, 1)):
                found.add(key)
    return found


def parse_business_map(
    document: str, labels: dict[str, str] | None = None
) -> BusinessMap | None:
    """The latest full fiscal year's revenue, broken down by each tagged axis."""
    contexts: dict[str, tuple[str, str, tuple[tuple[str, str], ...]]] = {}
    for context_id, body in _CONTEXT.findall(document):
        start, end = _START.search(body), _END.search(body)
        if start and end:
            contexts[context_id] = (
                start.group(1),
                end.group(1),
                tuple(_MEMBER.findall(body)),
            )

    totals: dict[tuple[str, str], float] = {}
    by_member: dict[tuple[str, str], dict[tuple[str, str], float]] = {}
    for raw_attrs, text in _FACT.findall(document):
        attrs = dict(_ATTR.findall(raw_attrs))
        if attrs.get("name") not in REVENUE_CONCEPTS:
            continue
        context = contexts.get(attrs.get("contextRef", ""))
        value = _number(text, attrs)
        if context is None or value is None:
            continue
        start, end, members = context
        period = (start, end)
        if not members:
            totals[period] = max(value, totals.get(period, 0.0))
        elif len(members) == 1 and members[0][0] in AXES:
            bucket = by_member.setdefault(period, {})
            bucket[members[0]] = max(value, bucket.get(members[0], 0.0))

    def days(period: tuple[str, str]) -> int:
        return (date.fromisoformat(period[1]) - date.fromisoformat(period[0])).days

    annual = [p for p in totals if 350 <= days(p) <= 380 and totals[p] > 0]
    if not annual:
        return None
    period = max(annual, key=lambda item: item[1])
    total = totals[period]
    members = {k: v for k, v in (by_member.get(period) or {}).items() if v > 0}
    rollups = _rollups(members)
    drivers = [
        Driver(
            kind=AXES[axis],
            member=member,
            label=member_label(member, labels),
            revenue=value,
            share=value / total,
        )
        for (axis, member), value in members.items()
        if (axis, member) not in rollups
    ]
    drivers.sort(key=lambda driver: (driver.kind, -driver.revenue))
    return BusinessMap(
        period_start=date.fromisoformat(period[0]),
        period_end=date.fromisoformat(period[1]),
        total_revenue=total,
        drivers=drivers,
    )


Fetch = Callable[[str], Awaitable[str]]


async def _fetch_text(url: str) -> str:
    # The SEC serves some plain-text files, such as its daily indexes, as a
    # generic binary type; they are accepted only when they really are text.
    document = await fetch_public_document(
        url, max_bytes=80_000_000, timeout_seconds=90, allow_binary_text=True
    )
    return document.text()


class BusinessMapService:
    def __init__(self, session: AsyncSession, fetch: Fetch | None = None):
        self.session = session
        self.fetch = fetch or _fetch_text

    @staticmethod
    def _state_path(name: str) -> Path:
        return Path(settings.STORAGE_DIR) / "_system" / name

    def _read_state(self, name: str) -> dict[str, Any]:
        try:
            return json.loads(self._state_path(name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _write_state(self, name: str, payload: dict[str, Any]) -> None:
        path = self._state_path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")

    async def _get(self, url: str) -> str:
        text = await self.fetch(url)
        await asyncio.sleep(REQUEST_SPACING_SECONDS)
        return text

    async def ticker_ciks(self) -> dict[str, int]:
        cached = self._read_state("sec_tickers.json")
        fetched_at = cached.get("fetched_at")
        if (
            fetched_at
            and datetime.now(UTC) - datetime.fromisoformat(fetched_at)
            < TICKER_MAP_MAX_AGE
        ):
            return {k: int(v) for k, v in cached.get("ciks", {}).items()}
        rows = json.loads(await self._get(TICKERS_URL))
        ciks = {
            str(row["ticker"]).upper(): int(row["cik_str"]) for row in rows.values()
        }
        self._write_state(
            "sec_tickers.json",
            {"fetched_at": datetime.now(UTC).isoformat(), "ciks": ciks},
        )
        return ciks

    async def latest_annual_report(self, cik: int) -> tuple[str, str] | None:
        """URL of the latest 10-K's primary document and its accession number."""
        submissions = json.loads(await self._get(SUBMISSIONS_URL.format(cik=cik)))
        recent = submissions.get("filings", {}).get("recent", {})
        for index, form in enumerate(recent.get("form", [])):
            if form != "10-K" or not recent["isInlineXBRL"][index]:
                continue
            accession = recent["accessionNumber"][index]
            folder = ARCHIVE_URL.format(cik=cik, accession=accession.replace("-", ""))
            return f"{folder}/{recent['primaryDocument'][index]}", accession
        return None

    async def _labels(self, document_url: str) -> dict[str, str]:
        folder = document_url.rsplit("/", 1)[0]
        try:
            index = json.loads(await self._get(f"{folder}/index.json"))
            names = [item["name"] for item in index["directory"]["item"]]
            linkbase = next(name for name in names if name.endswith("_lab.xml"))
            return parse_labels(await self._get(f"{folder}/{linkbase}"))
        except (StopIteration, KeyError, ValueError):
            return {}

    async def refresh_entity(
        self, entity: Entity, ticker: str, ciks: dict[str, int]
    ) -> str:
        """Map one company. Returns the outcome for the check record."""
        cik = ciks.get(ticker.upper())
        if cik is None:
            return "no_sec_filer"
        report = await self.latest_annual_report(cik)
        if report is None:
            return "no_annual_report"
        url, accession = report
        mapped = parse_business_map(await self._get(url), await self._labels(url))
        if mapped is None or not mapped.drivers:
            return "no_tagged_breakdown"
        await self.session.execute(
            delete(BusinessDriver).where(
                BusinessDriver.entity_id == entity.id,
                BusinessDriver.period_end == mapped.period_end,
            )
        )
        for driver in mapped.drivers:
            self.session.add(
                BusinessDriver(
                    entity_id=entity.id,
                    kind=driver.kind,
                    member=driver.member,
                    label=driver.label,
                    revenue=driver.revenue,
                    share=driver.share,
                    total_revenue=mapped.total_revenue,
                    period_start=mapped.period_start,
                    period_end=mapped.period_end,
                    filing_url=url,
                    accession=accession,
                )
            )
        await self.session.commit()
        return "mapped"

    async def candidates(self) -> list[tuple[Entity, str]]:
        """Holdings first, then other researched companies, each once."""
        held = (
            await self.session.execute(
                select(Entity, Security.ticker)
                .join(Security, Security.entity_id == Entity.id)
                .join(Position, Position.security_id == Security.id)
                .where(Position.list_type == "holding", Position.quantity > 0)
                .order_by(desc(Position.market_value))
            )
        ).all()
        researched = (
            await self.session.execute(
                select(Entity, Security.ticker)
                .join(Security, Security.entity_id == Entity.id)
                .join(
                    Profile,
                    (Profile.subject_type == "entity")
                    & (Profile.subject_id == Entity.id),
                )
                .where(Security.is_active.is_(True))
                .order_by(desc(Profile.updated_at))
            )
        ).all()
        seen: set[UUID] = set()
        ordered = []
        for entity, ticker in [*held, *researched]:
            if entity.id in seen or not ticker:
                continue
            seen.add(entity.id)
            ordered.append((entity, ticker))
        return ordered

    async def refresh(self, *, limit: int = 12) -> dict[str, int]:
        """Map up to ``limit`` companies not checked within the last 30 days."""
        checks = self._read_state("business_map_checks.json")
        now = datetime.now(UTC)
        due = [
            (entity, ticker)
            for entity, ticker in await self.candidates()
            if not checks.get(str(entity.id))
            or now - datetime.fromisoformat(checks[str(entity.id)]["checked_at"])
            >= RECHECK_AFTER
        ][:limit]
        outcomes: dict[str, int] = {}
        if not due:
            return outcomes
        ciks = await self.ticker_ciks()
        for entity, ticker in due:
            try:
                outcome = await self.refresh_entity(entity, ticker, ciks)
            except Exception:
                await self.session.rollback()
                outcome = "fetch_failed"
            outcomes[outcome] = outcomes.get(outcome, 0) + 1
            if outcome != "fetch_failed":
                checks[str(entity.id)] = {
                    "checked_at": now.isoformat(),
                    "outcome": outcome,
                }
        self._write_state("business_map_checks.json", checks)
        return outcomes

    async def latest_map(self, entity_id: UUID) -> dict[str, Any] | None:
        period_end = await self.session.scalar(
            select(BusinessDriver.period_end)
            .where(BusinessDriver.entity_id == entity_id)
            .order_by(desc(BusinessDriver.period_end))
            .limit(1)
        )
        if period_end is None:
            return None
        rows = (
            await self.session.scalars(
                select(BusinessDriver)
                .where(
                    BusinessDriver.entity_id == entity_id,
                    BusinessDriver.period_end == period_end,
                )
                .order_by(BusinessDriver.kind, desc(BusinessDriver.revenue))
            )
        ).all()
        first = rows[0]
        return {
            "entity_id": str(entity_id),
            "period_start": first.period_start.isoformat(),
            "period_end": first.period_end.isoformat(),
            "total_revenue": first.total_revenue,
            "filing_url": first.filing_url,
            "drivers": [
                {
                    "kind": row.kind,
                    "label": row.label,
                    "revenue": row.revenue,
                    "share": row.share,
                }
                for row in rows
            ],
        }
