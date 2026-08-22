from datetime import datetime

from pydantic import BaseModel, Field


class SetupStepResponse(BaseModel):
    id: str
    label: str
    description: str
    status: str
    status_label: str | None = None
    detail: str | None = None
    hint: str | None = None
    action_label: str | None = None
    href: str | None = None


class SetupStatusResponse(BaseModel):
    status: str
    completion_ratio: float
    next_recommended_step: str | None = None
    development_reset_enabled: bool = False
    steps: list[SetupStepResponse] = Field(default_factory=list)


class DevelopmentResetRequest(BaseModel):
    confirmation_text: str


class DevelopmentResetResponse(BaseModel):
    ok: bool
    detail: str
    reset_at: datetime
    cleared_tables: list[str] = Field(default_factory=list)
    preserved_tables: list[str] = Field(default_factory=list)
    storage_cleared: bool = False
    runtime_settings_reset: bool = False
    warnings: list[str] = Field(default_factory=list)
