from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class EvidenceSourceReferenceResponse(BaseModel):
    raw_evidence_id: UUID
    source_item_id: UUID | None = None
    source_id: UUID
    source_name: str
    source_type: str
    source_item_type: str | None = None
    origin_kind: str = "catalog"
    origin_label: str = "Source catalog"
    origin_detail: str | None = None
    title: str | None = None
    url: str | None = None
    url_kind: str = "unavailable"
    author: str | None = None
    created_at: datetime


class ReasoningEvidenceSourceResponse(EvidenceSourceReferenceResponse):
    evidence_roles: list[str] = Field(default_factory=list)
    knowledge_node_ids: list[UUID] = Field(default_factory=list)
