"""The companies the owner follows: held, watched, or being considered.

One definition, used wherever relevance to the owner matters (the insider
filings feed and the research Feed's ranking).
"""

from __future__ import annotations

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from investos.models.entity import Entity, Security
from investos.models.portfolio import Position

FOLLOWED_LIST_TYPES = ("holding", "watchlist", "considering")


async def followed_companies(session: AsyncSession) -> list[tuple[Entity, str]]:
    """Companies held or watched, with their ticker, largest positions first."""
    rows = (
        await session.execute(
            select(Entity, Security.ticker)
            .join(Security, Security.entity_id == Entity.id)
            .join(Position, Position.security_id == Security.id)
            .where(Position.list_type.in_(FOLLOWED_LIST_TYPES))
            .order_by(desc(Position.market_value))
        )
    ).all()
    seen, ordered = set(), []
    for entity, ticker in rows:
        if ticker and entity.id not in seen:
            seen.add(entity.id)
            ordered.append((entity, ticker))
    return ordered
