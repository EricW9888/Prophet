from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class LessonResponse(BaseModel):
    id: UUID
    title: str
    summary: str
    lesson_type: str
    applicable_sectors: list[str] = Field(default_factory=list)
    applicable_regimes: list[str] = Field(default_factory=list)
    originating_decision_review_id: Optional[UUID] = None
    originating_experiment_result_id: Optional[UUID] = None
    experiment_family_id: Optional[UUID] = None
    maturity_status: str = "active"
    confidence_score: float = 0.0
    supporting_observations: int = 0
    contradicting_observations: int = 0
    neutral_observations: int = 0
    last_validated_at: Optional[datetime] = None
    stale_after: Optional[datetime] = None
    metadata_json: dict = Field(default_factory=dict)
    usage_count: int
    created_at: datetime

    @field_validator("applicable_sectors", "applicable_regimes", mode="before")
    @classmethod
    def normalize_optional_lists(cls, value: list[str] | None) -> list[str]:
        return value or []

    @field_validator("metadata_json", mode="before")
    @classmethod
    def normalize_optional_metadata(cls, value: dict | None) -> dict:
        return value or {}

    model_config = ConfigDict(from_attributes=True)
