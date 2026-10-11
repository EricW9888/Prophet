"""Synthetic mail and real PostgreSQL exercise receipt identity and ledger links."""

from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from investos.models.evidence import RawEvidence
from investos.models.mailbox import MailboxReceiptIdentity
from investos.models.portfolio import Transaction
from investos.models.review import ReviewQueueItem
from investos.services.mailbox_identity import MailboxScope
from tests.test_mailbox_recovery import mailbox_service, receipt


def scope(*, username="synthetic@example.test", folder="Statements", validity="11"):
    return MailboxScope.from_runtime(
        SimpleNamespace(
            imap_host="imap.gmail.com", imap_port=993, username=username, folder=folder
        ),
        validity,
    )


def test_namespaces_are_stable_private_and_separate():
    first = scope()
    assert first == scope(username="SYNTHETIC@example.test")
    assert "@" not in first.account_namespace
    keys = {
        first.occurrence_key("1"),
        scope(username="second@example.test").occurrence_key("1"),
        scope(folder="Other label").occurrence_key("1"),
        scope(validity="12").occurrence_key("1"),
    }
    assert len(keys) == 4
    assert MailboxScope.from_runtime(
        SimpleNamespace(
            imap_host="mail.example.test",
            imap_port=993,
            username="CaseSensitive",
            folder="INBOX",
        ),
        "11",
    ) != MailboxScope.from_runtime(
        SimpleNamespace(
            imap_host="mail.example.test",
            imap_port=993,
            username="casesensitive",
            folder="INBOX",
        ),
        "11",
    )


@pytest.mark.parametrize("validity", [None, "", "0", "4294967296", "invalid"])
def test_missing_or_invalid_uidvalidity_never_uses_a_raw_uid(validity):
    with pytest.raises(ValueError, match="identity_unavailable"):
        scope(validity=validity)


async def test_same_uid_in_different_accounts_is_not_already_imported(mailbox_service):
    service = mailbox_service
    message = receipt()
    first = await service._process_message(
        "1", message, scope(), provider_message_id="7001"
    )
    assert first["transaction_created"]
    assert not await service._already_ingested(
        "1", scope(username="second@example.test")
    )
    second = await service._process_message(
        "1", message, scope(username="second@example.test"), provider_message_id="7001"
    )
    assert second["transaction_created"]
    assert first["evidence_id"] != second["evidence_id"]


async def test_gmail_label_copy_and_uidvalidity_reset_bind_one_corrected_transaction(
    mailbox_service,
):
    service = mailbox_service
    message = receipt()
    first = await service._process_message(
        "1", message, scope(), provider_message_id="7002"
    )
    evidence_id = UUID(first["evidence_id"])
    transaction = await service.session.get(Transaction, UUID(first["transaction_id"]))
    source_identity = transaction.source_identity
    transaction.status = "corrected"
    await service.session.commit()
    for uid, current_scope in [
        ("3", scope(folder="Another label")),
        ("1", scope(validity="12")),
    ]:
        result = await service._process_message(
            uid, message, current_scope, provider_message_id="7002"
        )
        assert result["skipped_existing"]
        assert result["evidence_id"] == str(evidence_id)
        assert await service._already_ingested(uid, current_scope)
    count = await service.session.scalar(
        select(func.count())
        .select_from(Transaction)
        .where(
            Transaction.provenance_json["raw_evidence_id"].astext == str(evidence_id)
        )
    )
    assert count == 1
    assert transaction.source_identity == source_identity
    assert transaction.status == "corrected"
    bindings = list(
        (
            await service.session.scalars(
                select(MailboxReceiptIdentity).where(
                    MailboxReceiptIdentity.raw_evidence_id == evidence_id
                )
            )
        ).all()
    )
    assert len(bindings) == 4
    assert {row.provenance_json["uid_validity"] for row in bindings} == {"11", "12"}


async def test_generic_reset_exact_content_deduplicates_changed_content_waits_for_review(
    mailbox_service,
):
    service = mailbox_service
    message = receipt()
    first = await service._process_message("1", message, scope())
    copy = await service._process_message("9", message, scope(validity="12"))
    assert copy["skipped_existing"]
    assert copy["evidence_id"] == first["evidence_id"]
    changed = receipt()
    changed.replace_header("Message-ID", message["Message-ID"])
    changed.set_content("Your individual account deposit is complete. Amount: $234.56")
    pending = await service._process_message("10", changed, scope(validity="12"))
    assert pending["needs_reconciliation"]
    assert not pending["transaction_created"]
    evidence = await service.session.get(RawEvidence, UUID(pending["evidence_id"]))
    assert (
        await service.ingestion.storage.get_object(evidence.raw_content_ref)
        == changed.as_bytes()
    )
    assert (
        str(first["evidence_id"])
        in evidence.metadata_json["identity_review"]["candidate_evidence_ids"]
    )


async def test_reused_provider_id_with_changed_content_preserves_conflict(
    mailbox_service,
):
    service = mailbox_service
    first_message = receipt()
    first = await service._process_message(
        "1", first_message, scope(), provider_message_id="7003"
    )
    changed = receipt()
    changed.set_content("Your individual account deposit is complete. Amount: $999.00")
    result = await service._process_message(
        "2", changed, scope(folder="Other"), provider_message_id="7003"
    )
    assert result["needs_reconciliation"]
    evidence = await service.session.get(RawEvidence, UUID(result["evidence_id"]))
    assert (
        evidence.metadata_json["identity_review"]["reason"]
        == "provider_message_content_changed"
    )
    assert evidence.metadata_json["identity_review"]["candidate_evidence_ids"] == [
        first["evidence_id"]
    ]


async def test_filtered_label_copy_cannot_rewrite_a_posted_receipt(mailbox_service):
    service = mailbox_service
    message = receipt()
    first = await service._process_message(
        "1", message, scope(), provider_message_id="7006"
    )
    evidence = await service.session.get(RawEvidence, UUID(first["evidence_id"]))
    result = await service._record_skip(
        "2", message, scope(folder="Different label"), provider_message_id="7006"
    )
    assert result == {"skipped_existing": True}
    assert evidence.source_item_type == "email_order_confirmation"
    assert evidence.metadata_json["operational_mailbox"] is True
    assert evidence.metadata_json["mailbox_status"] == "classified"


async def test_legacy_text_receipt_is_a_review_candidate_when_all_occurrence_ids_change(
    mailbox_service,
):
    from investos.schemas.evidence import RawEvidenceCreate

    service = mailbox_service
    message = receipt()
    source = await service._get_or_create_email_source()
    legacy = await service.ingestion.ingest_text(
        RawEvidenceCreate(
            source_id=source.id,
            source_item_type="email_order_confirmation",
            metadata_json={
                "external_id": "gmail:Old label:99",
                "operational_mailbox": True,
                "skip_extraction": True,
            },
            content="From: notifications@robinhood.com\nSubject: Your individual account deposit is complete\n\nYour individual account deposit is complete. Amount: $123.45",
        ),
        process_now=False,
    )
    result = await service._process_message(
        "8", message, scope(folder="New label"), provider_message_id="7009"
    )
    assert result["needs_reconciliation"] and not result["transaction_created"]
    incoming = await service.session.get(RawEvidence, UUID(result["evidence_id"]))
    assert (
        str(legacy.id)
        in incoming.metadata_json["identity_review"]["candidate_evidence_ids"]
    )


@pytest.mark.parametrize("decision", ["link_existing", "create_distinct"])
async def test_legacy_migration_is_explicit_and_preserves_evidence_corrections_and_links(
    mailbox_service, decision
):
    service = mailbox_service
    message = receipt()
    original = await service._process_message(
        "7", message, scope(), provider_message_id="7004"
    )
    legacy = await service.session.get(RawEvidence, UUID(original["evidence_id"]))
    legacy.external_id = "7"
    legacy.metadata_json = {"external_id": "7", "operational_mailbox": True}
    old_ref = legacy.raw_content_ref
    transaction = await service.session.get(
        Transaction, UUID(original["transaction_id"])
    )
    transaction.status = "canceled"
    old_source_identity = transaction.source_identity
    # Represent an earlier importer with no established namespace aliases.
    bindings = list(
        (
            await service.session.scalars(
                select(MailboxReceiptIdentity).where(
                    MailboxReceiptIdentity.raw_evidence_id == legacy.id
                )
            )
        ).all()
    )
    for binding in bindings:
        await service.session.delete(binding)
    await service.session.commit()
    # A label copy can change both folder and UID. The original content/time
    # provenance must still identify a legacy candidate instead of double-posting.
    pending_scope = scope(folder="Copied label", validity="12")
    pending = await service._process_message(
        "9", message, pending_scope, provider_message_id="7005"
    )
    incoming_id = UUID(pending["evidence_id"])
    assert incoming_id != legacy.id and pending["needs_reconciliation"]
    assert await service._already_ingested("9", pending_scope)
    reviews = await service.identities.reviews()
    assert reviews[0]["candidates"][0]["evidence_id"] == str(legacy.id)
    assert "$123.45" in reviews[0]["content_excerpt"]
    assert "$123.45" in reviews[0]["candidates"][0]["content_excerpt"]
    assert reviews[0]["candidates"][0]["transactions"][0]["status"] == "canceled"
    if decision == "link_existing":
        changed = receipt()
        changed.set_content(
            "Your individual account deposit is complete. Amount: $999.00"
        )
        conflict_scope = scope(folder="Conflicting copy")
        conflict = await service._process_message(
            "10", changed, conflict_scope, provider_message_id="7005"
        )
        conflict_id = UUID(conflict["evidence_id"])
        assert conflict["needs_reconciliation"]
    await service.identities.reconcile(
        incoming_id,
        decision=decision,
        existing_evidence_id=legacy.id if decision == "link_existing" else None,
    )
    assert legacy.raw_content_ref == old_ref and legacy.external_id == "7"
    assert (
        transaction.status == "canceled"
        and transaction.source_identity == old_source_identity
    )
    queue = await service.session.scalar(
        select(ReviewQueueItem).where(ReviewQueueItem.item_id == incoming_id)
    )
    assert queue.status == "resolved"
    assert (await service.session.get(RawEvidence, incoming_id)).metadata_json[
        "identity_review"
    ]["decision"] == decision
    if decision == "link_existing":
        await service.identities.reconcile(
            conflict_id, decision="link_existing", existing_evidence_id=incoming_id
        )
        assert (await service._existing_receipt("10", conflict_scope)).id == legacy.id
        conflict_evidence = await service.session.get(RawEvidence, conflict_id)
        assert conflict_evidence.metadata_json["identity_review"][
            "canonical_evidence_id"
        ] == str(legacy.id)
        result = await service._process_message(
            "9", message, pending_scope, provider_message_id="7005"
        )
        assert result["skipped_existing"] and result["evidence_id"] == str(legacy.id)
        another_label = await service._process_message(
            "9", message, scope(folder="Another"), provider_message_id="7005"
        )
        assert another_label["skipped_existing"] and another_label[
            "evidence_id"
        ] == str(legacy.id)
    else:
        assert not await service._already_ingested("9", pending_scope)
        result = await service._process_message(
            "9", message, pending_scope, provider_message_id="7005"
        )
        assert result["transaction_created"]


async def test_identity_binding_is_unique_in_database(mailbox_service):
    from sqlalchemy.exc import IntegrityError

    service = mailbox_service
    evidence = await service.identities.resolve("1", receipt(), scope())
    await service.session.commit()
    service.session.add(
        MailboxReceiptIdentity(
            identity_key=scope().occurrence_key("1"),
            identity_kind="imap_occurrence",
            account_namespace=scope().account_namespace,
            raw_evidence_id=evidence.id,
            provenance_json={},
        )
    )
    with pytest.raises(IntegrityError):
        await service.session.flush()
    await service.session.rollback()


async def test_actual_migration_marks_legacy_without_rewriting_ledger_or_receipts(
    mailbox_service,
):
    import importlib.util
    import json
    from pathlib import Path

    import sqlalchemy as sa

    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    path = (
        Path(__file__).parents[1] / "alembic/versions/8e3a7c1d4b29_mailbox_identity.py"
    )
    spec = importlib.util.spec_from_file_location("mailbox_identity_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    def run(connection):
        # A private transaction schema isolates actual migration DDL and data
        # transforms from every other test and the configured instance.
        schema = "test_mailbox_migration_" + uuid4().hex
        connection.execute(sa.text(f'CREATE SCHEMA "{schema}"'))
        connection.execute(sa.text(f'SET LOCAL search_path TO "{schema}", public'))
        connection.execute(sa.text("""CREATE TABLE raw_evidence (
            id uuid PRIMARY KEY, external_id text, source_item_type text,
            metadata_json jsonb, raw_content_ref text)"""))
        receipt_id = uuid4()
        connection.execute(
            sa.text("""INSERT INTO raw_evidence VALUES
            (:id, 'gmail:Statements:7', 'email_order_confirmation',
             CAST(:metadata AS jsonb), 'original.eml')"""),
            {
                "id": receipt_id,
                "metadata": json.dumps(
                    {"operational_mailbox": True, "original": "preserved"}
                ),
            },
        )
        connection.execute(
            sa.text(
                "CREATE TABLE preserved_ledger (receipt_id uuid REFERENCES raw_evidence(id), source_identity text, status text)"
            )
        )
        connection.execute(
            sa.text(
                "INSERT INTO preserved_ledger VALUES (:id, 'email_order_confirmation:gmail:Statements:7', 'corrected')"
            ),
            {"id": receipt_id},
        )
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        evidence = (
            connection.execute(sa.text("SELECT * FROM raw_evidence")).mappings().one()
        )
        assert (
            evidence["id"] == receipt_id
            and evidence["external_id"] == "gmail:Statements:7"
        )
        assert evidence["raw_content_ref"] == "original.eml"
        assert evidence["metadata_json"]["original"] == "preserved"
        assert (
            evidence["metadata_json"]["mailbox_identity_migration"]["status"]
            == "legacy_unverified"
        )
        assert (
            connection.execute(
                sa.text("SELECT count(*) FROM mailbox_receipt_identities")
            ).scalar_one()
            == 0
        )
        ledger = (
            connection.execute(sa.text("SELECT * FROM preserved_ledger"))
            .mappings()
            .one()
        )
        assert ledger["receipt_id"] == receipt_id
        assert (
            ledger["source_identity"] == "email_order_confirmation:gmail:Statements:7"
        )
        assert ledger["status"] == "corrected"
        with pytest.raises(RuntimeError, match="cannot be discarded"):
            migration.downgrade()

    await (await mailbox_service.session.connection()).run_sync(run)
