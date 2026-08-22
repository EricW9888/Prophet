from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel

from investos.schemas.lesson import LessonResponse


class DecisionJournalCreate(BaseModel):
    position_id: Optional[UUID] = None
    decision_type: str
    rationale: str
    expected_catalyst_timeframe: Optional[str] = None
    expected_return: Optional[float] = None


class DecisionReviewCreate(BaseModel):
    decision_journal_id: UUID
    outcome_assessment: str
    actual_return: Optional[float] = None
    mistake_preventable: Optional[bool] = None
    what_went_right: Optional[str] = None
    what_went_wrong: Optional[str] = None
    what_to_improve: Optional[str] = None


class DecisionReviewResponse(BaseModel):
    id: UUID
    decision_journal_id: UUID
    outcome_assessment: str
    actual_return: Optional[float] = None
    mistake_preventable: Optional[bool] = None
    what_went_right: Optional[str] = None
    what_went_wrong: Optional[str] = None
    what_to_improve: Optional[str] = None
    extracted_lessons: list[LessonResponse] = []
    reviewed_at: datetime


class DecisionJournalResponse(BaseModel):
    id: UUID
    position_id: Optional[UUID] = None
    position_label: Optional[str] = None
    decision_type: str
    rationale: str
    expected_catalyst_timeframe: Optional[str] = None
    expected_return: Optional[float] = None
    created_at: datetime
    reviews: list[DecisionReviewResponse] = []
