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


async def test_interrupted_import_recovers_existing_evidence_once(
    mailbox_service, monkeypatch
):
    service = mailbox_service
    uid = str(uuid4())
    runtime = SimpleNamespace(folder="Synthetic")
    original = service.portfolio.add_transaction_by_ticker
    monkeypatch.setattr(
        service.portfolio,
        "add_transaction_by_ticker",
        AsyncMock(side_effect=RuntimeError("synthetic posting failure")),
    )
    with pytest.raises(RuntimeError, match="posting failure"):
        await service._process_message(uid, receipt(), runtime)
    evidence = await service._existing_receipt(uid, runtime)
    assert evidence is not None
    original_id = evidence.id
    assert not await service._already_ingested(uid, runtime)

    monkeypatch.setattr(service.portfolio, "add_transaction_by_ticker", original)
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
    monkeypatch.setattr(service.portfolio, "add_transaction_by_ticker", post)
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
