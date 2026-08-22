from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class RawEvidenceBase(BaseModel):
    title: Optional[str] = None
    source_id: Optional[UUID] = None
    source_item_type: str = "manual_upload"
    url: Optional[str] = None
    author: Optional[str] = None
    public_time: Optional[datetime] = None
    metadata_json: Optional[dict] = None


class RawEvidenceCreate(RawEvidenceBase):
    content: str


class UrlEvidenceCreate(BaseModel):
    url: str
    title: Optional[str] = None
    source_id: Optional[UUID] = None
    source_item_type: str = "web_research"
    author: Optional[str] = None
    process_now: bool = True


class RawEvidenceResponse(RawEvidenceBase):
    id: UUID
    raw_content_ref: Optional[str] = None
    content_hash: Optional[str] = None
    is_processed: bool
    created_at: datetime
    ingest_time: datetime

    model_config = ConfigDict(from_attributes=True)
