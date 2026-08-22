from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel


class ReviewQueueItemResponse(BaseModel):
    id: UUID
    item_type: str
    item_id: UUID
    item_label: str
    priority_score: float
    status: str
    trigger_reason: str
    why_now_summary: str
    next_action: str
    signal_tags: list[str]
    size_factor: float
    evidence_change_factor: float
    contradiction_pressure: float
    thesis_drift: float
    catalyst_proximity: float
    coverage_weakness: float
    reasoning_run_id: Optional[UUID] = None
    created_at: datetime
