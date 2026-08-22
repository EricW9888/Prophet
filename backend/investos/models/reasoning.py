import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, utcnow


class EvidencePacket(Base):
    """Frozen snapshot of everything fed to a reasoning run."""

    __tablename__ = "evidence_packets"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    assembled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )

    query_text: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    subject_type: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    subject_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )

    direct_evidence_ids: Mapped[Optional[list[uuid.UUID]]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), nullable=True
    )
    connected_evidence_ids: Mapped[Optional[list[uuid.UUID]]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), nullable=True
    )
    historical_evidence_ids: Mapped[Optional[list[uuid.UUID]]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), nullable=True
    )
    contradiction_evidence_ids: Mapped[Optional[list[uuid.UUID]]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), nullable=True
    )
    lesson_ids: Mapped[Optional[list[uuid.UUID]]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), nullable=True
    )

    portfolio_context_json: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    coverage_map_snapshot_json: Mapped[Optional[dict]] = mapped_column(
        JSONB, nullable=True
    )

    retrieval_layers_used: Mapped[Optional[list[str]]] = mapped_column(
        ARRAY(String), nullable=True
    )
    gap_flags: Mapped[Optional[list[str]]] = mapped_column(ARRAY(String), nullable=True)

    total_token_estimate: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    @property
    def direct_evidence_count(self) -> int:
        return len(self.direct_evidence_ids or [])

    @property
    def connected_evidence_count(self) -> int:
        return len(self.connected_evidence_ids or [])

    @property
    def historical_evidence_count(self) -> int:
        return len(self.historical_evidence_ids or [])

    @property
    def contradiction_evidence_count(self) -> int:
        return len(self.contradiction_evidence_ids or [])

    @property
    def lesson_count(self) -> int:
        return len(self.lesson_ids or [])


class ReasoningRun(Base):
    """Record of a single LLM reasoning operation."""

    __tablename__ = "reasoning_runs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    evidence_packet_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("evidence_packets.id"), nullable=True
    )

    run_type: Mapped[str] = mapped_column(
        String, index=True
    )  # analysis|memo|implication|profile_update|research_planning|...

    model_used: Mapped[str] = mapped_column(String)
    model_version: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    prompt_hash: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    input_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    cost_usd: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    output_text: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    structured_output_json: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)

    duration_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class CritiqueRun(Base):
    """Record of a critique/review pass on a reasoning run."""

    __tablename__ = "critique_runs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    reasoning_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("reasoning_runs.id"), index=True
    )

    model_used: Mapped[str] = mapped_column(String)
    critique_text: Mapped[str] = mapped_column(String)

    issues_found: Mapped[Optional[list[str]]] = mapped_column(
        ARRAY(String), nullable=True
    )
    severity: Mapped[str] = mapped_column(String)  # none|minor|major|critical

    input_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    cost_usd: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
