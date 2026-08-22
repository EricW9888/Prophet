from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, Field


class AgentTurnRequest(BaseModel):
    session_id: UUID | None = None
    subject_id: UUID | None = None
    subject_type: str | None = None
    message: str
    auto_execute: bool = True


class AgentActionResponse(BaseModel):
    action_type: str
    status: str
    summary: str
    resource_id: Optional[UUID] = None
    resource_type: Optional[str] = None


class AgentActionLogEntryResponse(BaseModel):
    id: str
    timestamp: datetime
    source: str
    action_type: str
    status: str
    summary: str
    subject_id: str | None = None
    subject_type: str | None = None
    subject_name: str | None = None
    metadata: dict | None = None


class AgentActionLogResponse(BaseModel):
    actions: list[AgentActionLogEntryResponse] = Field(default_factory=list)


class AgentTurnResponse(BaseModel):
    session_id: UUID
    assistant_message: str
    subject_id: UUID
    subject_type: str
    subject_name: str | None = None
    resolution_reason: str | None = None
    process_mode: str | None = None
    reasoning_run_id: Optional[UUID] = None
    stance: Optional[str] = None
    confidence_band: Optional[str] = None
    thesis_summary: Optional[str] = None
    rationale_summary: Optional[str] = None
    source_feedback_influence: dict | None = None
    historical_analogy_lenses: list[dict] | None = None
    actions: list[AgentActionResponse] = Field(default_factory=list)
    subagents: dict[str, str] | None = None
    responded_at: datetime


class AgentTurnJobEventResponse(BaseModel):
    phase: str
    message: str
    created_at: datetime
    detail: dict | None = None


class AgentTurnJobResponse(BaseModel):
    job_id: UUID
    status: str
    request_message: str
    session_id: UUID | None = None
    created_at: datetime
    updated_at: datetime
    events: list[AgentTurnJobEventResponse] = Field(default_factory=list)
    result: AgentTurnResponse | None = None
    error: str | None = None


class AgentTurnJobListResponse(BaseModel):
    jobs: list[AgentTurnJobResponse] = Field(default_factory=list)


class AgentContextCandidateResponse(BaseModel):
    subject_id: UUID
    subject_type: str
    subject_name: str
    score: int
    reason: str


class AgentResolveResponse(BaseModel):
    subject_id: UUID
    subject_type: str
    subject_name: str
    resolution_reason: str
    candidates: list[AgentContextCandidateResponse] = Field(default_factory=list)


class AgentConversationEntryResponse(BaseModel):
    id: UUID
    role: str
    content: str
    created_at: datetime
    message_kind: str = "chat"
    is_artifact: bool = False
    origin: str | None = None
    process_mode: str | None = None
    resolution_reason: str | None = None
    reasoning_run_id: UUID | None = None
    stance: str | None = None
    confidence_band: str | None = None
    thesis_summary: str | None = None
    rationale_summary: str | None = None
    source_feedback_influence: dict | None = None
    historical_analogy_lenses: list[dict] | None = None
    actions: list[AgentActionResponse] = Field(default_factory=list)
    subagents: dict[str, str] | None = None


class AgentConversationHistoryResponse(BaseModel):
    session_id: UUID | None = None
    subject_id: UUID
    subject_type: str
    entries: list[AgentConversationEntryResponse] = Field(default_factory=list)


class AgentConversationSummaryResponse(BaseModel):
    session_id: UUID
    title: str
    subject_id: UUID | None = None
    subject_type: str | None = None
    subject_name: str | None = None
    latest_message_preview: str | None = None
    artifact_count: int = 0
    updated_at: datetime


class AgentConversationListResponse(BaseModel):
    conversations: list[AgentConversationSummaryResponse] = Field(default_factory=list)


class AgentConversationUpdateRequest(BaseModel):
    title: str = Field(min_length=1, max_length=120)
