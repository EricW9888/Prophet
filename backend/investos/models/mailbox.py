import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, utcnow


class MailboxReceiptIdentity(Base):
    """One unique occurrence/provider identity, bound to preserved evidence."""

    __tablename__ = "mailbox_receipt_identities"

    identity_key: Mapped[str] = mapped_column(String, primary_key=True)
    identity_kind: Mapped[str] = mapped_column(String, index=True)
    account_namespace: Mapped[str] = mapped_column(String, index=True)
    raw_evidence_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("raw_evidence.id"), index=True
    )
    provenance_json: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
