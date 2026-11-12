"""Publisher listing metadata feeds a bounded research frontier, not the graph."""

from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import re
from datetime import UTC, datetime, timedelta
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import case, exists, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert

from investos.config import settings
from investos.core.url_security import fetch_public_text
from investos.models.entity import Security
from investos.models.opportunity import (
    ListingDirectory,
    OpportunityUniverseMember,
    ResearchListing,
)
from investos.models.profile import Profile
from investos.services.runtime_settings import RuntimeSettingsStore
from investos.services.security_catalog import SecurityCatalogService


def parse_listing_directory(body: str) -> dict:
    reader = csv.DictReader(io.StringIO(body), delimiter="|")
    fields = set(reader.fieldnames or [])
    symbol_field = "Symbol" if "Symbol" in fields else "ACT Symbol"
    required = {symbol_field, "Security Name", "Test Issue", "ETF"}
    if not required <= fields:
        raise ValueError("The listing directory has an unsupported schema.")
    rows, footer, seen = [], None, set()
    for raw in reader:
        symbol = str(raw.get(symbol_field) or "").strip()
        if symbol.startswith("File Creation Time:"):
            footer = symbol
            continue
        if (
            footer is not None
            or raw.get("Test Issue") not in {"Y", "N"}
            or raw.get("ETF") not in {"Y", "N"}
        ):
            raise ValueError(
                "The listing directory has unverified flags or data after its footer."
            )
        if not symbol or not raw.get("Security Name") or symbol in seen:
            raise ValueError(
                "The listing directory contains incomplete or duplicate rows."
            )
        seen.add(symbol)
        name = raw["Security Name"].strip()
        normalized = name.casefold()
        kind = "unclassified"
        if raw.get("Test Issue") == "Y":
            kind = "test"
        elif raw.get("ETF") == "Y":
            kind = "etf"
        elif re.search(r"\b(warrants?|units?|rights|preferred)\b", normalized):
            pass
        elif "american depositary" in normalized or "american depository" in normalized:
            kind = "adr"
        elif any(
            label in normalized
            for label in ("common stock", "common shares", "ordinary shares")
        ):
            kind = "common_stock"
        rows.append(
            {
                "symbol": symbol,
                "name": name,
                "exchange": (
                    "NASDAQ"
                    if symbol_field == "Symbol"
                    else str(raw.get("Exchange") or "")
                ),
                "instrument_type": kind,
                "research_eligible": kind in {"common_stock", "adr"},
                "metadata_json": {"directory_fields": raw},
            }
        )
    if not footer or not rows:
        raise ValueError(
            "The listing directory has no complete data and creation-time footer."
        )
    return {
        "rows": rows,
        "publisher_clock": footer,
        "sha256": hashlib.sha256(body.encode()).hexdigest(),
    }


class ListingDirectoryService:
    def __init__(self, session):
        self.session = session

    @staticmethod
    def enabled():
        return (
            RuntimeSettingsStore.load().opportunity_discovery.universe_scope
            == "us_listed"
        )

    async def refresh(self):
        if not self.enabled():
            return {"status": "disabled", "reason": "manual_research_scope"}
        await self.session.execute(
            text(
                "SELECT pg_advisory_xact_lock(hashtext('prophet:listing-directories'))"
            )
        )
        for url in settings.US_LISTING_DIRECTORY_URLS[:10]:
            await self.session.execute(
                insert(ListingDirectory)
                .values(
                    id=uuid5(NAMESPACE_URL, "prophet:listings:" + url),
                    url=url,
                    status="not_checked",
                    metadata_json={},
                )
                .on_conflict_do_nothing(index_elements=[ListingDirectory.url])
            )
        await self.session.commit()
        now = datetime.now(UTC)
        due = list(
            await self.session.scalars(
                select(ListingDirectory)
                .where(
                    ListingDirectory.url.in_(settings.US_LISTING_DIRECTORY_URLS[:10]),
                    or_(
                        ListingDirectory.next_refresh_at.is_(None),
                        ListingDirectory.next_refresh_at <= now,
                    ),
                    or_(
                        ListingDirectory.lease_until.is_(None),
                        ListingDirectory.lease_until <= now,
                    ),
                )
                .with_for_update(skip_locked=True)
            )
        )
        lease = now + timedelta(minutes=3)
        for row in due:
            row.lease_until = lease
        await self.session.commit()
        results = await asyncio.gather(
            *(self._fetch(row.url) for row in due), return_exceptions=True
        )
        succeeded = failed = changed = 0
        for claimed, result in zip(due, results):
            row = await self.session.scalar(
                select(ListingDirectory)
                .where(
                    ListingDirectory.id == claimed.id,
                    ListingDirectory.lease_until == lease,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if row is None:
                continue
            observed = datetime.now(UTC)
            row.last_checked_at = observed
            row.lease_until = None
            if isinstance(result, Exception):
                failed += 1
                row.status, row.error = "error", str(result)[:400]
                row.next_refresh_at = observed + timedelta(minutes=15)
            else:
                if (row.metadata_json or {}).get("sha256") != result["sha256"]:
                    await self._store(row, result, observed=observed)
                    changed += 1
                succeeded += 1
                row.status, row.error = "ok", None
                row.last_success_at = observed
                row.next_refresh_at = observed + timedelta(
                    seconds=settings.LISTING_DIRECTORY_REFRESH_SECONDS
                )
            await self.session.commit()
        return {
            "status": "warning" if failed else "ok",
            "directories_checked": len(due),
            "directories_updated": succeeded,
            "directories_changed": changed,
            "directories_failed": failed,
        }

    @staticmethod
    async def _fetch(url):
        body = await fetch_public_text(
            url, timeout_seconds=15, max_bytes=5 * 1024 * 1024
        )
        return await asyncio.to_thread(parse_listing_directory, body)

    async def _store(self, directory, result, *, observed):
        for start in range(0, len(result["rows"]), 500):
            batch = [
                {
                    **row,
                    "metadata_json": {
                        **row["metadata_json"],
                        "snapshot_hash": result["sha256"],
                        "publisher_clock": result["publisher_clock"],
                    },
                    "id": uuid5(directory.id, row["symbol"]),
                    "directory_id": directory.id,
                    "is_current": True,
                    "observed_at": observed,
                }
                for row in result["rows"][start : start + 500]
            ]
            statement = insert(ResearchListing).values(batch)
            await self.session.execute(
                statement.on_conflict_do_update(
                    constraint="uq_research_listing_symbol",
                    set_={
                        key: getattr(statement.excluded, key)
                        for key in (
                            "name",
                            "exchange",
                            "instrument_type",
                            "research_eligible",
                            "is_current",
                            "observed_at",
                            "metadata_json",
                        )
                    },
                )
            )
        await self.session.execute(
            update(ResearchListing)
            .where(
                ResearchListing.directory_id == directory.id,
                ResearchListing.observed_at < observed,
            )
            .values(is_current=False)
        )
        directory.metadata_json = {
            "publisher_clock": result["publisher_clock"],
            "sha256": result["sha256"],
            "row_count": len(result["rows"]),
        }

    async def materialize(self, *, limit):
        if not self.enabled():
            return {"materialized": 0, "linked_existing": 0}
        # The pending count and admission must share a lock. Row locks alone
        # would let concurrent workers each fill a separate frontier batch.
        await self.session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext('prophet:listing-frontier'))")
        )
        pending = int(
            await self.session.scalar(
                select(func.count())
                .select_from(OpportunityUniverseMember)
                .where(
                    OpportunityUniverseMember.source == "us_exchange_directory",
                    OpportunityUniverseMember.enabled.is_(True),
                    OpportunityUniverseMember.last_inspected_at.is_(None),
                )
            )
            or 0
        )
        limit = max(0, limit - pending)
        if not limit:
            return {
                "materialized": 0,
                "linked_existing": 0,
                "pending_frontier": pending,
            }
        now = datetime.now(UTC)
        sources = {
            d.id: d for d in await self.session.scalars(select(ListingDirectory))
        }
        recent_subject = exists(
            select(Profile.id)
            .join(Security, Security.entity_id == Profile.subject_id)
            .where(
                Profile.subject_type == "entity",
                Security.ticker == ResearchListing.symbol,
                Profile.updated_at >= now - timedelta(days=1),
            )
        )
        base = (
            select(ResearchListing)
            .join(ListingDirectory)
            .where(
                ResearchListing.is_current.is_(True),
                ResearchListing.research_eligible.is_(True),
                ResearchListing.operator_excluded.is_(False),
                ResearchListing.member_id.is_(None),
                ListingDirectory.status == "ok",
                ListingDirectory.last_success_at
                >= now
                - timedelta(seconds=settings.LISTING_DIRECTORY_REFRESH_SECONDS * 2),
                or_(
                    ResearchListing.retry_at.is_(None), ResearchListing.retry_at <= now
                ),
            )
        )
        # Recent researched subjects get priority; reserve a slot for unexplored
        # listings so known names cannot permanently close the research frontier.
        priority = (
            list(
                await self.session.scalars(
                    base.order_by(
                        case((recent_subject, 0), else_=1),
                        ResearchListing.observed_at,
                        ResearchListing.symbol,
                        ResearchListing.id,
                    )
                    .limit(max(0, limit - 1))
                    .with_for_update(skip_locked=True, of=ResearchListing)
                )
            )
            if limit > 1
            else []
        )
        oldest = list(
            await self.session.scalars(
                base.where(ResearchListing.id.not_in([r.id for r in priority]))
                .order_by(
                    ResearchListing.observed_at,
                    ResearchListing.symbol,
                    ResearchListing.id,
                )
                .limit(limit - len(priority))
                .with_for_update(skip_locked=True, of=ResearchListing)
            )
        )
        created = linked = 0
        for listing in priority + oldest:
            try:
                async with self.session.begin_nested():
                    security = await SecurityCatalogService(
                        self.session
                    ).resolve_or_create_equity(
                        ticker=listing.symbol,
                        entity_name=listing.name,
                        instrument_type=listing.instrument_type,
                        exchange=listing.exchange,
                    )
                    if security.asset_class != "equity" or not security.is_active:
                        raise ValueError(
                            "The canonical instrument is not an active company equity."
                        )
                    member = await self.session.scalar(
                        select(OpportunityUniverseMember).where(
                            OpportunityUniverseMember.security_id == security.id,
                        )
                    )
                    if member is None:
                        member = OpportunityUniverseMember(
                            security_id=security.id,
                            entity_id=security.entity_id,
                            enabled=True,
                            priority=0.5,
                            source="us_exchange_directory",
                            metadata_json={
                                "origins": [
                                    {
                                        "source_type": "us_exchange_directory",
                                        "source_id": str(listing.id),
                                        "label": "Exchange listing directory",
                                        "observed_at": listing.observed_at.isoformat(),
                                        "metadata": {
                                            "directory_id": str(listing.directory_id),
                                            "source_url": sources[
                                                listing.directory_id
                                            ].url,
                                            "listing_symbol": listing.symbol,
                                            "exchange": listing.exchange,
                                            "listing_basis": listing.metadata_json,
                                        },
                                    }
                                ]
                            },
                        )
                        self.session.add(member)
                        await self.session.flush()
                        created += 1
                    else:
                        # Existing priority and pause decisions belong to the owner.
                        linked += 1
                    listing.member_id, listing.error, listing.retry_at = (
                        member.id,
                        None,
                        None,
                    )
            except Exception as exc:
                listing.error = str(exc)[:400]
                listing.retry_at = now + timedelta(days=1)
        await self.session.commit()
        return {
            "materialized": created,
            "linked_existing": linked,
            "directory_versions": [
                {
                    "url": d.url,
                    "snapshot": d.metadata_json,
                    "last_success_at": (
                        d.last_success_at.isoformat() if d.last_success_at else None
                    ),
                }
                for d in sources.values()
            ],
        }

    async def set_excluded(self, listing_id, excluded):
        listing = await self.session.get(
            ResearchListing, listing_id, with_for_update=True
        )
        if listing is None:
            raise ValueError("Listing not found.")
        listing.operator_excluded = excluded
        if listing.member_id:
            member = await self.session.get(
                OpportunityUniverseMember, listing.member_id, with_for_update=True
            )
            if member is not None:
                member.enabled = not excluded
        await self.session.commit()
        return {"id": listing.id, "operator_excluded": excluded}

    async def status(self, *, offset=0, query=""):
        filters = [ResearchListing.is_current.is_(True)]
        if query.strip():
            pattern = (
                "%"
                + query.strip()
                .replace("\\", "\\\\")
                .replace("%", "\\%")
                .replace("_", "\\_")
                + "%"
            )
            filters.append(
                or_(
                    ResearchListing.symbol.ilike(pattern),
                    ResearchListing.name.ilike(pattern),
                )
            )
        total, eligible, linked = (
            await self.session.execute(
                select(
                    func.count(),
                    func.count().filter(
                        ResearchListing.research_eligible.is_(True),
                        ResearchListing.operator_excluded.is_(False),
                    ),
                    func.count().filter(ResearchListing.member_id.is_not(None)),
                )
                .select_from(ResearchListing)
                .where(ResearchListing.is_current.is_(True))
            )
        ).one()
        match_count = int(
            await self.session.scalar(
                select(func.count()).select_from(ResearchListing).where(*filters)
            )
            or 0
        )
        directories = list(
            await self.session.scalars(
                select(ListingDirectory).order_by(ListingDirectory.url)
            )
        )
        listings = list(
            await self.session.scalars(
                select(ResearchListing)
                .where(*filters)
                .order_by(
                    ResearchListing.symbol,
                    ResearchListing.id,
                )
                .offset(offset)
                .limit(20)
            )
        )
        return {
            "scope": RuntimeSettingsStore.load().opportunity_discovery.universe_scope,
            "total": total,
            "eligible": eligible,
            "materialized": linked,
            "match_count": match_count,
            "offset": offset,
            "directories": [
                {
                    "url": d.url,
                    "status": d.status,
                    "error": d.error,
                    "last_checked_at": d.last_checked_at,
                    "last_success_at": d.last_success_at,
                    "metadata": d.metadata_json,
                }
                for d in directories
            ],
            "items": [
                {
                    "id": r.id,
                    "symbol": r.symbol,
                    "name": r.name,
                    "exchange": r.exchange,
                    "instrument_type": r.instrument_type,
                    "research_eligible": r.research_eligible,
                    "operator_excluded": r.operator_excluded,
                    "member_id": r.member_id,
                    "observed_at": r.observed_at,
                    "error": r.error,
                }
                for r in listings
            ],
        }
