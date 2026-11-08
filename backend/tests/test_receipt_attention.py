"""Real receipt producer, retained holds, ledger links and paginated ordering."""

import copy
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, update

from investos.models.evidence import RawEvidence
from investos.models.portfolio import Transaction
from investos.models.review import ReviewQueueItem
from investos.services.receipt_attention import ReceiptAttentionService
from investos.services.review import ReviewService
from tests.test_mailbox_identity import scope
from tests.test_mailbox_recovery import mailbox_service, receipt


@pytest.fixture(autouse=True)
async def isolated_reviews(mailbox_service):
    # This fixture's outer transaction rolls back all changes to the synthetic DB.
    await mailbox_service.session.execute(
        update(ReviewQueueItem).values(status="dismissed")
    )


async def held_receipt(
    service,
    monkeypatch,
    classification,
    *,
    candidate=None,
    body="Synthetic correspondence",
    subject=None,
):
    uid = str(uuid4().int % (2**32 - 1) + 1)
    message = receipt()
    message.replace_header("Subject", subject or f"Synthetic {uuid4()}")
    message.set_content(body)
    source = await service._get_or_create_email_source()
    legacy = RawEvidence(
        source_id=source.id,
        source_item_type="email",
        external_id=uid,
        title=message["Subject"],
        metadata_json={"classification": candidate} if candidate is not None else {},
    )
    service.session.add(legacy)
    await service.session.flush()
    classify = AsyncMock(return_value=classification)
    monkeypatch.setattr(service, "_classify_message", classify)
    result = await service._process_message(
        uid, message, scope(), allow_model=classification is not None
    )
    assert result["needs_reconciliation"] and not result["transaction_created"]
    incoming = await service.session.get(RawEvidence, UUID(result["evidence_id"]))
    item = await service.session.scalar(
        select(ReviewQueueItem).where(ReviewQueueItem.item_id == incoming.id)
    )
    return incoming, legacy, item, classify


@pytest.mark.parametrize(
    "kind", ["order_confirmation", "account_transfer", "corporate_action"]
)
async def test_financial_identity_holds_outrank_correspondence_before_pagination(
    mailbox_service, monkeypatch, kind
):
    service = mailbox_service
    transaction_count = await service.session.scalar(
        select(func.count()).select_from(Transaction)
    )
    correspondence = {"document_type": "newsletter", "confidence": 0.0}
    promo, _, low, _ = await held_receipt(
        service, monkeypatch, correspondence, candidate=correspondence
    )
    financial, _, high, _ = await held_receipt(
        service, monkeypatch, {"document_type": kind, "confidence": 0.5}
    )
    before = copy.deepcopy(financial.metadata_json["identity_review"])
    raw_ref = financial.raw_content_ref
    await service.session.flush()
    queue = ReviewService(service.session)
    first = await queue.queue_page(domain="portfolio_data", limit=1)
    assert first.items[0].id == high.id
    assert high.priority_score > low.priority_score
    assert low.priority_score == 30
    assert "provisional" in high.trigger_reason and "unverified" in low.trigger_reason
    assert financial.metadata_json["identity_review"] == before
    assert financial.raw_content_ref == raw_ref
    assert high.evidence_id == financial.id and low.evidence_id == promo.id
    assert high.status == low.status == "pending"
    assert transaction_count == await service.session.scalar(
        select(func.count()).select_from(Transaction)
    )


async def test_promotional_subject_cannot_demote_supported_confirmation_in_body(
    mailbox_service, monkeypatch
):
    current, _, item, classify = await held_receipt(
        mailbox_service,
        monkeypatch,
        {"document_type": "newsletter", "confidence": 0},
        body="Your individual account deposit is complete. Amount: $123.45",
        subject="Synthetic product newsletter",
    )
    assert item.priority_score == 95
    assert (
        current.metadata_json["mailbox_classification"]["document_type"]
        == "cash_activity"
    )
    classify.assert_not_awaited()
    assert current.metadata_json["identity_review"]["status"] == "pending"


@pytest.mark.parametrize(
    "classification,candidate,expected",
    [
        (None, None, 80),
        ({"document_type": "other", "confidence": 0}, None, 80),
        ({"document_type": "newsletter", "confidence": 0}, None, 80),
        (
            {"document_type": "newsletter", "quantity": 2},
            {"document_type": "newsletter"},
            95,
        ),
        ({"document_type": "newsletter"}, {"document_type": "corporate_action"}, 95),
    ],
)
async def test_unknown_or_conflicting_context_is_not_demoted(
    mailbox_service, monkeypatch, classification, candidate, expected
):
    _, _, item, _ = await held_receipt(
        mailbox_service, monkeypatch, classification, candidate=candidate
    )
    assert item.priority_score == expected
    assert item.status == "pending"


async def test_linked_canceled_ledger_candidate_wins_without_changing_provenance(
    mailbox_service, monkeypatch
):
    service = mailbox_service
    original = await service._process_message(
        "1", receipt(), scope(), provider_message_id="8765"
    )
    posted = await service.session.get(Transaction, UUID(original["transaction_id"]))
    posted.status = "canceled"
    before = copy.deepcopy(posted.provenance_json)
    current, _, item, _ = await held_receipt(
        service, monkeypatch, {"document_type": "newsletter"}
    )
    current.metadata_json = {
        **current.metadata_json,
        "identity_review": {
            **current.metadata_json["identity_review"],
            "candidate_evidence_ids": [original["evidence_id"]],
        },
    }
    item.status = "in_review"
    await ReceiptAttentionService(service.session).refresh()
    assert item.priority_score == 100 and "ledger activity" in item.trigger_reason
    assert item.status == "in_review" and posted.status == "canceled"
    assert posted.provenance_json == before
    assert current.metadata_json["identity_review"]["status"] == "pending"


async def test_refresh_backlog_keeps_missing_candidates_and_stable_ties(
    mailbox_service, monkeypatch
):
    service = mailbox_service
    newsletter = {"document_type": "newsletter"}
    current, _, item, classify = await held_receipt(
        service, monkeypatch, newsletter, candidate=newsletter
    )
    current.metadata_json = {
        **current.metadata_json,
        "identity_review": {
            **current.metadata_json["identity_review"],
            "candidate_evidence_ids": [str(uuid4()), "invalid"],
        },
    }
    await ReceiptAttentionService(service.session).refresh()
    assert item.priority_score == 80
    classify.reset_mock()
    before = copy.deepcopy(current.metadata_json)
    await ReceiptAttentionService(service.session).refresh()
    first = await ReviewService(service.session).queue_page(
        domain="portfolio_data", limit=1
    )
    second = await ReviewService(service.session).queue_page(
        domain="portfolio_data", limit=1
    )
    assert first.items[0].id == second.items[0].id
    assert current.metadata_json == before
    classify.assert_not_awaited()


async def test_excerpted_newsletter_classification_keeps_missing_body_uncertain(
    mailbox_service, monkeypatch
):
    newsletter = {"document_type": "newsletter", "confidence": 0}
    current, _, item, _ = await held_receipt(
        mailbox_service,
        monkeypatch,
        newsletter,
        candidate=newsletter,
        body="Synthetic correspondence " * 500,
    )
    assert current.metadata_json["attention_classification"]["input_truncated"]
    assert item.priority_score == 80 and "incomplete" in item.trigger_reason
