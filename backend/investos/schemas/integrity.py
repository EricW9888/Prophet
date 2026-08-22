from pydantic import BaseModel


class IntegrityAuditCounts(BaseModel):
    profiles: int = 0
    coverage_maps: int = 0
    conclusion_states: int = 0
    unresolved_questions_open: int = 0
    sources: int = 0
    raw_evidence: int = 0
    source_items: int = 0
    facts: int = 0
    claims: int = 0
    events: int = 0
    edges: int = 0
    duplicate_source_groups: int = 0
    duplicate_edge_groups: int = 0
    orphan_edges: int = 0
    unknown_edge_node_types: int = 0
    missing_storage_objects: int = 0


class IntegrityDuplicateSubject(BaseModel):
    subject_type: str
    subject_id: str
    count: int


class IntegrityDuplicateEdge(BaseModel):
    source_type: str
    source_id: str
    target_type: str
    target_id: str
    relationship_type: str
    count: int


class IntegrityAuditResponse(BaseModel):
    ok: bool
    counts: IntegrityAuditCounts
    duplicate_sources: list[IntegrityDuplicateSubject] = []
    duplicate_coverage_subjects: list[IntegrityDuplicateSubject] = []
    duplicate_conclusion_subjects: list[IntegrityDuplicateSubject] = []
    duplicate_edges: list[IntegrityDuplicateEdge] = []
    unknown_edge_node_types: list[str] = []
