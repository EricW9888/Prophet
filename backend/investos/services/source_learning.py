from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse
from uuid import UUID

from sqlalchemy import delete, desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from investos.core.llm import call_llm_json, compact_exception_message
from investos.models.catalog import (
    SourceProfile,
    SourceTrustProfile,
    SourceValueProfile,
)
from investos.models.evidence import RawEvidence, SourceItem
from investos.models.source import Source, SourceQualitySegment

SOURCE_EVALUATION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "source_type": {
            "type": "string",
            "enum": [
                "x_account",
                "youtube",
                "email",
                "filing",
                "news",
                "manual",
                "web_research",
                "analyst",
                "official",
                "peer_research",
                "ownership_tracker",
            ],
        },
        "specialization_domains": {"type": "array", "items": {"type": "string"}},
        "known_weaknesses": {"type": "array", "items": {"type": "string"}},
        "factual_reliability": {
            "type": "string",
            "enum": ["very_low", "low", "medium", "high", "very_high"],
        },
        "calibration": {
            "type": "string",
            "enum": ["under_confident", "calibrated", "over_confident"],
        },
        "correction_quality": {
            "type": "string",
            "enum": ["never_corrects", "slow_corrects", "fast_corrects"],
        },
        "noise_ratio": {
            "type": "string",
            "enum": ["very_noisy", "noisy", "moderate", "clean", "very_clean"],
        },
        "trust_trajectory": {
            "type": "string",
            "enum": ["improving", "stable", "degrading", "compromised"],
        },
        "idea_generation_value": {
            "type": "string",
            "enum": ["none", "low", "medium", "high"],
        },
        "timing_value": {
            "type": "string",
            "enum": ["none", "low", "medium", "high"],
        },
        "portfolio_relevance_value": {
            "type": "string",
            "enum": ["none", "low", "medium", "high"],
        },
        "specificity": {
            "type": "string",
            "enum": ["vague", "moderate", "specific", "very_specific"],
        },
        "originality": {
            "type": "string",
            "enum": ["repeater", "occasional_original", "primary_source"],
        },
        "quality_score": {"type": "number"},
        "originality_score": {"type": "number"},
        "timing_usefulness": {"type": "number"},
        "should_promote_to_trusted": {"type": "boolean"},
        "trust_reasoning": {"type": "string"},
    },
    "required": [
        "source_type",
        "specialization_domains",
        "known_weaknesses",
        "factual_reliability",
        "calibration",
        "correction_quality",
        "noise_ratio",
        "trust_trajectory",
        "idea_generation_value",
        "timing_value",
        "portfolio_relevance_value",
        "specificity",
        "originality",
        "quality_score",
        "originality_score",
        "timing_usefulness",
        "should_promote_to_trusted",
        "trust_reasoning",
    ],
}


@dataclass
class SourceContext:
    source: Source
    display_name: str
    inferred_type: str


class SourceLearningService:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_or_create_source_for_url(
        self,
        *,
        url: str | None,
        title: str | None,
        preferred_type: str = "web_research",
        description: str | None = None,
    ) -> SourceContext:
        normalized_url = (url or "").strip() or None
        display_name = self._display_name(normalized_url, title)
        inferred_type = self._infer_source_type(normalized_url, preferred_type)

        source = None
        if normalized_url:
            source = (
                await self.session.execute(
                    select(Source).where(Source.url == normalized_url)
                )
            ).scalar_one_or_none()
        if source is None:
            source = (
                await self.session.execute(
                    select(Source).where(Source.name == display_name)
                )
            ).scalar_one_or_none()
        if source is None:
            source = Source(
                name=display_name,
                source_type=inferred_type,
                url=normalized_url,
                description=description or "Auto-discovered by Prophet research.",
                is_trusted=False,
            )
            self.session.add(source)
            await self.session.flush()
        else:
            if normalized_url and not source.url:
                source.url = normalized_url
            if source.source_type == "web_research" and inferred_type != "web_research":
                source.source_type = inferred_type
            if not source.description and description:
                source.description = description
            await self.session.flush()
        return SourceContext(
            source=source, display_name=display_name, inferred_type=inferred_type
        )

    async def learn_from_source(
        self,
        *,
        source_id: UUID,
        subject_name: str | None = None,
        subject_type: str | None = None,
    ) -> dict[str, Any]:
        source = (
            await self.session.execute(select(Source).where(Source.id == source_id))
        ).scalar_one_or_none()
        if source is None:
            return {"learned": False, "reason": "source_not_found"}

        evidence_rows = (
            await self.session.execute(
                select(RawEvidence, SourceItem)
                .outerjoin(SourceItem, SourceItem.raw_evidence_id == RawEvidence.id)
                .where(RawEvidence.source_id == source.id)
                .order_by(desc(RawEvidence.created_at))
                .limit(8)
            )
        ).all()
        if not evidence_rows:
            return {"learned": False, "reason": "no_evidence"}

        evidence_count = len(evidence_rows)
        titles = []
        excerpts = []
        for raw_evidence, source_item in evidence_rows:
            titles.append(
                raw_evidence.title or raw_evidence.url or "Untitled research item"
            )
            excerpts.append(
                (source_item.summary if source_item else None)
                or (raw_evidence.title or "")
            )
        feedback_counts, feedback_notes = self._feedback_signals(evidence_rows)

        evaluation = await self._evaluate_source(
            source=source,
            subject_name=subject_name,
            subject_type=subject_type,
            titles=titles,
            excerpts=excerpts,
            evidence_count=evidence_count,
            feedback_counts=feedback_counts,
            feedback_notes=feedback_notes,
        )
        evaluation = self._apply_feedback_adjustment(evaluation, feedback_counts)

        source.source_type = evaluation["source_type"]
        if not source.description:
            source.description = evaluation["trust_reasoning"]

        await self._upsert_source_profiles(source, evaluation, evidence_count)
        await self._upsert_quality_segments(
            source, evaluation, subject_name, subject_type, evidence_count
        )

        promote = bool(evaluation["should_promote_to_trusted"])
        if source.source_type in {"official", "filing"} and evidence_count >= 1:
            promote = True
        if evidence_count < 2 and source.source_type not in {"official", "filing"}:
            promote = False
        source.apply_learned_trust(
            promote,
            reason=evaluation["trust_reasoning"],
        )
        await self.session.commit()
        await self.session.refresh(source)
        return {
            "learned": True,
            "source_id": str(source.id),
            "source_name": source.name,
            "source_type": source.source_type,
            "is_trusted": source.is_trusted,
            "trust_origin": source.trust_origin,
            "trust_review_status": source.trust_review_status,
            "trust_review_reason": source.trust_review_reason,
            "evidence_count": evidence_count,
            "trust_reasoning": evaluation["trust_reasoning"],
        }

    async def _evaluate_source(
        self,
        *,
        source: Source,
        subject_name: str | None,
        subject_type: str | None,
        titles: list[str],
        excerpts: list[str],
        evidence_count: int,
        feedback_counts: dict[str, int] | None = None,
        feedback_notes: list[str] | None = None,
    ) -> dict[str, Any]:
        domain = self._domain(source.url)
        feedback_counts = feedback_counts or {"useful": 0, "not_useful": 0}
        feedback_notes = feedback_notes or []
        fallback = {
            "source_type": source.source_type or "web_research",
            "specialization_domains": [subject_name] if subject_name else [],
            "known_weaknesses": [],
            "factual_reliability": "medium",
            "calibration": "calibrated",
            "correction_quality": "slow_corrects",
            "noise_ratio": "moderate",
            "trust_trajectory": "stable",
            "idea_generation_value": "medium",
            "timing_value": "medium",
            "portfolio_relevance_value": "medium" if subject_name else "low",
            "specificity": "moderate",
            "originality": "occasional_original",
            "quality_score": 0.6,
            "originality_score": 0.5,
            "timing_usefulness": 0.5,
            "should_promote_to_trusted": False,
            "trust_reasoning": "Source has been observed, but there is not enough evidence yet to promote it confidently.",
        }
        try:
            return await call_llm_json(
                system_prompt=(
                    "You are evaluating whether a discovered source should become part of Prophet trusted operating memory. "
                    "Use only the observed source metadata and recent evidence titles/summaries. "
                    "Be conservative: only recommend trusted promotion when the source appears directly useful, sufficiently specific, and likely to be portfolio-relevant. "
                    "Favor official/company/regulatory/analyst sources over generic repeaters. "
                    "Treat insider, ownership, congressional, and institutional-flow trackers as evidence sources whose timing delay, specificity, and later outcome accuracy must be measured rather than assumed."
                ),
                user_prompt=(
                    f"Source name: {source.name}\n"
                    f"Source url: {source.url or 'unknown'}\n"
                    f"Domain: {domain or 'unknown'}\n"
                    f"Current source type: {source.source_type}\n"
                    f"Current description: {source.description or 'none'}\n"
                    f"Current subject focus: {subject_name or 'portfolio-wide'} ({subject_type or 'unknown'})\n"
                    f"Observed evidence count: {evidence_count}\n"
                    f"User feedback counts: useful={feedback_counts.get('useful', 0)}, not_useful={feedback_counts.get('not_useful', 0)}\n"
                    "Recent user feedback notes:\n- "
                    + "\n- ".join(feedback_notes[:5] or ["none"])
                    + "\n"
                    "Recent evidence titles:\n- "
                    + "\n- ".join(titles[:6])
                    + "\nRecent evidence excerpts:\n- "
                    + "\n- ".join(excerpts[:4])
                ),
                schema=SOURCE_EVALUATION_SCHEMA,
                timeout_seconds=10,
            )
        except Exception as exc:
            import logging

            logging.getLogger(__name__).warning(
                "Source evaluation fell back after LLM failure: %s",
                compact_exception_message(exc),
            )
            return fallback

    @staticmethod
    def _feedback_signals(
        evidence_rows: list[tuple[RawEvidence, SourceItem | None]],
    ) -> tuple[dict[str, int], list[str]]:
        counts = {"useful": 0, "not_useful": 0}
        notes: list[str] = []
        for raw_evidence, _source_item in evidence_rows:
            feedback = (raw_evidence.metadata_json or {}).get("user_feedback")
            if not isinstance(feedback, dict):
                continue
            rating = str(feedback.get("rating") or "").strip()
            if rating not in counts:
                continue
            counts[rating] += 1
            note = str(feedback.get("note") or "").strip()
            label = raw_evidence.title or raw_evidence.url or "Untitled evidence"
            if note:
                notes.append(f"{rating}: {label}: {note[:240]}")
            else:
                notes.append(f"{rating}: {label}")
        return counts, notes

    @staticmethod
    def _apply_feedback_adjustment(
        evaluation: dict[str, Any],
        feedback_counts: dict[str, int],
    ) -> dict[str, Any]:
        useful = int(feedback_counts.get("useful") or 0)
        not_useful = int(feedback_counts.get("not_useful") or 0)
        total = useful + not_useful
        if total <= 0:
            return evaluation

        adjusted = dict(evaluation)
        net = (useful - not_useful) / total
        quality_delta = max(-0.25, min(0.25, net * 0.2))
        originality_delta = max(-0.15, min(0.15, net * 0.1))
        timing_delta = max(-0.2, min(0.2, net * 0.15))

        adjusted["quality_score"] = SourceLearningService._clamp_score(
            float(adjusted.get("quality_score") or 0.0) + quality_delta
        )
        adjusted["originality_score"] = SourceLearningService._clamp_score(
            float(adjusted.get("originality_score") or 0.0) + originality_delta
        )
        adjusted["timing_usefulness"] = SourceLearningService._clamp_score(
            float(adjusted.get("timing_usefulness") or 0.0) + timing_delta
        )

        if not_useful > useful:
            adjusted["should_promote_to_trusted"] = False
        if not_useful >= max(2, useful * 2):
            adjusted["noise_ratio"] = "noisy"
            adjusted["trust_trajectory"] = "degrading"
        elif useful >= max(2, not_useful * 2):
            adjusted["noise_ratio"] = "clean"
            adjusted["trust_trajectory"] = "improving"

        weaknesses = list(adjusted.get("known_weaknesses") or [])
        feedback_note = (
            f"User feedback observed: {useful} useful, {not_useful} not useful."
        )
        if (
            not_useful
            and "User flagged some evidence from this source as not useful."
            not in weaknesses
        ):
            weaknesses.append(
                "User flagged some evidence from this source as not useful."
            )
        adjusted["known_weaknesses"] = weaknesses
        adjusted["trust_reasoning"] = (
            f"{adjusted.get('trust_reasoning') or ''} {feedback_note}".strip()
        )
        return adjusted

    @staticmethod
    def _clamp_score(value: float) -> float:
        return round(max(0.0, min(1.0, value)), 4)

    async def _upsert_source_profiles(
        self,
        source: Source,
        evaluation: dict[str, Any],
        evidence_count: int,
    ) -> None:
        profile = (
            await self.session.execute(
                select(SourceProfile).where(SourceProfile.source_id == source.id)
            )
        ).scalar_one_or_none()
        if profile is None:
            profile = SourceProfile(
                source_id=source.id,
                specialization_domains=evaluation["specialization_domains"],
                known_weaknesses=evaluation["known_weaknesses"],
                trust_trajectory=evaluation["trust_trajectory"],
                first_tracked_at=datetime.now(UTC),
                total_claims_tracked=evidence_count,
                active_since=datetime.now(UTC),
                notes=evaluation["trust_reasoning"],
            )
            self.session.add(profile)
        else:
            profile.specialization_domains = evaluation["specialization_domains"]
            profile.known_weaknesses = evaluation["known_weaknesses"]
            profile.trust_trajectory = evaluation["trust_trajectory"]
            profile.total_claims_tracked = evidence_count
            profile.notes = evaluation["trust_reasoning"]
            profile.last_reviewed_at = datetime.now(UTC)

        trust = (
            await self.session.execute(
                select(SourceTrustProfile).where(
                    SourceTrustProfile.source_id == source.id
                )
            )
        ).scalar_one_or_none()
        if trust is None:
            trust = SourceTrustProfile(
                source_id=source.id,
                factual_reliability=evaluation["factual_reliability"],
                calibration=evaluation["calibration"],
                correction_quality=evaluation["correction_quality"],
                noise_ratio=evaluation["noise_ratio"],
                trust_trajectory=evaluation["trust_trajectory"],
            )
            self.session.add(trust)
        else:
            trust.factual_reliability = evaluation["factual_reliability"]
            trust.calibration = evaluation["calibration"]
            trust.correction_quality = evaluation["correction_quality"]
            trust.noise_ratio = evaluation["noise_ratio"]
            trust.trust_trajectory = evaluation["trust_trajectory"]
            trust.last_evaluated_at = datetime.now(UTC)

        value = (
            await self.session.execute(
                select(SourceValueProfile).where(
                    SourceValueProfile.source_id == source.id
                )
            )
        ).scalar_one_or_none()
        if value is None:
            value = SourceValueProfile(
                source_id=source.id,
                idea_generation_value=evaluation["idea_generation_value"],
                timing_value=evaluation["timing_value"],
                portfolio_relevance_value=evaluation["portfolio_relevance_value"],
                specificity=evaluation["specificity"],
                originality=evaluation["originality"],
                best_domains=evaluation["specialization_domains"],
            )
            self.session.add(value)
        else:
            value.idea_generation_value = evaluation["idea_generation_value"]
            value.timing_value = evaluation["timing_value"]
            value.portfolio_relevance_value = evaluation["portfolio_relevance_value"]
            value.specificity = evaluation["specificity"]
            value.originality = evaluation["originality"]
            value.best_domains = evaluation["specialization_domains"]
            value.last_evaluated_at = datetime.now(UTC)

    async def _upsert_quality_segments(
        self,
        source: Source,
        evaluation: dict[str, Any],
        subject_name: str | None,
        subject_type: str | None,
        evidence_count: int,
    ) -> None:
        await self.session.execute(
            delete(SourceQualitySegment).where(
                SourceQualitySegment.source_id == source.id
            )
        )
        self.session.add(
            SourceQualitySegment(
                source_id=source.id,
                domain=self._domain(source.url),
                ticker=subject_name if subject_type == "entity" else None,
                quality_score=float(evaluation["quality_score"]),
                originality_score=float(evaluation["originality_score"]),
                timing_usefulness=float(evaluation["timing_usefulness"]),
                notes=evaluation["trust_reasoning"],
                evidence_count=evidence_count,
                last_evaluated=datetime.now(UTC),
            )
        )

    def _display_name(self, url: str | None, title: str | None) -> str:
        domain = self._domain(url)
        if title and " - " in title:
            prefix = title.split(" - ", 1)[0].strip()
            if len(prefix) >= 4:
                return prefix[:120]
        if domain:
            root = domain.replace("www.", "")
            return root[:120]
        return (title or "Auto-discovered source")[:120]

    def _domain(self, url: str | None) -> str | None:
        if not url:
            return None
        try:
            return urlparse(url).netloc.lower() or None
        except Exception as exc:
            import logging

            logging.getLogger(__name__).exception("Masked failure caught")
            return None

    def _infer_source_type(self, url: str | None, preferred_type: str) -> str:
        domain = self._domain(url) or ""
        if "youtube.com" in domain or "youtu.be" in domain:
            return "youtube"
        if "x.com" in domain or "twitter.com" in domain:
            return "x_account"
        if "sec.gov" in domain:
            return "official"
        return preferred_type
