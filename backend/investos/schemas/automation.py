from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class AutomationJobStatus(BaseModel):
    name: str
    enabled: bool
    interval_seconds: Optional[int] = None
    last_run_at: Optional[datetime] = None
    last_status: str = "idle"
    detail: Optional[str] = None


class AutomationStatusResponse(BaseModel):
    automation_enabled: bool
    jobs: list[AutomationJobStatus]
    provider_health: dict = Field(default_factory=dict)
