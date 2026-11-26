"""Re-attribute evidence that was filed under a title-derived source name (#302).

Before #302, auto-discovered sources were named after the text before the first
" - " in a title and matched by that name, so filings were filed under form
types ("424B2") and third-party pages under the company they discussed. This
moves each such evidence row, with its processed item and claim records, to the
source for its URL's publisher, and withdraws learned trust from the emptied
title-named source. Operator trust decisions and Prophet's own system sources
are never touched; learned profiles stay with the old row as history.

`plan()` only reads. `apply()` writes, and is run only after the owner approves
the plan (trust records are an owner gate).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from investos.models.catalog import SourceClaimRecord
from investos.models.evidence import RawEvidence, SourceItem
from investos.models.knowledge import Claim
from investos.models.source import Source

SYSTEM_SOURCE_NAMES = frozenset(
    {
        "Prophet Agent Conversation",
        "Manual Research Inbox",
        "Gmail Operational Inbox",
    }
)
REPAIR_REASON = "Evidence re-attributed to its publisher domain (#302)."


def publisher_host(url: str | None) -> str | None:
    """Same identity rule as SourceLearningService._publisher."""
    if not url:
        return None
    try:
        host = (urlparse(url).netloc or "").lower()
    except ValueError:
        return None
    host = host.split("@")[-1].split(":")[0]
    host = host[4:] if host.startswith("www.") else host
    return host[:120] or None


def _normalized_name(name: str | None) -> str:
    value = (name or "").strip().lower()
    return value[4:] if value.startswith("www.") else value


@dataclass
class SourceMove:
    source_id: UUID
    source_name: str
    was_trusted: bool
    moves: dict[str, list[UUID]] = field(default_factory=dict)  # host -> evidence ids
    kept: int = 0  # evidence already at this source's own host

    @property
    def moved(self) -> int:
        return sum(len(ids) for ids in self.moves.values())


class SourceAttributionRepair:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def _candidates(self) -> list[SourceMove]:
        rows = (
            await self.session.execute(
                select(
                    Source.id,
                    Source.name,
                    Source.is_trusted,
                    RawEvidence.id,
                    RawEvidence.url,
                )
                .join(RawEvidence, RawEvidence.source_id == Source.id)
                .where(Source.trust_origin != "operator")
                .where(Source.name.not_in(SYSTEM_SOURCE_NAMES))
                .where(RawEvidence.url.is_not(None))
                .order_by(Source.name, RawEvidence.created_at)
            )
        ).all()
        plans: dict[UUID, SourceMove] = {}
        for source_id, name, trusted, evidence_id, url in rows:
            host = publisher_host(url)
            plan = plans.setdefault(
                source_id, SourceMove(source_id, name, bool(trusted))
            )
            if not host or host == _normalized_name(name):
                plan.kept += 1
                continue
            plan.moves.setdefault(host, []).append(evidence_id)
        return [plan for plan in plans.values() if plan.moved]

    async def _existing_hosts(self, hosts: list[str]) -> set[str]:
        names = list(hosts) + [f"www.{h}" for h in hosts]
        found = set(
            (
                await self.session.execute(
                    select(Source.name).where(Source.name.in_(names))
                )
            ).scalars()
        )
        return {h for h in hosts if h in found or f"www.{h}" in found}

    @staticmethod
    def _ordered(candidates: list[SourceMove]) -> list[SourceMove]:
        # Larger and trusted sources claim a publisher name first, deterministically.
        return sorted(
            candidates,
            key=lambda c: (
                -int(c.was_trusted),
                -c.moved,
                c.source_name,
                str(c.source_id),
            ),
        )

    async def _actions(
        self, candidates: list[SourceMove]
    ) -> list[tuple[SourceMove, str]]:
        """Decide per source: "rename" keeps a single-publisher source and its learned
        record under the publisher's name; "move" files evidence under existing or new
        publisher sources and empties the title-named row."""
        hosts = sorted({h for c in candidates for h in c.moves})
        claimed = await self._existing_hosts(hosts)
        actions = []
        for item in self._ordered(candidates):
            only = next(iter(item.moves)) if len(item.moves) == 1 else None
            if only and item.kept == 0 and only not in claimed:
                claimed.add(only)
                actions.append((item, "rename"))
            else:
                claimed.update(item.moves)
                actions.append((item, "move"))
        return actions

    async def plan(self, *, sample: int = 40) -> dict[str, Any]:
        actions = await self._actions(await self._candidates())
        renames = [c for c, a in actions if a == "rename"]
        moves = [c for c, a in actions if a == "move"]
        return {
            "sources_affected": len(actions),
            "renamed_to_publisher_keeping_trust": len(renames),
            "renamed_trusted": sum(1 for c in renames if c.was_trusted),
            "emptied_by_moves": sum(1 for c in moves if c.kept == 0),
            "trust_withdrawn": sum(1 for c in moves if c.kept == 0 and c.was_trusted),
            "evidence_rows_moved": sum(c.moved for c in moves),
            "sample": [
                {
                    "source": c.source_name,
                    "trusted": c.was_trusted,
                    "action": a,
                    "rows": c.moved,
                    "kept": c.kept,
                    "to": sorted(c.moves, key=lambda h: -len(c.moves[h]))[:4],
                }
                for c, a in actions[:sample]
            ],
        }

    async def _publisher(self, host: str, source_type: str) -> Source:
        found = (
            (
                await self.session.execute(
                    select(Source)
                    .where(Source.name.in_((host, f"www.{host}")))
                    .order_by(Source.created_at.asc(), Source.id.asc())
                )
            )
            .scalars()
            .first()
        )
        if found is not None:
            return found
        created = Source(
            name=host,
            source_type=source_type,
            url=f"https://{host}",
            description="Publisher created when evidence was re-attributed from a title-derived source (#302).",
            is_trusted=False,
        )
        self.session.add(created)
        await self.session.flush()
        return created

    async def _move(
        self, old: Source, target: Source, evidence_ids: list[UUID]
    ) -> None:
        await self.session.execute(
            update(RawEvidence)
            .where(RawEvidence.id.in_(evidence_ids))
            .values(source_id=target.id)
        )
        await self.session.execute(
            update(SourceItem)
            .where(SourceItem.raw_evidence_id.in_(evidence_ids))
            .values(source_id=target.id)
        )
        claim_ids = (
            select(Claim.id)
            .join(SourceItem, SourceItem.id == Claim.source_item_id)
            .where(SourceItem.raw_evidence_id.in_(evidence_ids))
        )
        await self.session.execute(
            update(SourceClaimRecord)
            .where(SourceClaimRecord.source_id == old.id)
            .where(SourceClaimRecord.claim_id.in_(claim_ids))
            .values(source_id=target.id)
        )

    async def apply(self) -> dict[str, Any]:
        actions = await self._actions(await self._candidates())
        renamed = moved = trust_withdrawn = 0
        for item, action in actions:
            old = await self.session.get(Source, item.source_id)
            if action == "rename":
                host = next(iter(item.moves))
                old.description = f"Renamed from title-derived name {old.name!r} to its publisher (#302). {old.description or ''}".strip()
                old.name = host
                old.url = old.url or f"https://{host}"
                await self.session.flush()
                renamed += 1
                continue
            for host, evidence_ids in item.moves.items():
                target = await self._publisher(
                    host, "official" if host.endswith("sec.gov") else old.source_type
                )
                await self._move(old, target, evidence_ids)
                moved += len(evidence_ids)
            if item.kept == 0:
                if old.is_trusted:
                    old.apply_learned_trust(False, reason=REPAIR_REASON)
                    trust_withdrawn += 1
                old.description = f"{REPAIR_REASON} {old.description or ''}".strip()
        await self.session.flush()
        return {
            "sources_renamed": renamed,
            "evidence_moved": moved,
            "trust_withdrawn": trust_withdrawn,
        }
