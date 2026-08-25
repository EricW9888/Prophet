from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from investos.models.entity import Entity, Security


class SecurityCatalogService:
    """Own canonical lookup and creation of ticker-backed securities."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def resolve_or_create_equity(
        self,
        *,
        ticker: str,
        entity_name: str | None = None,
    ) -> Security:
        normalized_ticker = ticker.strip().upper()
        if not normalized_ticker:
            raise ValueError("Ticker is required.")

        security = (
            await self.session.execute(
                select(Security)
                .where(Security.ticker == normalized_ticker)
                .order_by(Security.is_active.desc(), Security.id)
                .options(selectinload(Security.entity))
                .limit(1)
            )
        ).scalar_one_or_none()
        if security is not None:
            if (
                entity_name
                and security.entity is not None
                and security.entity.name.strip().upper() == normalized_ticker
            ):
                security.entity.name = entity_name.strip()
                await self.session.flush()
            return security

        entity = Entity(
            name=(entity_name or normalized_ticker).strip() or normalized_ticker,
            entity_type="company",
        )
        self.session.add(entity)
        await self.session.flush()

        security = Security(
            entity_id=entity.id,
            ticker=normalized_ticker,
            asset_class="equity",
            instrument_type="common_stock",
        )
        self.session.add(security)
        await self.session.flush()
        await self.session.refresh(security, ["entity"])
        return security
