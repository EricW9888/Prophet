from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel


class ActiveWatcherResponse(BaseModel):
    id: UUID
    source: str
    source_id: UUID | None = None
    ticker: str | None = None
    entity_id: UUID | None = None
    condition_type: str
    condition_params_json: dict[str, Any] | None = None
    objective: str
    adjustment_plan: str | None = None
    deadline: datetime | None = None
    status: str
    is_active: bool
    last_checked_at: datetime | None = None
    triggered_at: datetime | None = None
    trigger_detail: str | None = None
    created_at: datetime | None = None
    countdown_seconds: int | None = None
    is_overdue: bool = False
    reminder_kind: str = "condition"
