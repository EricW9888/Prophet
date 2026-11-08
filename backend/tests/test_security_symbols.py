import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select

from investos.db import async_session_maker
from investos.models.entity import Entity, Security
from investos.models.knowledge_mutation import KnowledgeMutation
from investos.models.portfolio import CashLedgerEntry, Lot, Position, Transaction
from investos.schemas.portfolio import SecuritySymbolChangeRequest
from investos.services.security_catalog import SecurityCatalogService


@pytest.mark.asyncio(loop_scope="session")
async def test_owner_symbol_change_retains_instrument_ledger_and_historical_lookup():
    old = "O" + uuid4().hex[:7].upper()
    new = "N" + uuid4().hex[:7].upper()
    async with async_session_maker() as session:
        catalog = SecurityCatalogService(session)
        security = await catalog.resolve_or_create_equity(
            ticker=old, entity_name="Synthetic Issuer"
        )
        position = Position(
            security_id=security.id,
            direction="long",
            list_type="holding",
            quantity=Decimal("2.5"),
            avg_cost_basis=Decimal("20"),
            current_price=Decimal("30"),
            market_value=Decimal("75"),
        )
        session.add(position)
        await session.commit()
        security_id, entity_id, position_id = (
            security.id,
            security.entity_id,
            position.id,
        )
        before_counts = [
            await session.scalar(select(func.count()).select_from(model))
            for model in (Transaction, Lot, CashLedgerEntry)
        ]
        request = SecuritySymbolChangeRequest(
            expected_ticker=old,
            new_ticker=new,
            effective_date=datetime.now(UTC).date() - timedelta(days=2),
            reference_url="https://exchange.example.test/symbol-notice",
            reason="Owner verified unchanged instrument identifier against the exchange notice.",
            confirmed_same_instrument=True,
        )
        try:
            result = await catalog.change_symbol(security_id, request)
            retried = await catalog.change_symbol(security_id, request)
            assert result["audit_id"] == retried["audit_id"]
            assert (
                await catalog.resolve_or_create_equity(ticker=old)
            ).id == security_id
            assert (
                await catalog.resolve_or_create_equity(ticker=new)
            ).id == security_id
            assert position.quantity == Decimal("2.5") and position.avg_cost_basis == 20
            assert position.market_value == 75
            after_counts = [
                await session.scalar(select(func.count()).select_from(model))
                for model in (Transaction, Lot, CashLedgerEntry)
            ]
            assert after_counts == before_counts
            event = await session.get(KnowledgeMutation, result["audit_id"])
            assert event.metadata_json["before"]["ticker"] == old
            assert event.metadata_json["after"]["ticker"] == new
            assert event.created_at is not None and event.actor == "owner"
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(Security)
                    .where(Security.id == security_id)
                )
                == 1
            )
            with pytest.raises(ValueError, match="changed since"):
                await catalog.change_symbol(
                    security_id, request.model_copy(update={"expected_ticker": "STALE"})
                )
            await session.rollback()
        finally:
            await session.execute(
                delete(KnowledgeMutation).where(
                    KnowledgeMutation.node_id == security_id
                )
            )
            await session.execute(delete(Position).where(Position.id == position_id))
            await session.execute(delete(Security).where(Security.id == security_id))
            await session.execute(delete(Entity).where(Entity.id == entity_id))
            await session.commit()


@pytest.mark.asyncio(loop_scope="session")
async def test_competing_symbol_corrections_reload_locked_identity():
    old = "O" + uuid4().hex[:7].upper()
    targets = ["N" + uuid4().hex[:7].upper() for _ in range(2)]
    async with async_session_maker() as session:
        security = await SecurityCatalogService(session).resolve_or_create_equity(
            ticker=old
        )
        await session.commit()
        security_id, entity_id = security.id, security.entity_id
    ready = asyncio.Event()
    waiting = 0

    async def change(target):
        nonlocal waiting
        async with async_session_maker() as session:
            cached = await session.get(Security, security_id)
            assert cached.ticker == old
            waiting += 1
            if waiting == 2:
                ready.set()
            await ready.wait()
            return await SecurityCatalogService(session).change_symbol(
                security_id,
                SecuritySymbolChangeRequest(
                    expected_ticker=old,
                    new_ticker=target,
                    effective_date=datetime.now(UTC).date(),
                    reference_url="https://exchange.example.test/notice",
                    reason="Synthetic concurrent owner correction",
                    confirmed_same_instrument=True,
                ),
            )

    try:
        results = await asyncio.gather(
            *(change(target) for target in targets), return_exceptions=True
        )
        assert sum(isinstance(value, dict) for value in results) == 1
        assert sum(isinstance(value, ValueError) for value in results) == 1
        async with async_session_maker() as session:
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(KnowledgeMutation)
                    .where(
                        KnowledgeMutation.node_id == security_id,
                        KnowledgeMutation.change_type == "symbol_change",
                    )
                )
                == 1
            )
    finally:
        async with async_session_maker() as session:
            await session.execute(
                delete(KnowledgeMutation).where(
                    KnowledgeMutation.node_id == security_id
                )
            )
            await session.execute(delete(Security).where(Security.id == security_id))
            await session.execute(delete(Entity).where(Entity.id == entity_id))
            await session.commit()


@pytest.mark.asyncio(loop_scope="session")
async def test_symbol_conflicts_and_reuse_require_identity_review():
    first_ticker = "A" + uuid4().hex[:7].upper()
    second_ticker = "B" + uuid4().hex[:7].upper()
    async with async_session_maker() as session:
        catalog = SecurityCatalogService(session)
        first = await catalog.resolve_or_create_equity(ticker=first_ticker)
        second = await catalog.resolve_or_create_equity(ticker=second_ticker)
        await session.commit()
        ids = [first.id, second.id]
        entity_ids = [first.entity_id, second.entity_id]
        payload = SecuritySymbolChangeRequest(
            expected_ticker=first_ticker,
            new_ticker=second_ticker,
            effective_date=datetime.now(UTC).date(),
            reference_url="https://exchange.example.test/notice",
            reason="Synthetic conflicting identifier",
            confirmed_same_instrument=True,
        )
        try:
            with pytest.raises(ValueError, match="another current or historical"):
                await catalog.change_symbol(first.id, payload)
            await session.rollback()
            with pytest.raises(ValueError, match="future"):
                await catalog.change_symbol(
                    ids[0],
                    payload.model_copy(
                        update={
                            "effective_date": datetime.now(UTC).date()
                            + timedelta(days=2)
                        }
                    ),
                )
            await session.rollback()
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(KnowledgeMutation)
                    .where(KnowledgeMutation.node_id.in_(ids))
                )
                == 0
            )
            with pytest.raises(ValueError, match="reserved for cash-ledger"):
                await catalog.change_symbol(
                    ids[0], payload.model_copy(update={"new_ticker": "CASH"})
                )
            replacement = "C" + uuid4().hex[:7].upper()
            await catalog.change_symbol(
                ids[0], payload.model_copy(update={"new_ticker": replacement})
            )
            reused = Security(
                entity_id=entity_ids[1],
                ticker=first_ticker,
                asset_class="equity",
                instrument_type="common_stock",
            )
            session.add(reused)
            await session.flush()
            ids.append(reused.id)
            with pytest.raises(ValueError, match="multiple current or historical"):
                await catalog.resolve_or_create_equity(ticker=first_ticker)
            await session.rollback()
        finally:
            await session.execute(
                delete(KnowledgeMutation).where(KnowledgeMutation.node_id.in_(ids))
            )
            await session.execute(delete(Security).where(Security.id.in_(ids)))
            await session.execute(delete(Entity).where(Entity.id.in_(entity_ids)))
            await session.commit()
