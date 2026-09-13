from __future__ import annotations

import asyncio
import importlib.util
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import ModuleType
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from investos.config import settings
from investos.models.entity import Entity, Security
from investos.models.portfolio import CashLedgerEntry, Lot, Position, Transaction
from investos.schemas.portfolio import TransactionCreate
from investos.services.portfolio import (
    MailboxTransactionReconciliationRequired,
    PortfolioService,
)
from investos.services.security_catalog import SecurityCatalogService
from investos.services.transaction_provenance import transaction_source_identity


def _load_migration() -> ModuleType:
    path = (
        Path(__file__).parents[1]
        / "alembic"
        / "versions"
        / "6c1a4e8b2d90_add_transaction_source_identity.py"
    )
    spec = importlib.util.spec_from_file_location(
        "transaction_identity_migration", path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MIGRATION = _load_migration()
EXECUTED_AT = datetime(2026, 8, 3, 15, tzinfo=UTC)


@pytest.fixture
async def portfolio_service():
    test_engine = create_async_engine(
        settings.SQLALCHEMY_DATABASE_URI, poolclass=NullPool
    )
    async with test_engine.connect() as connection:
        transaction = await connection.begin()
        async with AsyncSession(
            bind=connection,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        ) as session:
            yield PortfolioService(session)
        await transaction.rollback()
    await test_engine.dispose()


def _transaction(
    *,
    action: str = "buy",
    quantity: float = 0.75,
    price: float = 628.87,
    notes: str = "Synthetic transaction",
    evidence_id=None,
) -> TransactionCreate:
    provenance = None
    if evidence_id is not None:
        provenance = {
            "source_type": "email_order_confirmation",
            "external_id": f"Synthetic:{evidence_id}",
            "raw_evidence_id": str(evidence_id),
        }
    return TransactionCreate(
        action=action,
        quantity=quantity,
        price=price,
        executed_at=EXECUTED_AT,
        notes=notes,
        lot_type="broker_confirmation",
        provenance_json=provenance,
    )


async def test_migrated_schema_has_transaction_source_identity_constraint() -> None:
    def inspect_schema(connection):
        inspector = sa.inspect(connection)
        columns = {column["name"] for column in inspector.get_columns("transactions")}
        constraints = {
            constraint["name"]: tuple(constraint["column_names"])
            for constraint in inspector.get_unique_constraints("transactions")
        }
        return columns, constraints

    test_engine = create_async_engine(
        settings.SQLALCHEMY_DATABASE_URI, poolclass=NullPool
    )
    try:
        async with test_engine.connect() as connection:
            columns, constraints = await connection.run_sync(inspect_schema)
    finally:
        await test_engine.dispose()

    assert "source_identity" in columns
    assert constraints["uq_transactions_source_identity"] == ("source_identity",)


async def test_source_identity_matches_migration_and_prefers_provider_identity() -> (
    None
):
    evidence_id = uuid4()
    cases = [
        None,
        {},
        {"raw_evidence_id": str(evidence_id)},
        {"source_type": "GMAIL", "external_id": "Folder:123"},
        {"source": "gmail", "external_id": "x" * 600},
        {"raw_evidence_id": "not-a-uuid", "source": "gmail", "external_id": "7"},
    ]
    for provenance in cases:
        assert transaction_source_identity(provenance) == MIGRATION._source_identity(
            provenance
        )
    assert transaction_source_identity(cases[2]) == f"evidence:{evidence_id}"
    assert transaction_source_identity(cases[5]) == "gmail:7"


async def test_provider_identity_survives_evidence_rematerialization(
    portfolio_service,
) -> None:
    service = portfolio_service
    ticker = f"T{uuid4().hex[:7].upper()}"
    first = _transaction(evidence_id=uuid4())
    second = _transaction(evidence_id=uuid4())
    external_id = f"Synthetic:{uuid4()}"
    first.provenance_json["external_id"] = external_id
    second.provenance_json["external_id"] = external_id

    created = await service.add_sourced_transaction_by_ticker(ticker, first)
    repeated = await service.add_sourced_transaction_by_ticker(ticker, second)

    assert created.disposition == "created"
    assert repeated.disposition == "existing"
    assert repeated.transaction.id == created.transaction.id
    assert created.transaction.source_identity == (
        f"email_order_confirmation:{external_id}"
    )


async def test_replay_upgrades_an_older_internal_source_identity(
    portfolio_service,
) -> None:
    service = portfolio_service
    ticker = f"T{uuid4().hex[:7].upper()}"
    original_evidence_id = uuid4()
    transaction = _transaction(evidence_id=original_evidence_id)
    external_id = f"Synthetic:{uuid4()}"
    transaction.provenance_json["external_id"] = external_id
    created = await service.add_transaction_by_ticker(ticker, transaction)
    created.source_identity = f"evidence:{original_evidence_id}"
    await service.session.commit()

    replay = _transaction(evidence_id=uuid4())
    replay.provenance_json["external_id"] = external_id
    repeated = await service.add_sourced_transaction_by_ticker(ticker, replay)

    assert repeated.disposition == "existing"
    assert repeated.transaction.id == created.id
    assert repeated.transaction.source_identity == (
        f"email_order_confirmation:{external_id}"
    )


async def test_sourced_transaction_is_idempotent_but_distinct_receipts_are_distinct(
    portfolio_service,
) -> None:
    service = portfolio_service
    ticker = f"T{uuid4().hex[:7].upper()}"
    first_source, second_source = uuid4(), uuid4()

    created = await service.add_sourced_transaction_by_ticker(
        ticker, _transaction(evidence_id=first_source)
    )
    repeated = await service.add_sourced_transaction_by_ticker(
        ticker, _transaction(evidence_id=first_source)
    )
    distinct = await service.add_sourced_transaction_by_ticker(
        ticker, _transaction(evidence_id=second_source)
    )

    assert created.disposition == "created"
    assert repeated.disposition == "existing"
    assert repeated.transaction.id == created.transaction.id
    assert distinct.disposition == "created"
    assert distinct.transaction.id != created.transaction.id
    assert (
        await service.session.scalar(
            select(func.count())
            .select_from(Transaction)
            .where(Transaction.position_id == created.transaction.position_id)
        )
        == 2
    )


async def test_ambiguous_legacy_matches_require_review(portfolio_service) -> None:
    service = portfolio_service
    ticker = f"T{uuid4().hex[:7].upper()}"
    first = await service.add_transaction_by_ticker(ticker, _transaction())
    await service.add_transaction_by_ticker(ticker, _transaction())

    with pytest.raises(
        MailboxTransactionReconciliationRequired,
        match="Multiple legacy transactions",
    ):
        await service.add_sourced_transaction_by_ticker(
            ticker, _transaction(evidence_id=uuid4())
        )

    assert (
        await service.session.scalar(
            select(func.count())
            .select_from(Transaction)
            .where(Transaction.position_id == first.position_id)
        )
        == 2
    )


async def test_reconciliation_is_conservative_idempotent_and_rebuilds_book(
    portfolio_service,
) -> None:
    service = portfolio_service
    ticker = f"T{uuid4().hex[:7].upper()}"
    legacy = await service.add_transaction_by_ticker(ticker, _transaction())
    legacy.price = Decimal("628.8700000000000045")
    await service.session.commit()
    canonical = await service.add_transaction_by_ticker(
        ticker, _transaction(evidence_id=uuid4())
    )

    preview = await service.reconcile_mailbox_transaction_duplicates(dry_run=True)
    preview_pair = next(
        pair
        for pair in preview.candidates
        if pair.legacy_transaction_id == legacy.id
        and pair.canonical_transaction_id == canonical.id
    )
    assert preview_pair.ticker == ticker
    assert preview.candidate_count >= 1
    assert preview.ambiguous_group_count == 0
    assert preview.applied_count == 0
    assert preview.expected_buying_power_adjustment >= 471.6525
    assert legacy.status == "settled"

    applied = await service.reconcile_mailbox_transaction_duplicates(dry_run=False)
    await service.session.refresh(legacy)
    position = await service.session.get(Position, canonical.position_id)
    canonical_cash = await service.session.scalar(
        select(CashLedgerEntry).where(CashLedgerEntry.transaction_id == canonical.id)
    )

    assert applied.applied_count == preview.candidate_count
    assert legacy.status == "corrected"
    assert legacy.superseded_by_id == canonical.id
    assert position is not None
    assert Decimal(position.quantity) == Decimal("0.75")
    assert canonical_cash is not None
    assert Decimal(canonical_cash.amount) == Decimal("-471.6525")
    assert (
        await service.reconcile_mailbox_transaction_duplicates(dry_run=True)
    ).candidate_count == 0


async def test_migration_backfills_only_unambiguous_source_identities() -> None:
    schema = f"transaction_identity_{uuid4().hex}"
    unique_id, duplicate_a, duplicate_b = uuid4(), uuid4(), uuid4()
    unique_evidence, duplicate_evidence = uuid4(), uuid4()

    test_engine = create_async_engine(
        settings.SQLALCHEMY_DATABASE_URI, poolclass=NullPool
    )
    try:
        async with test_engine.connect() as connection:
            transaction = await connection.begin()
            try:
                await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
                await connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
                await connection.execute(text("""
                        CREATE TABLE transactions (
                            id uuid PRIMARY KEY,
                            provenance_json jsonb,
                            source_identity varchar(512)
                        )
                        """))
                await connection.execute(
                    text("""
                        INSERT INTO transactions (id, provenance_json) VALUES
                        (:unique_id, jsonb_build_object(
                            'raw_evidence_id', CAST(:unique_evidence AS text)
                        )),
                        (:duplicate_a, jsonb_build_object(
                            'raw_evidence_id', CAST(:duplicate_evidence AS text)
                        )),
                        (:duplicate_b, jsonb_build_object(
                            'raw_evidence_id', CAST(:duplicate_evidence AS text)
                        ))
                        """),
                    {
                        "unique_id": unique_id,
                        "unique_evidence": str(unique_evidence),
                        "duplicate_a": duplicate_a,
                        "duplicate_b": duplicate_b,
                        "duplicate_evidence": str(duplicate_evidence),
                    },
                )
                await connection.run_sync(MIGRATION._backfill_unique_source_identities)
                rows = dict(
                    (
                        await connection.execute(
                            text("SELECT id, source_identity FROM transactions")
                        )
                    ).all()
                )
                assert rows[unique_id] == f"evidence:{unique_evidence}"
                assert rows[duplicate_a] is None
                assert rows[duplicate_b] is None
            finally:
                await transaction.rollback()
    finally:
        await test_engine.dispose()


async def test_concurrent_replay_of_one_source_converges_on_one_transaction() -> None:
    ticker = f"T{uuid4().hex[:7].upper()}"
    evidence_id = uuid4()
    test_engine = create_async_engine(
        settings.SQLALCHEMY_DATABASE_URI, poolclass=NullPool
    )
    session_maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with session_maker() as setup_session:
        security = await SecurityCatalogService(setup_session).resolve_or_create_equity(
            ticker=ticker
        )
        position = Position(
            security_id=security.id, direction="long", list_type="holding"
        )
        setup_session.add(position)
        await setup_session.commit()
        security_id, entity_id, position_id = (
            security.id,
            security.entity_id,
            position.id,
        )

    async def post_once():
        async with session_maker() as session:
            return await PortfolioService(session).add_sourced_transaction_by_ticker(
                ticker, _transaction(evidence_id=evidence_id)
            )

    try:
        first, second = await asyncio.gather(post_once(), post_once())
        assert first.transaction.id == second.transaction.id
        async with session_maker() as session:
            count = await session.scalar(
                select(func.count())
                .select_from(Transaction)
                .where(
                    Transaction.source_identity
                    == f"email_order_confirmation:Synthetic:{evidence_id}"
                )
            )
            assert count == 1
    finally:
        async with session_maker() as session:
            await session.execute(
                sa.delete(CashLedgerEntry).where(
                    CashLedgerEntry.transaction_id.in_(
                        select(Transaction.id).where(
                            Transaction.position_id == position_id
                        )
                    )
                )
            )
            await session.execute(sa.delete(Lot).where(Lot.position_id == position_id))
            await session.execute(
                sa.delete(Transaction).where(Transaction.position_id == position_id)
            )
            await session.execute(sa.delete(Position).where(Position.id == position_id))
            await session.execute(sa.delete(Security).where(Security.id == security_id))
            await session.execute(sa.delete(Entity).where(Entity.id == entity_id))
            await session.commit()
        await test_engine.dispose()
