from datetime import datetime
from typing import Any, Optional
from uuid import UUID

from pydantic import BaseModel, Field

from investos.schemas.provenance import ReasoningEvidenceSourceResponse


class EvidencePacketSummaryResponse(BaseModel):
    id: UUID
    query_text: Optional[str] = None
    subject_type: Optional[str] = None
    subject_id: Optional[UUID] = None
    assembled_at: datetime
    retrieval_layers_used: list[str] = Field(default_factory=list)
    gap_flags: list[str] = Field(default_factory=list)
    total_token_estimate: Optional[int] = None
    direct_evidence_count: int = 0
    connected_evidence_count: int = 0
    historical_evidence_count: int = 0
    contradiction_evidence_count: int = 0
    sources: list[ReasoningEvidenceSourceResponse] = Field(default_factory=list)
    coverage_snapshot: dict[str, Any] = Field(default_factory=dict)
    portfolio_context: dict[str, Any] = Field(default_factory=dict)


class CritiqueTraceResponse(BaseModel):
    id: UUID
    model_used: str
    critique_text: str
    issues_found: list[str] = Field(default_factory=list)
    severity: str
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    created_at: datetime


class ReasoningRunTraceResponse(BaseModel):
    id: UUID
    run_type: str
    model_used: str
    model_version: Optional[str] = None
    prompt_hash: Optional[str] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    cost_usd: Optional[float] = None
    duration_ms: Optional[int] = None
    created_at: datetime
    output_text: Optional[str] = None
    structured_output_json: dict[str, Any] = Field(default_factory=dict)
    evidence_packet: Optional[EvidencePacketSummaryResponse] = None
    critique: Optional[CritiqueTraceResponse] = None
