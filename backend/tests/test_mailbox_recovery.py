"""Synthetic database tests: receipt persistence is not ledger completion."""

from decimal import Decimal
from email.message import EmailMessage
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from investos.config import settings
from investos.core.storage import LocalStorage
from investos.models.evidence import RawEvidence
from investos.models.portfolio import Transaction
from investos.models.review import ReviewQueueItem
from investos.schemas.portfolio import TransactionCreate
from investos.services.mailbox import GmailMailboxService


@pytest.fixture
async def mailbox_service(tmp_path):
    engine = create_async_engine(settings.SQLALCHEMY_DATABASE_URI, poolclass=NullPool)
    async with engine.connect() as connection:
        transaction = await connection.begin()
        async with AsyncSession(
            bind=connection,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        ) as session:
            service = GmailMailboxService(session)
            service.ingestion.storage = LocalStorage(tmp_path)
            yield service
        await transaction.rollback()
    await engine.dispose()


def receipt(sender="notifications@robinhood.com"):
    message = EmailMessage()
    message["From"] = sender
    message["Subject"] = "Your individual account deposit is complete"
    message["Date"] = "Mon, 03 Aug 2026 15:00:00 +0000"
    message.set_content("Your individual account deposit is complete. Amount: $123.45")
    return message


def order_receipt(
    *, ticker: str, quantity: str, price: str, sender="notifications@robinhood.com"
):
    message = EmailMessage()
    message["From"] = sender
    message["Subject"] = f"Your {ticker} order was executed"
    message["Date"] = "Mon, 03 Aug 2026 15:00:00 +0000"
    message.set_content(
        f"Your order to buy {quantity} shares of {ticker} has been executed "
        f"at an average price of ${price}"
    )
    return message


async def test_interrupted_import_recovers_existing_evidence_once(
    mailbox_service, monkeypatch
):
    service = mailbox_service
    uid = str(uuid4())
    runtime = SimpleNamespace(folder="Synthetic")
    original = service.portfolio.add_sourced_transaction_by_ticker
    monkeypatch.setattr(
        service.portfolio,
        "add_sourced_transaction_by_ticker",
        AsyncMock(side_effect=RuntimeError("synthetic posting failure")),
    )
    with pytest.raises(RuntimeError, match="posting failure"):
        await service._process_message(uid, receipt(), runtime)
    evidence = await service._existing_receipt(uid, runtime)
    assert evidence is not None
    original_id = evidence.id
    assert not await service._already_ingested(uid, runtime)

    monkeypatch.setattr(
        service.portfolio, "add_sourced_transaction_by_ticker", original
    )
    result = await service._process_message(uid, receipt(), runtime)
    assert result["transaction_created"]
    assert result["evidence_id"] == str(original_id)
    assert await service._already_ingested(uid, runtime)
    count = await service.session.scalar(
        select(func.count())
        .select_from(RawEvidence)
        .where(RawEvidence.external_id == service._external_id_for_uid(uid, runtime))
    )
    assert count == 1
    posted = (
        (
            await service.session.execute(
                select(Transaction).where(
                    Transaction.provenance_json["raw_evidence_id"].astext
                    == str(original_id)
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(posted) == 1
    assert posted[0].price == Decimal("123.45")
    posted[0].status = "corrected"
    await service.session.commit()
    assert await service._already_ingested(uid, runtime)


async def test_model_confidence_cannot_post_unverified_cash(
    mailbox_service, monkeypatch
):
    service = mailbox_service
    monkeypatch.setattr(
        service,
        "_classify_message",
        AsyncMock(
            return_value={
                "document_type": "cash_activity",
                "action": "deposit",
                "ticker": "CASH",
                "quantity": 1,
                "price": 999999,
                "confidence": 1.0,
            }
        ),
    )
    post = AsyncMock()
    monkeypatch.setattr(service.portfolio, "add_sourced_transaction_by_ticker", post)
    uid = str(uuid4())
    result = await service._process_message(uid, receipt("not-a-broker@example.test"))
    assert result["needs_reconciliation"]
    assert not result["transaction_created"]
    post.assert_not_awaited()
    assert await service._already_ingested(uid)
    review = await service.session.scalar(
        select(ReviewQueueItem)
        .where(ReviewQueueItem.item_type == "mailbox_receipt")
        .limit(1)
    )
    assert review is not None


async def test_mailbox_adopts_matching_legacy_fill_despite_float_tail(
    mailbox_service,
):
    service = mailbox_service
    ticker = f"T{uuid4().hex[:7].upper()}"
    executed_at = service._parse_datetime_fallback(
        None, service._parse_email_datetime("Mon, 03 Aug 2026 15:00:00 +0000")
    )
    notes = f"Deterministic parse: BUY 0.75 {ticker} @ $628.87"
    legacy = await service.portfolio.add_transaction_by_ticker(
        ticker=ticker,
        txn_data=TransactionCreate(
            action="buy",
            quantity=0.75,
            price=628.87,
            executed_at=executed_at,
            notes=notes,
            lot_type="csv_import",
        ),
    )
    legacy.price = Decimal("628.8700000000000045")
    await service.session.commit()

    uid = str(uuid4())
    result = await service._process_message(
        uid,
        order_receipt(ticker=ticker, quantity="0.75", price="628.87"),
    )

    assert not result["transaction_created"]
    assert result["transaction_reconciled"]
    assert result["transaction_id"] == str(legacy.id)
    transactions = list(
        (
            await service.session.execute(
                select(Transaction).where(Transaction.position_id == legacy.position_id)
            )
        )
        .scalars()
        .all()
    )
    assert len(transactions) == 1
    assert transactions[0].source_identity == f"email_order_confirmation:{uid}"
    assert transactions[0].provenance_json["legacy_transaction_adopted"] is True


async def test_incomplete_ingestion_commit_is_found_by_metadata(mailbox_service):
    service = mailbox_service
    uid = str(uuid4())
    source = await service._get_or_create_email_source()
    evidence = RawEvidence(
        source_id=source.id,
        source_item_type="email_order_confirmation",
        metadata_json={"external_id": uid, "operational_mailbox": True},
    )
    service.session.add(evidence)
    await service.session.commit()
    assert (await service._existing_receipt(uid)).id == evidence.id
    assert not await service._already_ingested(uid)


async def test_deferred_receipts_rotate_behind_unattempted_work(mailbox_service):
    service = mailbox_service
    runtime = SimpleNamespace(folder=f"Synthetic-{uuid4()}")
    await service._preserve_deferred_receipt("1", receipt(), runtime)
    await service.session.commit()
    first_id = (await service._existing_receipt("1", runtime)).id
    assert not await service._already_ingested("1", runtime)
    assert await service._prioritize_pending_uids([b"1", b"2", b"3"], runtime) == [
        b"2",
        b"3",
        b"1",
    ]

    # Reopening the service uses durable state, not an in-process cursor.
    restarted = GmailMailboxService(service.session)
    restarted.ingestion.storage = service.ingestion.storage
    await restarted._preserve_deferred_receipt("2", receipt(), runtime)
    await service.session.commit()
    assert await restarted._prioritize_pending_uids([b"1", b"2", b"3"], runtime) == [
        b"3",
        b"1",
        b"2",
    ]
    await restarted._preserve_deferred_receipt("1", receipt(), runtime)
    await service.session.commit()
    assert (await restarted._existing_receipt("1", runtime)).id == first_id
    assert await restarted._prioritize_pending_uids([b"1", b"2"], runtime) == [
        b"2",
        b"1",
    ]


@pytest.mark.parametrize("outcome", ["supported", "irrelevant", "review"])
async def test_deferred_reclassification_reuses_receipt(
    mailbox_service, monkeypatch, outcome
):
    service = mailbox_service
    uid = str(uuid4())
    runtime = SimpleNamespace(folder="Synthetic")
    message = receipt() if outcome == "supported" else receipt("sender@example.test")
    await service._preserve_deferred_receipt(uid, message, runtime)
    await service.session.commit()
    evidence = await service._existing_receipt(uid, runtime)
    original_id, original_ref = evidence.id, evidence.raw_content_ref
    assert not await service._already_ingested(uid, runtime)
    monkeypatch.setattr(
        service,
        "_classify_message",
        AsyncMock(
            return_value={
                "document_type": (
                    "other" if outcome == "irrelevant" else "cash_activity"
                ),
                "action": "deposit",
                "ticker": "CASH",
                "quantity": 1,
                "price": 123.45,
                "confidence": 1.0,
            }
        ),
    )
    result = await service._process_message(uid, message, runtime)
    await service.session.commit()
    assert result["transaction_created"] is (outcome == "supported")
    assert await service._already_ingested(uid, runtime)
    evidence = await service._existing_receipt(uid, runtime)
    assert evidence.id == original_id
    assert evidence.raw_content_ref == original_ref
    assert (
        evidence.metadata_json["mailbox_status"]
        == {
            "supported": "classified",
            "irrelevant": "irrelevant",
            "review": "needs_review",
        }[outcome]
    )
    assert (
        await service.session.scalar(
            select(func.count())
            .select_from(RawEvidence)
            .where(
                RawEvidence.external_id == service._external_id_for_uid(uid, runtime)
            )
        )
        == 1
    )
