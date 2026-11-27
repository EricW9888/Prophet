"""Industry and sector for companies, from the SEC's own classification.

Every SEC filer's submissions record carries an industry (its SIC description)
and the SEC office that reviews it, which serves as a sector. Prophet's
companies had neither recorded, so nothing could link companies by industry.

A company is matched to its SEC filer by a linked ticker, or else by an exact
match of its normalized name to the filer's registered name (a name two filers
share is ambiguous and skipped). Only the industry and sector are recorded:
no ticker or security link is created, because ticker identity is the owner's
decision. Empty fields are filled; a value already present is never
overwritten.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from investos.models.entity import Entity, Security
from investos.services.business_map import (
    SUBMISSIONS_URL,
    TICKERS_URL,
    BusinessMapService,
    Fetch,
)

STATE_FILE = "sec_industry_checks.json"
RECHECK_AFTER = timedelta(days=90)
_SUFFIXES = re.compile(
    r"\b(inc|incorporated|corp|corporation|co|company|ltd|limited|plc|holding|holdings"
    r"|group|sa|nv|ag|se|the|class [a-z]|common stock)\b"
)


def readable_industry(text: str | None) -> str | None:
    """SEC descriptions are sometimes all capitals ("SERVICES-PREPACKAGED SOFTWARE")."""
    value = " ".join((text or "").split())
    if not value:
        return None
    return value.capitalize() if value.isupper() else value


def sector_from_office(office: str | None) -> str | None:
    """'06 Technology' -> 'Technology'."""
    value = re.sub(r"^\d+\s*", "", (office or "").strip())
    return value or None


def company_name_key(name: str | None) -> str:
    """'Apple Inc.' and 'APPLE INC' compare equal; legal suffixes are ignored."""
    value = re.sub(r"[^a-z0-9 ]", " ", (name or "").lower())
    return " ".join(_SUFFIXES.sub(" ", value).split())


class SecIndustryService:
    def __init__(self, session: AsyncSession, fetch: Fetch | None = None):
        self.session = session
        self.maps = BusinessMapService(session, fetch=fetch)

    async def _filers_by_name(self) -> dict[str, int]:
        listing = json.loads(await self.maps._get(TICKERS_URL))
        by_name: dict[str, int] = {}
        ambiguous: set[str] = set()
        for row in listing.values():
            key = company_name_key(row.get("title"))
            cik = int(row["cik_str"])
            if not key:
                continue
            if key in by_name and by_name[key] != cik:
                ambiguous.add(key)
            by_name.setdefault(key, cik)
        return {key: cik for key, cik in by_name.items() if key not in ambiguous}

    async def refresh(self, *, limit: int = 500) -> dict[str, int]:
        now = datetime.now(UTC)
        checks = self.maps._read_state(STATE_FILE)
        rows = (
            await self.session.execute(
                select(Entity, Security.ticker)
                .outerjoin(Security, Security.entity_id == Entity.id)
                .where(Entity.industry.is_(None), Entity.entity_type == "company")
                .order_by(Security.ticker.is_(None), Entity.name)
            )
        ).all()
        counts = {"checked": 0, "filled": 0, "no_sec_match": 0}
        tickers: dict[str, str] = {}
        entities: dict[str, Entity] = {}
        for entity, ticker in rows:
            key = str(entity.id)
            last = checks.get(key)
            if last and now - datetime.fromisoformat(last) < RECHECK_AFTER:
                continue
            entities.setdefault(key, entity)
            if ticker and key not in tickers:
                tickers[key] = str(ticker).upper()
        if not entities:
            return counts
        ciks = await self.maps.ticker_ciks()
        by_name = await self._filers_by_name()
        for key, entity in list(entities.items())[:limit]:
            counts["checked"] += 1
            checks[key] = now.isoformat()
            cik = ciks.get(tickers[key]) if key in tickers else None
            cik = cik or by_name.get(company_name_key(entity.name))
            if cik is None:
                counts["no_sec_match"] += 1
                continue
            try:
                data = json.loads(await self.maps._get(SUBMISSIONS_URL.format(cik=cik)))
            except Exception:
                checks.pop(key, None)
                continue
            industry = readable_industry(data.get("sicDescription"))
            if industry and not entity.industry:
                entity.industry = industry
                counts["filled"] += 1
            sector = sector_from_office(data.get("ownerOrg"))
            if sector and not entity.sector:
                entity.sector = sector
        await self.session.commit()
        self.maps._write_state(STATE_FILE, checks)
        return counts
