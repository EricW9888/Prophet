import re
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from investos.models.entity import Entity, Security
from investos.models.knowledge_mutation import KnowledgeMutation
from investos.schemas.portfolio import SecuritySymbolChangeRequest
from investos.services.knowledge_audit import KnowledgeAuditService
from investos.services.subject_alias import normalize_alias

ISSUER_SYMBOL_LABEL = re.compile(
    r"^(?P<name>.+?)\s+\((?P<symbol>[A-Z0-9][A-Z0-9.\^=\-]{0,31})\)$"
)


class SecurityCatalogService:
    """Own canonical lookup and creation of ticker-backed securities."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def _symbol_candidates(self, ticker: str) -> list[Security]:
        direct = list(
            (
                await self.session.scalars(
                    select(Security)
                    .where(Security.ticker == ticker)
                    .options(selectinload(Security.entity))
                )
            ).all()
        )
        history_ids = (
            await self.session.scalars(
                select(KnowledgeMutation.node_id).where(
                    KnowledgeMutation.node_type == "security",
                    KnowledgeMutation.change_type == "symbol_change",
                    KnowledgeMutation.metadata_json["before"]["ticker"].astext
                    == ticker,
                )
            )
        ).all()
        known = {row.id: row for row in direct}
        if history_ids:
            historical = (
                await self.session.scalars(
                    select(Security)
                    .where(Security.id.in_(history_ids))
                    .options(selectinload(Security.entity))
                )
            ).all()
            known.update({row.id: row for row in historical})
        return list(known.values())

    async def resolve_existing(self, ticker: str) -> Security | None:
        candidates = await self._symbol_candidates(ticker.strip().upper())
        if len(candidates) > 1:
            raise ValueError(
                "Symbol has multiple current or historical identities; instrument review is required"
            )
        return candidates[0] if candidates else None

    @staticmethod
    def is_issuer_symbol_label(label: str) -> bool:
        return ISSUER_SYMBOL_LABEL.fullmatch(label.strip()) is not None

    async def resolve_issuer_symbol_label(self, label: str) -> Security | None:
        """Require the supplied issuer name and symbol to agree with the catalog."""
        match = ISSUER_SYMBOL_LABEL.fullmatch(label.strip())
        if match is None:
            return None
        try:
            security = await self.resolve_existing(match["symbol"])
        except ValueError:
            return None
        if security is None or not security.is_active:
            return None
        issuer_name = normalize_alias(match["name"])
        known_names = {
            normalize_alias(name)
            for name in (security.entity.name, *(security.entity.aliases or []))
        }
        return security if issuer_name and issuer_name in known_names else None

    async def change_symbol(
        self, security_id: UUID, payload: SecuritySymbolChangeRequest
    ) -> dict:
        """Owner-confirmed name change of the same instrument, never a trade."""
        before = payload.expected_ticker.strip().upper()
        after = payload.new_ticker.strip().upper()
        if not all(
            re.fullmatch(r"[A-Z0-9][A-Z0-9.\^=\-]{0,31}", ticker)
            for ticker in (before, after)
        ):
            raise ValueError("Invalid symbol")
        if before == after:
            raise ValueError("Choose a different symbol")
        if after == "CASH":
            raise ValueError("CASH is reserved for cash-ledger activity")
        if payload.effective_date > datetime.now(UTC).date():
            raise ValueError("A future symbol change cannot update the current catalog")
        for ticker in sorted({before, after}):
            await self.session.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:symbol))"),
                {"symbol": f"security:{ticker}"},
            )
        security = await self.session.scalar(
            select(Security)
            .where(Security.id == security_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if security is None:
            raise ValueError("Security not found")
        metadata = {
            "version": 1,
            "before": {"ticker": before},
            "after": {"ticker": after},
            "effective_date": payload.effective_date.isoformat(),
            "reference_url": str(payload.reference_url),
            "confirmed_same_instrument": True,
        }
        if security.ticker == after:
            previous = await self.session.scalar(
                select(KnowledgeMutation)
                .where(
                    KnowledgeMutation.node_type == "security",
                    KnowledgeMutation.node_id == security_id,
                    KnowledgeMutation.change_type == "symbol_change",
                    KnowledgeMutation.metadata_json == metadata,
                )
                .limit(1)
            )
            if previous:
                await self.session.commit()
                return {
                    "security_id": security_id,
                    "ticker": after,
                    "audit_id": previous.id,
                    **metadata,
                }
        if security.ticker != before:
            raise ValueError(
                "Symbol changed since this correction was prepared; reload before retrying"
            )
        candidates = await self._symbol_candidates(after)
        if any(candidate.id != security_id for candidate in candidates):
            raise ValueError(
                "Target symbol belongs to another current or historical instrument; identity review is required"
            )
        security.ticker = after
        event = await KnowledgeAuditService(self.session).record_change(
            node_type="security",
            node_id=security.id,
            change_type="symbol_change",
            reason=payload.reason.strip(),
            actor="owner",
            source_type="owner_confirmed_reference",
            subject_type="entity",
            subject_id=security.entity_id,
            metadata=metadata,
        )
        await self.session.commit()
        return {
            "security_id": security_id,
            "ticker": after,
            "audit_id": event.id,
            **metadata,
        }

    async def resolve_or_create_equity(
        self,
        *,
        ticker: str,
        entity_name: str | None = None,
    ) -> Security:
        normalized_ticker = ticker.strip().upper()
        if not normalized_ticker:
            raise ValueError("Ticker is required.")

        await self.session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:symbol))"),
            {"symbol": f"security:{normalized_ticker}"},
        )
        security = await self.resolve_existing(normalized_ticker)
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
