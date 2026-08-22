import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, utcnow


class KnowledgeMutation(Base):
    """Durable audit event for knowledge and graph lifecycle changes."""

    __tablename__ = "knowledge_mutations"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    node_type: Mapped[str] = mapped_column(String, index=True)
    node_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    change_type: Mapped[str] = mapped_column(String, index=True)
    reason: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    actor: Mapped[str] = mapped_column(String, default="system", index=True)
    source_type: Mapped[Optional[str]] = mapped_column(
        String, nullable=True, index=True
    )
    source_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True, index=True
    )
    subject_type: Mapped[Optional[str]] = mapped_column(
        String, nullable=True, index=True
    )
    subject_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True, index=True
    )
    metadata_json: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
