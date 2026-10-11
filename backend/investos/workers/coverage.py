import json
import re
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from investos.core.llm import call_llm_json, compact_exception_message
from investos.core.prompting import compact_text
from investos.models.base import utcnow
from investos.models.coverage import (
    CoverageMap,
    MissingEvidenceClass,
    UnresolvedQuestion,
)
from investos.models.graph import Edge
from investos.models.knowledge import Claim, Fact
from investos.services.artifact_hygiene import is_artifact_subject_name
from investos.services.canonical_state import CanonicalStateService
from investos.services.question_lifecycle import (
    ACTIVE_QUESTION_STATUSES,
    QuestionLifecycleService,
)

COVERAGE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "overall_coverage_score": {"type": "number"},
        "missing_classes": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "class_name": {"type": "string"},
                    "importance_to_thesis": {"type": "string"},
                },
                "required": ["class_name", "importance_to_thesis"],
            },
        },
        "unresolved_questions": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "question_text": {"type": "string"},
                    "urgency": {"type": "integer"},
                    "mechanism": {"type": "string"},
                    "time_horizon": {"type": "string"},
                    "decision_relevance": {"type": "string"},
                    "existing_question_id": {"type": ["string", "null"]},
                },
                "required": [
                    "question_text",
                    "urgency",
                    "mechanism",
                    "time_horizon",
                    "decision_relevance",
                    "existing_question_id",
                ],
            },
        },
    },
    "required": ["overall_coverage_score", "missing_classes", "unresolved_questions"],
}


class CoverageWorker:
    def __init__(self, session: AsyncSession):
        self.session = session

    @staticmethod
    def _is_artifact_subject_name(subject_name: str | None) -> bool:
        return is_artifact_subject_name(subject_name)

    @staticmethod
    def _subject_specific_questions(
        *,
        subject_name: str,
        fact_count: int,
        claim_count: int,
        contradiction_count: int,
    ) -> list[dict[str, object]]:
        name = " ".join((subject_name or "").split()).strip() or "this subject"
        questions: list[dict[str, object]] = []
        thin = fact_count + claim_count < 4

        def add(text: str, urgency: int) -> None:
            if text not in {str(item["question_text"]) for item in questions}:
                questions.append({"question_text": text, "urgency": urgency})

        if thin:
            add(
                f"Which concrete operating, financial, competitive, valuation, or timing metric would most change the view on {name}?",
                3,
            )
            add(
                f"What primary or high-quality source should be used to verify the central mechanism behind the current {name} thesis?",
                3,
            )
        if contradiction_count == 0:
            add(
                f"What specific counterfactual would falsify the current thesis on {name}, and what source would prove or disprove it?",
                2,
            )
        return questions

    async def _refresh_subject_counts(
        self, subject_id: UUID, subject_type: str, subject_name: str
    ) -> tuple[CoverageMap, list[Fact], list[Claim]]:
        edges = list(
            (
                await self.session.execute(
                    select(Edge).where(
                        Edge.target_type == subject_type,
                        Edge.target_id == subject_id,
                        Edge.source_type.in_(["fact", "claim"]),
                    )
                )
            )
            .scalars()
            .all()
        )
        fact_ids = [edge.source_id for edge in edges if edge.source_type == "fact"]
        claim_ids = [edge.source_id for edge in edges if edge.source_type == "claim"]

        facts = (
            list(
                (
                    await self.session.execute(
                        select(Fact).where(
                            Fact.id.in_(fact_ids),
                            Fact.is_deprecated.is_(False),
                        )
                    )
                )
                .scalars()
                .all()
            )
            if fact_ids
            else []
        )
        claims = (
            list(
                (
                    await self.session.execute(
                        select(Claim).where(
                            Claim.id.in_(claim_ids),
                            Claim.is_deprecated.is_(False),
                        )
                    )
                )
                .scalars()
                .all()
            )
            if claim_ids
            else []
        )

        c_map = await CanonicalStateService(self.session).ensure_coverage_map(
            subject_type=subject_type,
            subject_id=subject_id,
            create=lambda: CoverageMap(
                subject_id=subject_id,
                subject_type=subject_type,
                evidence_class_coverage_json={},
            ),
        )

        c_map.total_evidence_count = len(facts) + len(claims)
        c_map.high_tier_evidence_count = len(
            [fact for fact in facts if fact.tier in {"hard_fact", "strong_derived"}]
        )
        c_map.contradiction_count = len(
            [
                claim
                for claim in claims
                if claim.contradiction_role == "contradicts_consensus"
            ]
        )
        c_map.unresolved_contradiction_count = c_map.contradiction_count
        c_map.evidence_class_coverage_json = {
            "hard_facts": bool(facts),
            "claims": bool(claims),
            "contradictions": c_map.contradiction_count > 0,
            "historical_context": False,
            "benchmark_context": False,
        }
        c_map.overall_coverage_score = min(
            100.0,
            (len(facts) * 12.5) + (len(claims) * 7.5),
        )

        if self._is_artifact_subject_name(subject_name):
            c_map.overall_coverage_score = 0.0
            c_map.evidence_class_coverage_json = {
                **(c_map.evidence_class_coverage_json or {}),
                "internal_artifact": True,
            }
        return c_map, facts, claims

    async def refresh_subject_counts(
        self, subject_id: UUID, subject_type: str, subject_name: str
    ) -> CoverageMap:
        """Refresh objective coverage counts without analysis or a commit."""
        c_map, _, _ = await self._refresh_subject_counts(
            subject_id=subject_id,
            subject_type=subject_type,
            subject_name=subject_name,
        )
        return c_map

    async def audit_subject_coverage(
        self, subject_id: UUID, subject_type: str, subject_name: str
    ) -> CoverageMap:
        c_map, facts, claims = await self._refresh_subject_counts(
            subject_id=subject_id,
            subject_type=subject_type,
            subject_name=subject_name,
        )

        if self._is_artifact_subject_name(subject_name):
            await self.session.commit()
            return c_map

        existing_questions = list(
            (
                await self.session.execute(
                    select(UnresolvedQuestion)
                    .where(UnresolvedQuestion.coverage_map_id == c_map.id)
                    .order_by(
                        UnresolvedQuestion.urgency.desc(),
                        UnresolvedQuestion.created_at.asc(),
                    )
                    .limit(30)
                )
            )
            .scalars()
            .all()
        )
        evidence_context = {
            "existing_uncertainties": [
                dict(
                    id=str(row.id),
                    question_text=row.question_text,
                    dimensions=row.uncertainty_json or {},
                    status=row.status,
                )
                for row in existing_questions
            ],
            "subject_name": subject_name,
            "subject_type": subject_type,
            "facts": [
                {
                    "statement": compact_text(fact.statement, max_chars=180),
                    "tier": fact.tier,
                    "importance": fact.importance,
                }
                for fact in facts[:12]
            ],
            "claims": [
                {
                    "statement": compact_text(claim.statement, max_chars=180),
                    "tier": claim.tier,
                    "importance": claim.importance,
                    "contradiction_role": claim.contradiction_role,
                }
                for claim in claims[:12]
            ],
            "current_counts": {
                "facts": len(facts),
                "claims": len(claims),
                "high_tier_facts": c_map.high_tier_evidence_count,
                "contradictions": c_map.contradiction_count,
            },
        }
        audit_completed = False
        try:
            audit = await call_llm_json(
                system_prompt=(
                    "Audit coverage for an Prophet profile. "
                    "Identify what evidence classes are missing and what questions remain unresolved. "
                    "Do not assume completeness. Keep the list short and decision-relevant. "
                    "Describe each uncertainty by mechanism, time_horizon and decision_relevance. "
                    "For a paraphrase of an existing uncertainty, return its existing_question_id and reuse its exact dimensions. "
                    "Different mechanisms, horizons and decisions must remain distinct. Do not treat exhausted research as answered."
                ),
                user_prompt=json.dumps(evidence_context, ensure_ascii=True),
                schema=COVERAGE_SCHEMA,
            )
            audit_completed = True
        except Exception as exc:
            import logging

            logging.getLogger(__name__).warning(
                "Coverage LLM skipped after failure: %s",
                compact_exception_message(exc),
            )
            audit = self._fallback_coverage_audit(
                subject_name=subject_name,
                facts=facts,
                claims=claims,
                contradiction_count=int(c_map.contradiction_count or 0),
            )
        c_map.overall_coverage_score = float(audit["overall_coverage_score"])
        missing_classes = audit.get("missing_classes", [])
        unresolved_questions = audit.get("unresolved_questions", [])

        # Provider failure must not erase a subject's durable research backlog
        # or replace it with generic fallback questions. Reconcile only a
        # successful structured audit; deterministic fallback still refreshes
        # counts and the coverage score.
        if audit_completed:
            await self._reconcile_missing_classes(c_map.id, missing_classes)
            await self._reconcile_unresolved_questions(c_map.id, unresolved_questions)

        await self.session.commit()
        return c_map

    @staticmethod
    def _normalized_gap_key(value: str | None) -> str:
        return " ".join(re.sub(r"[^a-z0-9]+", " ", (value or "").casefold()).split())

    async def _reconcile_missing_classes(
        self,
        coverage_map_id: UUID,
        missing_classes: list[dict[str, object]],
    ) -> None:
        existing = list(
            (
                await self.session.execute(
                    select(MissingEvidenceClass).where(
                        MissingEvidenceClass.coverage_map_id == coverage_map_id
                    )
                )
            )
            .scalars()
            .all()
        )
        existing_by_key = {
            self._normalized_gap_key(item.class_name): item
            for item in existing
            if self._normalized_gap_key(item.class_name)
        }
        incoming_keys: set[str] = set()
        for payload in missing_classes[:6]:
            class_name = " ".join(str(payload.get("class_name") or "").split()).strip()
            importance = " ".join(
                str(payload.get("importance_to_thesis") or "").split()
            ).strip()
            key = self._normalized_gap_key(class_name)
            if not key or key in incoming_keys:
                continue
            incoming_keys.add(key)
            item = existing_by_key.get(key)
            if item is None:
                self.session.add(
                    MissingEvidenceClass(
                        coverage_map_id=coverage_map_id,
                        class_name=class_name,
                        importance_to_thesis=importance,
                    )
                )
                continue
            item.class_name = class_name
            item.importance_to_thesis = importance
            item.resolved_at = None

        for key, item in existing_by_key.items():
            if key not in incoming_keys and item.resolved_at is None:
                item.resolved_at = utcnow()

    async def _reconcile_unresolved_questions(
        self, coverage_map_id: UUID, unresolved_questions: list[dict[str, object]]
    ) -> None:
        coverage = await self.session.get(CoverageMap, coverage_map_id)
        if coverage is None:
            return
        lifecycle = QuestionLifecycleService(self.session)
        existing = list(
            (
                await self.session.execute(
                    select(UnresolvedQuestion)
                    .where(UnresolvedQuestion.coverage_map_id == coverage_map_id)
                    .order_by(UnresolvedQuestion.created_at)
                )
            )
            .scalars()
            .all()
        )
        grouped = {}
        for question in existing:
            grouped.setdefault(
                question.semantic_key
                or self._normalized_gap_key(question.question_text),
                [],
            ).append(question)
        for rows in grouped.values():
            rows.sort(key=lambda row: (row.status != "answered", row.created_at))
            for duplicate in rows[1:]:
                if duplicate.status in ACTIVE_QUESTION_STATUSES:
                    duplicate.superseded_by_id = rows[0].id
                    lifecycle.transition(
                        duplicate,
                        status="superseded",
                        outcome="duplicate_prompt",
                        canonical_question_id=str(rows[0].id),
                    )
        retained = set()
        for payload in unresolved_questions[:5]:
            # Exact legacy matching is only a bridge to structured identity.
            if not payload.get("existing_question_id"):
                match = next(
                    (
                        row
                        for row in existing
                        if row.status != "superseded"
                        and self._normalized_gap_key(row.question_text)
                        == self._normalized_gap_key(
                            str(payload.get("question_text") or "")
                        )
                    ),
                    None,
                )
                if match is not None:
                    payload = {**payload, "existing_question_id": str(match.id)}
            question = await lifecycle.ensure_question(coverage, payload, existing)
            if question is not None:
                retained.add(question.id)
                if question not in existing:
                    existing.append(question)
        for question in existing:
            if (
                question.id not in retained
                and question.status in ACTIVE_QUESTION_STATUSES
            ):
                lifecycle.transition(
                    question,
                    status="archived",
                    outcome="coverage_no_longer_prioritized",
                )

    def _fallback_coverage_audit(
        self,
        *,
        subject_name: str,
        facts: list[Fact],
        claims: list[Claim],
        contradiction_count: int,
    ) -> dict[str, object]:
        missing_classes: list[dict[str, str]] = []
        unresolved_questions: list[dict[str, object]] = []
        fact_count = len(facts)
        claim_count = len(claims)
        high_tier_facts = len(
            [fact for fact in facts if fact.tier in {"hard_fact", "strong_derived"}]
        )

        if self._is_artifact_subject_name(subject_name):
            return {
                "overall_coverage_score": 0.0,
                "missing_classes": [],
                "unresolved_questions": [],
            }

        if high_tier_facts < 2:
            missing_classes.append(
                {
                    "class_name": "higher-tier evidence",
                    "importance_to_thesis": "Need stronger primary or strongly-derived evidence before treating the view as robust.",
                }
            )
        if claim_count == 0:
            missing_classes.append(
                {
                    "class_name": "interpretive claims",
                    "importance_to_thesis": "Need analytical interpretation connecting facts to the investment thesis.",
                }
            )
        if contradiction_count == 0:
            missing_classes.append(
                {
                    "class_name": "contradictory evidence",
                    "importance_to_thesis": "Need at least one serious opposing view so the system does not become one-sided.",
                }
            )
        unresolved_questions.extend(
            self._subject_specific_questions(
                subject_name=subject_name,
                fact_count=fact_count,
                claim_count=claim_count,
                contradiction_count=contradiction_count,
            )
        )

        score = min(
            100.0,
            float((fact_count * 12.5) + (claim_count * 7.5) + (high_tier_facts * 5.0)),
        )
        return {
            "overall_coverage_score": score,
            "missing_classes": missing_classes[:4],
            "unresolved_questions": unresolved_questions[:4],
        }
