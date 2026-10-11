"""Durable IMAP occurrence identities and explicit legacy reconciliation."""

from __future__ import annotations

import email
import hashlib
import io
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from email.message import Message
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from investos.models.evidence import RawEvidence
from investos.models.mailbox import MailboxReceiptIdentity
from investos.models.portfolio import Transaction
from investos.models.review import ReviewQueueItem


def _digest(value) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=True, sort_keys=True).encode()
    ).hexdigest()


@dataclass(frozen=True)
class MailboxScope:
    account_namespace: str
    folder: str
    uid_validity: str

    @classmethod
    def from_runtime(cls, runtime, uid_validity):
        host = runtime.imap_host.strip().lower()
        username = runtime.username.strip()
        if host in {"imap.gmail.com", "imap.googlemail.com"}:
            host = "imap.gmail.com"
            username = username.lower()
        validity = str(uid_validity)
        if (
            not host
            or not username
            or not validity.isdigit()
            or not 0 < int(validity) < 2**32
        ):
            raise ValueError("gmail_mailbox_identity_unavailable")
        folder = runtime.folder
        if folder.upper() == "INBOX":
            folder = "INBOX"
        if not folder:
            raise ValueError("gmail_mailbox_identity_unavailable")
        return cls(
            _digest([host, runtime.imap_port, username]), folder, str(int(validity))
        )

    def occurrence_key(self, uid: str) -> str:
        if not str(uid).isdigit() or not 0 < int(uid) < 2**32:
            raise ValueError("gmail_invalid_uid")
        return f"imap:v2:{self.account_namespace}:{_digest([self.folder, self.uid_validity, str(int(uid))])}"


class MailboxIdentityService:
    def __init__(self, session: AsyncSession, mailbox):
        self.session = session
        self.mailbox = mailbox

    async def existing(self, key: str) -> RawEvidence | None:
        return await self.session.scalar(
            select(RawEvidence)
            .join(
                MailboxReceiptIdentity,
                MailboxReceiptIdentity.raw_evidence_id == RawEvidence.id,
            )
            .where(MailboxReceiptIdentity.identity_key == key)
        )

    async def resolve(
        self,
        uid: str,
        message: Message,
        scope: MailboxScope,
        *,
        provider_message_id: str | None = None,
        raw_bytes: bytes | None = None,
    ) -> RawEvidence:
        occurrence = scope.occurrence_key(uid)
        known = await self.existing(occurrence)
        if known is not None:
            return known
        raw = raw_bytes if raw_bytes is not None else message.as_bytes()
        raw_hash = hashlib.sha256(raw).hexdigest()
        if provider_message_id is not None:
            if (
                not str(provider_message_id).isdigit()
                or not 0 < int(provider_message_id) < 2**64
            ):
                raise ValueError("gmail_invalid_provider_message_id")
            provider_message_id = str(int(provider_message_id))
        provider_key = (
            f"gmail:v2:{scope.account_namespace}:{provider_message_id}"
            if provider_message_id
            else None
        )
        content_key = f"mail-content:v2:{scope.account_namespace}:{raw_hash}"
        key = provider_key or content_key
        known = await self.existing(key)
        provenance = dict(
            version=2,
            account_namespace=scope.account_namespace,
            folder=scope.folder,
            uid_validity=scope.uid_validity,
            uid=uid,
            provider_message_id=provider_message_id,
            message_id=message.get("Message-ID"),
            raw_sha256=raw_hash,
        )
        known_binding = (
            await self.session.get(MailboxReceiptIdentity, key) if known else None
        )
        conflict = (
            known is not None
            and provider_key
            and known_binding.provenance_json.get("raw_sha256") != raw_hash
        )
        if known is not None and not conflict:
            self._bind(occurrence, "imap_occurrence", known.id, scope, provenance)
            await self.session.flush()
            return known

        legacy_ids = [uid, f"gmail:{scope.folder}:{uid}"]
        sender = self.mailbox._decode_header(message.get("From", ""))
        subject = self.mailbox._decode_header(message.get("Subject", ""))
        public_time = self.mailbox._parse_email_datetime(message.get("Date"))
        legacy_text_hash = hashlib.sha256(
            f"From: {sender}\nSubject: {subject}\n\n{self.mailbox._extract_text_body(message)}".encode()
        ).hexdigest()
        legacy = list(
            (
                await self.session.scalars(
                    select(RawEvidence)
                    .where(
                        RawEvidence.source_item_type.in_(
                            ["email", "email_order_confirmation", "email_skip"]
                        ),
                        RawEvidence.metadata_json["mailbox_identity"].astext.is_(None),
                        or_(
                            RawEvidence.external_id.in_(legacy_ids),
                            RawEvidence.metadata_json["external_id"].astext.in_(
                                legacy_ids
                            ),
                            RawEvidence.content_hash == legacy_text_hash,
                            (
                                (RawEvidence.title == subject)
                                & (RawEvidence.public_time == public_time)
                                & (RawEvidence.author == sender)
                                if public_time is not None
                                else False
                            ),
                        ),
                    )
                    .order_by(RawEvidence.ingest_time.asc())
                    .limit(100)
                )
            ).all()
        )
        candidates = {str(evidence.id) for evidence in legacy}
        reason = "ambiguous_legacy_identity" if candidates else None
        if conflict:
            candidates.add(str(known.id))
            reason = "provider_message_content_changed"
            key = content_key
            same_conflict = await self.existing(key)
            if same_conflict is not None:
                self._bind(
                    occurrence, "imap_occurrence", same_conflict.id, scope, provenance
                )
                await self.session.flush()
                return same_conflict
        if not provider_key:
            # A Message-ID is a hint, never proof. Changed content after a reset
            # must not become a second trade solely because its UID changed.
            prior = list(
                (
                    await self.session.scalars(
                        select(MailboxReceiptIdentity)
                        .where(
                            MailboxReceiptIdentity.account_namespace
                            == scope.account_namespace,
                            MailboxReceiptIdentity.identity_kind == "imap_occurrence",
                            or_(
                                (
                                    MailboxReceiptIdentity.provenance_json[
                                        "message_id"
                                    ].astext
                                    == message.get("Message-ID")
                                    if message.get("Message-ID")
                                    else False
                                ),
                                (
                                    MailboxReceiptIdentity.provenance_json[
                                        "folder"
                                    ].astext
                                    == scope.folder
                                )
                                & (
                                    MailboxReceiptIdentity.provenance_json[
                                        "uid_validity"
                                    ].astext
                                    != scope.uid_validity
                                )
                                & (
                                    MailboxReceiptIdentity.provenance_json["uid"].astext
                                    == uid
                                ),
                            ),
                        )
                        .order_by(MailboxReceiptIdentity.created_at.desc())
                        .limit(100)
                    )
                ).all()
            )
            if prior:
                candidates.update(str(row.raw_evidence_id) for row in prior)
                reason = reason or "unverified_message_after_uidvalidity_change"
            elif await self.session.scalar(
                select(MailboxReceiptIdentity.identity_key)
                .where(
                    MailboxReceiptIdentity.account_namespace == scope.account_namespace,
                    MailboxReceiptIdentity.identity_kind == "imap_occurrence",
                    MailboxReceiptIdentity.provenance_json["folder"].astext
                    == scope.folder,
                    MailboxReceiptIdentity.provenance_json["uid_validity"].astext
                    != scope.uid_validity,
                )
                .limit(1)
            ):
                reason = reason or "unverified_message_after_uidvalidity_change"

        source = await self.mailbox._get_or_create_email_source()
        raw_ref = await self.mailbox.ingestion.storage.put_object(
            io.BytesIO(raw), "receipt.eml", "message/rfc822"
        )
        evidence = RawEvidence(
            source_id=source.id,
            source_item_type="email",
            external_id=key,
            title=subject,
            author=sender,
            public_time=public_time,
            event_time=public_time,
            raw_content_ref=raw_ref,
            content_hash=raw_hash,
            is_processed=True,
            metadata_json=dict(
                external_id=key,
                uid=uid,
                skip_extraction=True,
                operational_mailbox=True,
                mailbox_identity=provenance,
                raw_content_format="rfc822",
                mailbox_status="needs_review" if reason else "identity_established",
            ),
        )
        if reason:
            evidence.metadata_json["identity_review"] = dict(
                status="pending",
                reason=reason,
                candidate_evidence_ids=sorted(candidates),
            )
        self.session.add(evidence)
        await self.session.flush()
        self._bind(
            key,
            "gmail_message" if provider_key and not conflict else "exact_content",
            evidence.id,
            scope,
            provenance,
        )
        self._bind(occurrence, "imap_occurrence", evidence.id, scope, provenance)
        if reason:
            self.session.add(
                ReviewQueueItem(
                    item_type="mailbox_identity",
                    item_id=evidence.id,
                    priority_score=95,
                    trigger_reason="Mailbox identity is ambiguous; compare preserved receipts before posting.",
                )
            )
        await self.session.flush()
        return evidence

    def _bind(self, key, kind, evidence_id, scope, provenance):
        self.session.add(
            MailboxReceiptIdentity(
                identity_key=key,
                identity_kind=kind,
                account_namespace=scope.account_namespace,
                raw_evidence_id=evidence_id,
                provenance_json=provenance,
            )
        )

    async def reviews(self):
        evidence_rows = list(
            (
                await self.session.scalars(
                    select(RawEvidence)
                    .where(
                        RawEvidence.metadata_json["identity_review"]["status"].astext
                        == "pending"
                    )
                    .order_by(RawEvidence.ingest_time.asc())
                    .limit(100)
                )
            ).all()
        )
        results = []
        for evidence in evidence_rows:
            review = evidence.metadata_json["identity_review"]
            candidates = []
            for evidence_id in review["candidate_evidence_ids"]:
                candidate = await self.session.get(RawEvidence, UUID(evidence_id))
                if candidate is not None:
                    candidates.append(
                        dict(
                            evidence_id=str(candidate.id),
                            title=candidate.title,
                            public_time=candidate.public_time,
                            external_id=candidate.external_id,
                            mailbox_identity=(candidate.metadata_json or {}).get(
                                "mailbox_identity"
                            ),
                            **await self._review_content(candidate),
                        )
                    )
            results.append(
                dict(
                    evidence_id=str(evidence.id),
                    title=evidence.title,
                    public_time=evidence.public_time,
                    reason=review["reason"],
                    mailbox_identity=evidence.metadata_json["mailbox_identity"],
                    candidates=candidates,
                    **await self._review_content(evidence),
                )
            )
        return results

    async def _review_content(self, evidence):
        content = None
        if evidence.raw_content_ref:
            try:
                raw = await self.mailbox.ingestion.storage.get_object(
                    evidence.raw_content_ref
                )
                if (evidence.metadata_json or {}).get(
                    "raw_content_format"
                ) == "rfc822" or evidence.raw_content_ref.endswith(".eml"):
                    content = self.mailbox._extract_text_body(
                        email.message_from_bytes(raw)
                    )
                else:
                    content = raw.decode("utf-8", errors="replace")
            except (OSError, ValueError):
                pass
        transactions = list(
            (
                await self.session.scalars(
                    select(Transaction).where(
                        Transaction.provenance_json["raw_evidence_id"].astext
                        == str(evidence.id)
                    )
                )
            ).all()
        )
        return dict(
            content_excerpt=content[:5000] if content else None,
            content_truncated=bool(content and len(content) > 5000),
            transactions=[
                dict(
                    id=str(row.id),
                    status=row.status,
                    action=row.action,
                    quantity=str(row.quantity),
                    price=str(row.price) if row.price is not None else None,
                )
                for row in transactions
            ],
        )

    async def reconcile(
        self, evidence_id: UUID, *, decision: str, existing_evidence_id: UUID | None
    ):
        evidence = await self.session.scalar(
            select(RawEvidence).where(RawEvidence.id == evidence_id).with_for_update()
        )
        if evidence is None:
            raise ValueError("Receipt does not exist.")
        metadata = dict(evidence.metadata_json or {})
        review = dict(metadata.get("identity_review") or {})
        if review.get("status") != "pending":
            raise ValueError("Receipt has no pending identity review.")
        if await self.session.scalar(
            select(Transaction.id)
            .where(
                Transaction.provenance_json["raw_evidence_id"].astext
                == str(evidence.id)
            )
            .limit(1)
        ):
            raise ValueError("A posted receipt cannot be rebound.")
        if decision == "link_existing":
            if str(existing_evidence_id) not in review.get(
                "candidate_evidence_ids", []
            ):
                raise ValueError("Select a preserved candidate receipt.")
            existing = await self.session.get(RawEvidence, existing_evidence_id)
            if (
                existing is None
                or (existing.metadata_json or {})
                .get("identity_review", {})
                .get("status")
                == "pending"
            ):
                raise ValueError("Candidate identity must be established first.")
            seen = {evidence.id}
            for _ in range(20):
                if existing.id in seen:
                    raise ValueError("Receipt identity links contain a cycle.")
                seen.add(existing.id)
                prior_review = (existing.metadata_json or {}).get("identity_review", {})
                if prior_review.get("decision") != "link_existing":
                    break
                target_id = prior_review.get(
                    "canonical_evidence_id"
                ) or prior_review.get("existing_evidence_id")
                existing = await self.session.get(RawEvidence, UUID(target_id))
                if (
                    existing is None
                    or (existing.metadata_json or {})
                    .get("identity_review", {})
                    .get("status")
                    == "pending"
                ):
                    raise ValueError(
                        "Linked candidate identity must be established first."
                    )
            else:
                raise ValueError(
                    "Receipt identity link chain requires manual inspection."
                )
            bindings = list(
                (
                    await self.session.scalars(
                        select(MailboxReceiptIdentity).where(
                            MailboxReceiptIdentity.raw_evidence_id == evidence.id
                        )
                    )
                ).all()
            )
            for binding in bindings:
                binding.raw_evidence_id = existing.id
                binding.provenance_json = {
                    **binding.provenance_json,
                    "identity_review_receipt_id": str(evidence.id),
                    "linked_at": datetime.now(UTC).isoformat(),
                }
            metadata["mailbox_status"] = "identity_duplicate"
            review["canonical_evidence_id"] = str(existing.id)
        elif decision == "create_distinct":
            if existing_evidence_id is not None:
                raise ValueError("A distinct receipt must not select another receipt.")
            metadata["mailbox_status"] = "identity_established"
        else:
            raise ValueError("Unsupported identity decision.")
        metadata["identity_review"] = {
            **review,
            "status": "resolved",
            "decision": decision,
            "existing_evidence_id": (
                str(existing_evidence_id) if existing_evidence_id else None
            ),
            "reviewed_at": datetime.now(UTC).isoformat(),
        }
        evidence.metadata_json = metadata
        queue_rows = list(
            (
                await self.session.scalars(
                    select(ReviewQueueItem).where(
                        ReviewQueueItem.item_type == "mailbox_identity",
                        ReviewQueueItem.item_id == evidence.id,
                    )
                )
            ).all()
        )
        for item in queue_rows:
            item.status = "resolved"
            item.resolved_at = datetime.now(UTC)
        await self.session.commit()
        return dict(evidence_id=str(evidence.id), status="resolved", decision=decision)
