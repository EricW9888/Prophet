"""Exercise receipt provenance through ingestion and the actual legacy upgrade."""

import importlib.util
import json
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, text

from alembic.migration import MigrationContext
from alembic.operations import Operations
from investos.api.routes.review import router as review_router
from investos.api.routes.source import router as source_router
from investos.db import get_session
from investos.models.evidence import RawEvidence
from investos.models.portfolio import Transaction
from investos.models.review import ReviewQueueItem
from investos.services.portfolio import MailboxTransactionReconciliationRequired
from investos.services.review import ReviewService
from tests.test_mailbox_recovery import DEFAULT_SCOPE, mailbox_service, receipt


@pytest.mark.parametrize(
    "document_type,action",
    [
        ("corporate_action", "spinoff"),
        ("account_transfer", "transfer"),
        ("order_confirmation", "buy"),
    ],
)
async def test_ingestion_review_retains_original_without_posting(
    mailbox_service, monkeypatch, document_type, action
):
    service = mailbox_service
    classification = {
        "document_type": document_type,
        "action": action,
        "ticker": "SYN",
        "confidence": 1,
    }
    monkeypatch.setattr(
        service, "_classify_message", AsyncMock(return_value=classification)
    )
    before = await service.session.scalar(select(func.count()).select_from(Transaction))
    uid = str(uuid4().int % (2**32 - 1) + 1)
    message = receipt("synthetic@example.test")
    result = await service._process_message(uid, message, DEFAULT_SCOPE)
    assert result["needs_reconciliation"] and not result["transaction_created"]
    evidence = await service.session.get(RawEvidence, UUID(result["evidence_id"]))
    review = await service.session.scalar(
        select(ReviewQueueItem).where(ReviewQueueItem.evidence_id == evidence.id)
    )
    assert review.item_id == uuid5(
        NAMESPACE_URL, f"mailbox-reconciliation:{evidence.external_id}"
    )
    old_identity = (review.id, review.item_id, review.priority_score, review.status)
    response = await ReviewService(service.session)._serialize(review)
    assert response.evidence_id == evidence.id and response.item_label == evidence.title
    assert response.review_domain == "portfolio_data"
    app = FastAPI()
    app.include_router(review_router, prefix="/api")
    app.include_router(source_router, prefix="/api")

    async def session_dependency():
        yield service.session

    app.dependency_overrides[get_session] = session_dependency
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        queued = await client.get(f"/api/review/queue/{review.id}")
        assert queued.status_code == 200
        linked = queued.json()["evidence_id"]
        original = await client.get(f"/api/sources/evidence/{linked}")
        assert original.status_code == 200
        assert original.json()["id"] == str(evidence.id) != str(review.item_id)
        assert original.json()["title"] == evidence.title
    # Retrying the producer enriches the same concern; it is not a new decision.
    await service._queue_reconciliation_review(
        evidence_id=evidence.id,
        external_id=evidence.external_id,
        subject=evidence.title,
        classification=classification,
    )
    assert (
        review.id,
        review.item_id,
        review.priority_score,
        review.status,
    ) == old_identity
    assert evidence.metadata_json["mailbox_status"] == "needs_review"
    assert (
        await service.ingestion.storage.get_object(evidence.raw_content_ref)
        == message.as_bytes()
    )
    assert (
        await service.session.scalar(select(func.count()).select_from(Transaction))
        == before
    )


async def test_ambiguous_legacy_posting_path_also_retains_receipt_provenance(
    mailbox_service, monkeypatch
):
    service = mailbox_service
    monkeypatch.setattr(
        service.portfolio,
        "add_sourced_transaction_by_ticker",
        AsyncMock(
            side_effect=MailboxTransactionReconciliationRequired("synthetic conflict")
        ),
    )
    result = await service._process_message(
        str(uuid4().int % (2**32 - 1) + 1), receipt(), DEFAULT_SCOPE
    )
    assert result["needs_reconciliation"] and not result["transaction_created"]
    evidence_id = UUID(result["evidence_id"])
    review = await service.session.scalar(
        select(ReviewQueueItem).where(ReviewQueueItem.evidence_id == evidence_id)
    )
    assert (
        await ReviewService(service.session)._serialize(review)
    ).evidence_id == evidence_id


async def test_existing_review_provenance_cannot_be_silently_replaced(
    mailbox_service, monkeypatch
):
    service = mailbox_service
    classification = {
        "document_type": "account_transfer",
        "action": "transfer",
        "confidence": 1,
    }
    monkeypatch.setattr(
        service, "_classify_message", AsyncMock(return_value=classification)
    )
    result = await service._process_message(
        str(uuid4().int % (2**32 - 1) + 1),
        receipt("synthetic@example.test"),
        DEFAULT_SCOPE,
    )
    original = await service.session.get(RawEvidence, UUID(result["evidence_id"]))
    review = await service.session.scalar(
        select(ReviewQueueItem).where(ReviewQueueItem.evidence_id == original.id)
    )
    other = RawEvidence(
        source_id=original.source_id,
        source_item_type="email",
        title="A different synthetic receipt",
    )
    service.session.add(other)
    await service.session.flush()
    with pytest.raises(ValueError, match="review_receipt_provenance_conflict"):
        await service._queue_reconciliation_review(
            evidence_id=other.id,
            external_id=original.external_id,
            subject=other.title,
            classification=classification,
        )
    assert review.evidence_id == original.id and review.status == "pending"


async def test_receipt_provenance_upgrade_preserves_identity_and_refuses_ambiguous_keys(
    mailbox_service,
):
    session = mailbox_service.session
    # Temporary tables exercise the actual upgrade without rewriting product data.
    await session.execute(
        text(
            "CREATE TEMP TABLE raw_evidence (id UUID PRIMARY KEY, source_item_type TEXT, external_id TEXT, metadata_json JSONB)"
        )
    )
    await session.execute(
        text(
            "CREATE TEMP TABLE review_queue_items (id UUID PRIMARY KEY, item_type TEXT, item_id UUID, status TEXT, priority_score FLOAT)"
        )
    )
    direct, unique, metadata_only, collision_a, collision_b = [
        uuid4() for _ in range(5)
    ]
    for key, external, metadata in [
        (direct, None, {}),
        (unique, "scoped:unique", {}),
        (metadata_only, None, {"external_id": "legacy:metadata"}),
        (collision_a, "unscoped:duplicate", {}),
        (collision_b, "unscoped:duplicate", {}),
    ]:
        await session.execute(
            text(
                "INSERT INTO raw_evidence VALUES (:id, 'email_order_confirmation', :external, CAST(:metadata AS JSONB))"
            ),
            {"id": key, "external": external, "metadata": json.dumps(metadata)},
        )
    expected = {}
    for kind, item_id, evidence_id in [
        ("mailbox_identity", direct, direct),
        (
            "corporate_action",
            uuid5(NAMESPACE_URL, "mailbox-reconciliation:scoped:unique"),
            unique,
        ),
        (
            "mailbox_receipt",
            uuid5(NAMESPACE_URL, "mailbox-reconciliation:legacy:metadata"),
            metadata_only,
        ),
        (
            "account_transfer",
            uuid5(NAMESPACE_URL, "mailbox-reconciliation:unscoped:duplicate"),
            None,
        ),
        ("corporate_action", uuid4(), None),
        ("entity", direct, None),
    ]:
        key = uuid4()
        expected[key] = (kind, item_id, "in_review", 95.0, evidence_id)
        await session.execute(
            text(
                "INSERT INTO review_queue_items VALUES (:id, :kind, :item, 'in_review', 95.0)"
            ),
            {"id": key, "kind": kind, "item": item_id},
        )
    spec = importlib.util.spec_from_file_location(
        "review_receipt_upgrade",
        Path(__file__).parents[1]
        / "alembic/versions/e0a7c4d2b965_review_receipt_provenance.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def upgrade(connection):
        with Operations.context(MigrationContext.configure(connection)):
            module.upgrade()

    await (await session.connection()).run_sync(upgrade)
    rows = (
        await session.execute(
            text(
                "SELECT id, item_type, item_id, status, priority_score, evidence_id FROM review_queue_items"
            )
        )
    ).all()
    assert {row.id: tuple(row)[1:] for row in rows} == expected
