from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel


class GraphCitationResponse(BaseModel):
    raw_evidence_id: UUID
    source_item_id: Optional[UUID] = None
    source_id: UUID
    source_name: str
    source_type: str
    source_item_type: Optional[str] = None
    origin_kind: str = "catalog"
    origin_label: str = "Source catalog"
    origin_detail: Optional[str] = None
    layer: str = "knowledge"
    is_system: bool = False
    system_reason: Optional[str] = None
    title: Optional[str] = None
    url: Optional[str] = None
    author: Optional[str] = None
    created_at: datetime


class GraphConnectionResponse(BaseModel):
    edge_id: UUID
    direction: str
    relationship_type: str
    confidence: float
    node_id: UUID
    node_type: str
    label: str
    subtitle: Optional[str] = None
    tier: Optional[str] = None
    created_at: Optional[datetime] = None


class GraphNodeDetailResponse(BaseModel):
    id: UUID | str
    node_type: str
    label: str
    layer: str = "knowledge"
    body: Optional[str] = None
    tier: Optional[str] = None
    created_at: Optional[datetime] = None
    relevance: Optional[float] = None
    relevance_reasoning: Optional[str] = None
    properties: dict[str, object] = {}
    citations: list[GraphCitationResponse] = []
    connections: list[GraphConnectionResponse] = []


class GraphWebNodeResponse(BaseModel):
    key: str
    id: UUID | str
    node_type: str
    label: str
    layer: str = "knowledge"
    subtitle: Optional[str] = None
    tier: Optional[str] = None
    created_at: Optional[datetime] = None
    is_root: bool = False
    x: Optional[float] = None
    y: Optional[float] = None
    vx: Optional[float] = None
    vy: Optional[float] = None
    is_autonomous: bool = False


class GraphNodeLayoutItem(BaseModel):
    node_key: str
    x: float
    y: float
    vx: float = 0.0
    vy: float = 0.0


class GraphLayoutSyncRequest(BaseModel):
    layouts: list[GraphNodeLayoutItem]


class GraphWebEdgeResponse(BaseModel):
    id: UUID
    source_key: str
    target_key: str
    relationship_type: str
    confidence: float


class GraphNeighborhoodResponse(BaseModel):
    root_key: str
    depth: int = 1
    nodes: list[GraphWebNodeResponse]
    edges: list[GraphWebEdgeResponse]


class GraphStatsResponse(BaseModel):
    active_facts: int
    active_claims: int
    active_events: int
    deprecated_facts: int
    deprecated_claims: int
    deprecated_events: int
    total_edges: int
    profiles: int
    sources: int
    raw_evidence: int
    source_items: int
    fundamental_metrics: int
    market_setup_signals: int
    active_knowledge_nodes: int
    total_knowledge_nodes: int


class GraphSearchResultResponse(BaseModel):
    node_type: str
    node_id: UUID | str
    label: str
    subtitle: Optional[str] = None
    layer: str = "knowledge"
    created_at: Optional[datetime] = None


class GraphRelationResponse(BaseModel):
    node_a_key: str
    node_b_key: str
    direct_relationships: list[GraphWebEdgeResponse]
    shared_neighbor_keys: list[str]
    summary: str
    nodes: list[GraphWebNodeResponse]
    edges: list[GraphWebEdgeResponse]


class SubjectAliasResponse(BaseModel):
    id: UUID
    alias: str
    normalized_alias: str
    subject_type: str
    subject_id: UUID
    subject_name: str
    source: str
    confidence: float
    reason: Optional[str] = None
    linked_symbols: list[str] = []
    created_at: datetime
    updated_at: datetime


class SubjectAliasSubjectOption(BaseModel):
    subject_type: str
    subject_id: UUID
    subject_name: str
    subtitle: Optional[str] = None
    linked_symbols: list[str] = []
    is_active_holding: bool = False


class SubjectAliasCreate(BaseModel):
    alias: str
    subject_type: str
    subject_id: UUID
    reason: Optional[str] = None


class SubjectAliasUpdate(BaseModel):
    alias: Optional[str] = None
    subject_type: Optional[str] = None
    subject_id: Optional[UUID] = None
    reason: Optional[str] = None
    confidence: Optional[float] = None
