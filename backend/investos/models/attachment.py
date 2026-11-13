from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, utcnow


class ChatAttachment(Base):
    """Private conversation inputs, deliberately outside autonomous evidence intake."""

    __tablename__ = "chat_attachments"
    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    name: Mapped[str] = mapped_column(String(200))
    content_type: Mapped[str] = mapped_column(String(100))
    size_bytes: Mapped[int] = mapped_column(Integer)
    content_hash: Mapped[str] = mapped_column(String(64))
    storage_path: Mapped[str] = mapped_column(Text)
    extracted_text: Mapped[str] = mapped_column(Text, default="")
    representation_json: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
