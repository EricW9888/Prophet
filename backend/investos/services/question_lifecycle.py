"""Question work is owned by outcomes and due dates, independent of fetched IDs."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError

from investos.models.coverage import CoverageMap, QuestionHistory, UnresolvedQuestion
from investos.models.entity import Security
from investos.models.portfolio import Position

ACTIVE_QUESTION_STATUSES = (
    "open",
    "investigating",
    "needs_research",
    "partial_answer",
    "awaiting_extraction",
    "awaiting_resolution",
    "awaiting_provider",
)
RESEARCH_QUESTION_STATUSES = (
    "open",
    "needs_research",
    "partial_answer",
    "awaiting_provider",
)
RESOLUTION_QUESTION_STATUSES = (
    "investigating",
    "awaiting_resolution",
    "awaiting_extraction",
    "awaiting_provider",
)
# Conservative automation budgets, not a claim about research completeness.
MAX_RESEARCH_ATTEMPTS = 3
MAX_RESOLUTION_ATTEMPTS = 3
RETRY_HOURS = 24
MAX_EXTRACTION_WAIT_DAYS = 7


class QuestionLifecycleService:
    def __init__(self, session):
        self.session = session

    def record(self, question, event_type, **detail):
        self.session.add(
            QuestionHistory(
                question_id=question.id, event_type=event_type, detail_json=detail
            )
        )

    def transition(
        self, question, *, status, outcome, next_action=None, delay_hours=0, **detail
    ):
        previous = question.status
        question.status = status
        question.owner = (
            "research_automation" if status in ACTIVE_QUESTION_STATUSES else "history"
        )
        question.last_material_outcome = outcome
        question.next_action = next_action
        question.next_review_at = (
            datetime.now(UTC) + timedelta(hours=delay_hours) if next_action else None
        )
        self.record(
            question,
            "outcome",
            previous_status=previous,
            status=status,
            outcome=outcome,
            next_action=next_action,
            next_review_at=(
                question.next_review_at.isoformat() if question.next_review_at else None
            ),
            **detail,
        )

    def retry_research(self, question, outcome, *, partial=False, **detail):
        exhausted = (
            int(getattr(question, "research_attempt_count", 0) or 0)
            >= MAX_RESEARCH_ATTEMPTS
            or int(getattr(question, "resolution_attempt_count", 0) or 0)
            >= MAX_RESOLUTION_ATTEMPTS
        )
        self.transition(
            question,
            status=(
                "abandoned"
                if exhausted
                else "partial_answer" if partial else "needs_research"
            ),
            outcome="research_budget_exhausted" if exhausted else outcome,
            next_action=None if exhausted else "research",
            delay_hours=RETRY_HOURS,
            attempt_outcome=outcome,
            **detail,
        )

    @staticmethod
    def urgency(value):
        try:
            return max(1, min(5, int(value or 1)))
        except (TypeError, ValueError):
            return 1

    async def canonical_subject(self, coverage):
        subject_type, subject_id = coverage.subject_type, coverage.subject_id
        if subject_type == "position":
            position = await self.session.get(Position, subject_id)
            if position is not None:
                subject_type, subject_id = "security", position.security_id
        if subject_type == "security":
            security = await self.session.get(Security, subject_id)
            if security is not None:
                subject_type, subject_id = "entity", security.entity_id
        return dict(subject_type=subject_type, subject_id=str(subject_id))

    async def ensure_question(self, coverage, payload, candidates):
        text = " ".join(str(payload.get("question_text") or "").split())
        if not text:
            return None
        identity = {
            field: " ".join(str(payload.get(field) or "").lower().split())
            for field in ("mechanism", "time_horizon", "decision_relevance")
        }
        subject = await self.canonical_subject(coverage)
        identity.update(subject)
        complete = all(
            identity[field]
            for field in ("mechanism", "time_horizon", "decision_relevance")
        )
        key = (
            hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
            if complete
            else None
        )
        existing_id = str(payload.get("existing_question_id") or "")
        existing = next((row for row in candidates if str(row.id) == existing_id), None)
        if existing is not None and existing.superseded_by_id is not None:
            existing = None
        if (
            existing is not None
            and existing.semantic_key is not None
            and existing.semantic_key != key
        ):
            # A model cannot collapse distinct mechanisms, horizons or decisions
            # merely by naming an existing row.
            existing = None
        canonical = (
            await self.session.scalar(
                select(UnresolvedQuestion).where(UnresolvedQuestion.semantic_key == key)
            )
            if key
            else None
        )
        if canonical is not None:
            if existing is not None and existing.id != canonical.id:
                existing.superseded_by_id = canonical.id
                self.transition(
                    existing,
                    status="superseded",
                    outcome="semantic_duplicate",
                    canonical_question_id=str(canonical.id),
                )
                self.record(
                    canonical, "question_merged", previous_question_id=str(existing.id)
                )
            existing = canonical
        if existing is None and not complete:
            # Legacy callers keep exact deduplication until a successful audit
            # supplies semantic dimensions. Never invent dimensions from prose.
            existing = next(
                (
                    row
                    for row in candidates
                    if " ".join(row.question_text.lower().split()).rstrip("?.")
                    == text.lower().rstrip("?.")
                ),
                None,
            )
        if existing is not None:
            if key and existing.semantic_key is None:
                try:
                    async with self.session.begin_nested():
                        existing.semantic_key = key
                        existing.uncertainty_json = identity
                        await self.session.flush()
                except IntegrityError:
                    await self.session.refresh(existing)
                    canonical = await self.session.scalar(
                        select(UnresolvedQuestion).where(
                            UnresolvedQuestion.semantic_key == key
                        )
                    )
                    if canonical is None:
                        raise
                    existing.superseded_by_id = canonical.id
                    self.transition(
                        existing,
                        status="superseded",
                        outcome="semantic_duplicate",
                        canonical_question_id=str(canonical.id),
                    )
                    self.record(
                        canonical,
                        "question_merged",
                        previous_question_id=str(existing.id),
                    )
                    existing = canonical
            existing.urgency = self.urgency(payload.get("urgency", existing.urgency))
            self.record(
                existing,
                "prompt_reinforced",
                question_text=text,
                coverage_map_id=str(coverage.id),
                uncertainty=identity,
            )
            if existing.status in {"obsolete", "archived"}:
                self.transition(
                    existing,
                    status="needs_research",
                    outcome="reprioritized_by_coverage",
                    next_action="research",
                )
            return existing
        question = UnresolvedQuestion(
            coverage_map_id=coverage.id,
            question_text=text,
            urgency=self.urgency(payload.get("urgency")),
            semantic_key=key,
            uncertainty_json=(
                identity
                if complete
                else {**subject, "identity_status": "unclassified_legacy"}
            ),
            status="needs_research",
            owner="research_automation",
            next_action="research",
            next_review_at=datetime.now(UTC),
            last_material_outcome="created",
            research_attempt_count=0,
            resolution_attempt_count=0,
        )
        try:
            async with self.session.begin_nested():
                self.session.add(question)
                await self.session.flush()
        except IntegrityError:
            if key is None:
                raise
            question = await self.session.scalar(
                select(UnresolvedQuestion).where(UnresolvedQuestion.semantic_key == key)
            )
            if question is None:
                raise
        self.record(
            question,
            "prompt_created",
            question_text=text,
            coverage_map_id=str(coverage.id),
            uncertainty=identity,
        )
        return question

    @staticmethod
    def due_clause():
        return or_(
            UnresolvedQuestion.next_review_at.is_(None),
            UnresolvedQuestion.next_review_at <= datetime.now(UTC),
        )

    @staticmethod
    def portfolio_clause():
        tracked = or_(
            Position.list_type.in_(["watchlist", "considering"]),
            (Position.list_type == "holding") & (Position.quantity > 0),
        )
        entity_positions = (
            select(Position.id)
            .join(Security, Position.security_id == Security.id)
            .where(
                Security.entity_id == CoverageMap.subject_id,
                tracked,
            )
            .exists()
        )
        security_positions = (
            select(Position.id)
            .where(
                Position.security_id == CoverageMap.subject_id,
                tracked,
            )
            .exists()
        )
        direct_positions = (
            select(Position.id)
            .where(
                Position.id == CoverageMap.subject_id,
                tracked,
            )
            .exists()
        )
        return or_(
            CoverageMap.subject_type == "portfolio",
            (CoverageMap.subject_type == "entity") & entity_positions,
            (CoverageMap.subject_type == "security") & security_positions,
            (CoverageMap.subject_type == "position") & direct_positions,
        )

    async def snapshot(self):
        outcome_status = QuestionHistory.detail_json["status"].astext.label(
            "outcome_status"
        )
        historical = await self.session.scalar(
            select(func.count()).select_from(UnresolvedQuestion)
        )
        statuses = dict(
            (
                await self.session.execute(
                    select(UnresolvedQuestion.status, func.count()).group_by(
                        UnresolvedQuestion.status
                    )
                )
            ).all()
        )
        eligible = (
            select(func.count())
            .select_from(UnresolvedQuestion)
            .join(CoverageMap)
            .where(
                UnresolvedQuestion.status.in_(ACTIVE_QUESTION_STATUSES),
                self.portfolio_clause(),
            )
        )
        completed = dict(
            (
                await self.session.execute(
                    select(outcome_status, func.count())
                    .where(
                        QuestionHistory.event_type == "outcome",
                        QuestionHistory.created_at
                        >= datetime.now(UTC) - timedelta(days=1),
                        outcome_status.in_(
                            ["answered", "abandoned", "archived", "partial_answer"]
                        ),
                    )
                    .group_by("outcome_status")
                )
            ).all()
        )
        return dict(
            historical=historical or 0,
            by_status=statuses,
            outcomes_last_24h=completed,
            eligible_active=await self.session.scalar(eligible) or 0,
            eligible_due=await self.session.scalar(
                eligible.where(
                    self.due_clause(), UnresolvedQuestion.next_action.is_not(None)
                )
            )
            or 0,
            max_research_attempts=MAX_RESEARCH_ATTEMPTS,
            max_resolution_attempts=MAX_RESOLUTION_ATTEMPTS,
        )

    async def retire_ineligible(self, limit=100):
        rows = (
            await self.session.scalars(
                select(UnresolvedQuestion)
                .join(CoverageMap)
                .where(
                    UnresolvedQuestion.status.in_(ACTIVE_QUESTION_STATUSES),
                    ~self.portfolio_clause(),
                )
                .order_by(UnresolvedQuestion.created_at)
                .limit(limit)
            )
        ).all()
        for question in rows:
            self.transition(
                question, status="archived", outcome="outside_active_portfolio"
            )
        return len(rows)
