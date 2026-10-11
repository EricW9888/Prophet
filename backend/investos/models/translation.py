"""Read-only presentation derivatives, never inputs to extraction or beliefs."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, utcnow


class EvidenceTranslation(Base):
    __tablename__ = "evidence_translations"
    __table_args__ = (
        UniqueConstraint(
            "evidence_id",
            "source_hash",
            "target_language",
            "recipe_version",
            name="uq_evidence_translation_version_language",
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    evidence_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("raw_evidence.id"), index=True
    )
    source_hash: Mapped[str] = mapped_column(String(64))
    target_language: Mapped[str] = mapped_column(String)
    recipe_version: Mapped[str] = mapped_column(String)
    provider: Mapped[str] = mapped_column(String)
    translated_text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
