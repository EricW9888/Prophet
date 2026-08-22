from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel


class VerificationRequest(BaseModel):
    subject_id: UUID
    subject_type: str
    trigger: str = "user_challenge"
    challenge_text: Optional[str] = None


class VerificationResponse(BaseModel):
    id: UUID
    subject_id: UUID
    subject_type: str
    trigger: str
    prior_stance: str
    verified_stance: str
    confidence_band: str
    conclusion_changed: bool
    contradiction_coverage_status: str
    missing_classes_found: list[str] = []
    change_reasoning: str
    what_would_falsify: list[str] = []
    supporting_evidence_ids: list[str] = []
    contradicting_evidence_ids: list[str] = []
    verified_at: datetime
