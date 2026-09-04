from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import delete, desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from investos.models.catalog import SourceClaimRecord
from investos.models.conclusion import ConclusionState
from investos.models.coverage import (
    CoverageMap,
    MissingEvidenceClass,
    UnresolvedQuestion,
)
from investos.models.entity import Entity, Security
from investos.models.evidence import RawEvidence
from investos.models.knowledge import Claim, Event, Fact
from investos.models.portfolio import Position
from investos.models.review import ReviewQueueItem
from investos.models.shadow import ExperimentResult, ShadowExperiment
from investos.models.source import Source
from investos.models.theme import Theme
from investos.schemas.review import ReviewQueueItemResponse
from investos.services.artifact_hygiene import (
    is_artifact_question_text,
    is_artifact_subject_name,
)
from investos.services.canonical_state import CanonicalStateService
from investos.services.ownership_signals import OwnershipSignalService
from investos.services.source_claim_policy import (
    days_between,
    source_claim_due_at,
    source_claim_priority,
)

SOURCE_CLAIM_REVIEW_LIMIT = 75


class ReviewService:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def list_queue(self) -> list[ReviewQueueItemResponse]:
        items = (
            (
                await self.session.execute(
                    select(ReviewQueueItem)
                    .where(ReviewQueueItem.status.in_(["pending", "in_review"]))
                    .order_by(
                        desc(ReviewQueueItem.priority_score),
                        desc(ReviewQueueItem.created_at),
                    )
                    .limit(100)
                )
            )
            .scalars()
            .all()
        )
        return [await self._serialize(item) for item in items]

    async def refresh_queue(self) -> list[ReviewQueueItemResponse]:
        await self.session.execute(
            delete(ReviewQueueItem).where(
                ReviewQueueItem.status.in_(["pending", "in_review"])
            )
        )
        await self.session.flush()

        await self._queue_unresolved_questions()
        await self._queue_holdings_needing_review()
        await self._queue_source_claim_assessments()
        await self._queue_ownership_signals()
        await self._queue_shadow_followups()

        # Deduplicate: keep only the highest-priority item per (item_type, item_id)
        await self._deduplicate_queue()

        await self.session.commit()
        return await self.list_queue()

    async def _deduplicate_queue(self) -> None:
        """Remove duplicate review items, keeping the highest-priority entry per (item_type, item_id)."""
        items = list(
            (
                await self.session.execute(
                    select(ReviewQueueItem)
                    .where(ReviewQueueItem.status.in_(["pending", "in_review"]))
                    .order_by(desc(ReviewQueueItem.priority_score))
                )
            )
            .scalars()
            .all()
        )
        seen: set[tuple[str, UUID]] = set()
        for item in items:
            key = (item.item_type, item.item_id)
            if key in seen:
                await self.session.delete(item)
            else:
                seen.add(key)

    async def _queue_unresolved_questions(self) -> None:
        # Only queue questions for portfolio-relevant subjects
        tracked_entity_ids = await self._tracked_entity_ids()
        tracked_label_keys = await self._tracked_subject_label_keys()
        rows = (
            await self.session.execute(
                select(UnresolvedQuestion, CoverageMap)
                .join(CoverageMap, UnresolvedQuestion.coverage_map_id == CoverageMap.id)
                .where(UnresolvedQuestion.status == "open")
                .order_by(
                    desc(UnresolvedQuestion.urgency),
                    desc(UnresolvedQuestion.created_at),
                )
                .limit(25)
            )
        ).all()
        for question, coverage in rows:
            if self._is_artifact_question(question.question_text):
                continue
            if await self._is_artifact_coverage(coverage):
                continue
            # Skip entities that aren't portfolio positions or tracked entities
            if (
                coverage.subject_type == "entity"
                and coverage.subject_id not in tracked_entity_ids
            ):
                continue
            if coverage.subject_type == "theme":
                theme = await self.session.get(Theme, coverage.subject_id)
                if (
                    theme is not None
                    and self._label_key(theme.name) in tracked_label_keys
                ):
                    continue
            urgency = float(question.urgency or 0)
            coverage_weakness = max(
                0.0, 10.0 - float(coverage.overall_coverage_score or 0.0)
            )
            priority = urgency * 10.0 + coverage_weakness
            self.session.add(
                ReviewQueueItem(
                    item_type=coverage.subject_type,
                    item_id=coverage.subject_id,
                    priority_score=priority,
                    size_factor=0.0,
                    evidence_change_factor=0.0,
                    contradiction_pressure=float(coverage.contradiction_count or 0),
                    thesis_drift=0.0,
                    catalyst_proximity=0.0,
                    coverage_weakness=coverage_weakness,
                    trigger_reason=f"Open research question: {question.question_text}",
                )
            )

    @staticmethod
    def _is_artifact_question(text: str | None) -> bool:
        return is_artifact_question_text(text)

    async def _is_artifact_coverage(self, coverage: CoverageMap) -> bool:
        label = ""
        if coverage.subject_type == "theme":
            theme = await self.session.get(Theme, coverage.subject_id)
            label = "" if theme is None else theme.name
        elif coverage.subject_type == "entity":
            entity = await self.session.get(Entity, coverage.subject_id)
            label = "" if entity is None else entity.name
        return is_artifact_subject_name(label)

    async def _tracked_entity_ids(self) -> set[UUID]:
        """Return entity IDs that are linked to portfolio positions."""
        rows = (
            (
                await self.session.execute(
                    select(Security.entity_id)
                    .join(Position, Position.security_id == Security.id)
                    .where(
                        Position.list_type.in_(["holding", "watchlist", "considering"])
                    )
                )
            )
            .scalars()
            .all()
        )
        return set(rows)

    @staticmethod
    def _label_key(label: str | None) -> str:
        return " ".join((label or "").casefold().replace("·", " ").split())

    async def _tracked_subject_label_keys(self) -> set[str]:
        rows = (
            await self.session.execute(
                select(Security.ticker, Entity.name)
                .join(Position, Position.security_id == Security.id)
                .join(Entity, Security.entity_id == Entity.id)
                .where(Position.list_type.in_(["holding", "watchlist", "considering"]))
            )
        ).all()
        keys: set[str] = set()
        for ticker, name in rows:
            if ticker:
                keys.add(self._label_key(str(ticker)))
            if name:
                keys.add(self._label_key(str(name)))
            if ticker and name:
                keys.add(self._label_key(f"{ticker} {name}"))
                keys.add(self._label_key(f"{ticker} · {name}"))
        return keys

    async def _queue_holdings_needing_review(self) -> None:
        canonical = CanonicalStateService(self.session)
        positions = (
            (
                await self.session.execute(
                    select(Position)
                    .where(Position.list_type == "holding")
                    .order_by(desc(Position.market_value))
                    .limit(25)
                )
            )
            .scalars()
            .all()
        )
        for position in positions:
            conclusion = await canonical.get_conclusion_state(
                subject_type="position",
                subject_id=position.id,
            )
            if (
                conclusion
                and conclusion.current_stance not in {"no_view", "uncertain"}
                and conclusion.confidence_band not in {"very_low", "low"}
            ):
                continue
            coverage = await canonical.get_coverage_map(
                subject_type="position",
                subject_id=position.id,
            )
            size_factor = min(100.0, float(position.market_value or 0.0) / 1000.0)
            contradiction_pressure = (
                0.0 if coverage is None else float(coverage.contradiction_count or 0)
            )
            coverage_weakness = (
                10.0
                if coverage is None
                else max(0.0, 10.0 - float(coverage.overall_coverage_score or 0.0))
            )
            priority = (
                25.0 + size_factor + (coverage_weakness * 2.0) + contradiction_pressure
            )
            trigger = "Holding lacks a strong accepted state."
            if conclusion is not None:
                trigger = f"Holding stance is {conclusion.current_stance} at {conclusion.confidence_band} confidence."
            self.session.add(
                ReviewQueueItem(
                    item_type="position",
                    item_id=position.id,
                    priority_score=priority,
                    size_factor=size_factor,
                    evidence_change_factor=0.0,
                    contradiction_pressure=contradiction_pressure,
                    thesis_drift=0.0,
                    catalyst_proximity=0.0,
                    coverage_weakness=coverage_weakness,
                    trigger_reason=trigger,
                )
            )

    async def _queue_shadow_followups(self) -> None:
        cutoff = datetime.now(UTC) - timedelta(days=7)
        opportunity_rows = (
            (
                await self.session.execute(
                    select(ShadowExperiment)
                    .where(ShadowExperiment.created_at >= cutoff)
                    .order_by(desc(ShadowExperiment.created_at))
                    .limit(20)
                )
            )
            .scalars()
            .all()
        )
        for experiment in opportunity_rows:
            context = (experiment.initial_portfolio_state_json or {}).get(
                "experiment_context"
            ) or {}
            profile = context.get("discovery_profile") or {}
            if (
                context.get("trigger_type") != "autonomous_discovery"
                or not profile.get("captured_at")
                or not profile.get("evidence_refs")
                or experiment.run_status in {"failed", "skipped"}
            ):
                continue
            priority = max(0.0, min(1.0, float(profile.get("priority_score") or 0.0)))
            self.session.add(
                ReviewQueueItem(
                    item_type="shadow_experiment",
                    item_id=experiment.id,
                    priority_score=40.0 + (priority * 50.0),
                    size_factor=0.0,
                    evidence_change_factor=priority * 10.0,
                    contradiction_pressure=0.0,
                    thesis_drift=0.0,
                    catalyst_proximity=priority * 10.0,
                    coverage_weakness=0.0,
                    trigger_reason=(
                        "Evidence-linked shadow opportunity: "
                        + str(
                            profile.get("why_now")
                            or profile.get("investable_thesis")
                            or experiment.name
                        )
                    ),
                )
            )

        rows = (
            await self.session.execute(
                select(ShadowExperiment, ExperimentResult)
                .join(
                    ExperimentResult,
                    ExperimentResult.experiment_id == ShadowExperiment.id,
                )
                .where(
                    ShadowExperiment.completed_at.is_not(None),
                    ShadowExperiment.completed_at >= cutoff,
                )
                .order_by(desc(ShadowExperiment.completed_at))
                .limit(15)
            )
        ).all()
        for experiment, result in rows:
            alpha = abs(float(result.alpha or 0.0))
            if alpha < 0.03:
                continue
            self.session.add(
                ReviewQueueItem(
                    item_type="shadow_experiment",
                    item_id=experiment.id,
                    priority_score=30.0 + (alpha * 100.0),
                    size_factor=0.0,
                    evidence_change_factor=0.0,
                    contradiction_pressure=0.0,
                    thesis_drift=alpha * 10.0,
                    catalyst_proximity=0.0,
                    coverage_weakness=0.0,
                    trigger_reason=(
                        f"Shadow experiment produced material divergence versus actual portfolio. "
                        f"alpha={alpha:.2%}"
                    ),
                    reasoning_run_id=result.reasoning_run_id,
                )
            )

    async def _queue_source_claim_assessments(self) -> None:
        now = datetime.now(UTC)
        rows = (
            await self.session.execute(
                select(SourceClaimRecord, Claim, Source)
                .join(Claim, SourceClaimRecord.claim_id == Claim.id)
                .join(Source, SourceClaimRecord.source_id == Source.id)
                .where(
                    SourceClaimRecord.assessment == "pending",
                    Claim.is_deprecated.is_(False),
                )
                .order_by(SourceClaimRecord.claim_time)
                .limit(SOURCE_CLAIM_REVIEW_LIMIT)
            )
        ).all()
        for record, claim, source in rows:
            due_at = self._source_claim_due_at(record, claim)
            if due_at is None or due_at > now:
                continue
            overdue_days = self._days_between(due_at, now)
            priority = self._source_claim_priority(record, claim, due_at, now)
            self.session.add(
                ReviewQueueItem(
                    item_type="source_claim_record",
                    item_id=record.id,
                    priority_score=priority,
                    size_factor=0.0,
                    evidence_change_factor=min(20.0, overdue_days),
                    contradiction_pressure=0.0,
                    thesis_drift=0.0,
                    catalyst_proximity=0.0,
                    coverage_weakness=0.0,
                    trigger_reason=(
                        "Pending source claim due for outcome assessment: "
                        f"{self._shorten(claim.statement, 160)}"
                    ),
                )
            )

    async def _queue_ownership_signals(self) -> None:
        await OwnershipSignalService(self.session).queue_review_items()

    async def _serialize(self, item: ReviewQueueItem) -> ReviewQueueItemResponse:
        label = await self._label(item.item_type, item.item_id)
        ctx = await self._review_context(item, label)
        return ReviewQueueItemResponse(
            id=item.id,
            item_type=item.item_type,
            item_id=item.item_id,
            item_label=label,
            priority_score=float(item.priority_score),
            status=item.status,
            trigger_reason=item.trigger_reason,
            why_now_summary=self._why_now_summary(item, ctx),
            next_action=self._next_action(item, ctx),
            signal_tags=self._signal_tags(item, ctx),
            size_factor=float(item.size_factor),
            evidence_change_factor=float(item.evidence_change_factor),
            contradiction_pressure=float(item.contradiction_pressure),
            thesis_drift=float(item.thesis_drift),
            catalyst_proximity=float(item.catalyst_proximity),
            coverage_weakness=float(item.coverage_weakness),
            reasoning_run_id=item.reasoning_run_id,
            created_at=item.created_at,
        )

    @staticmethod
    def _age_phrase(dt: datetime | None) -> str:
        """Render a timestamp as a dated, human-relative phrase."""
        if dt is None:
            return "no date on record"
        d = dt if dt.tzinfo else dt.replace(tzinfo=UTC)
        days = (datetime.now(UTC) - d).days
        stamp = d.strftime("%b %-d, %Y")
        if days <= 0:
            return f"today ({stamp})"
        if days == 1:
            return f"1 day ago ({stamp})"
        return f"{days} days ago ({stamp})"

    @staticmethod
    def _as_utc(dt: datetime | None) -> datetime | None:
        if dt is None:
            return None
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)

    @staticmethod
    def _days_between(start: datetime | None, end: datetime | None) -> float:
        return days_between(start, end)

    @staticmethod
    def _source_claim_due_at(
        record: SourceClaimRecord, claim: Claim | None = None
    ) -> datetime | None:
        return source_claim_due_at(record, claim)

    @staticmethod
    def _source_claim_priority(
        record: SourceClaimRecord,
        claim: Claim | None,
        due_at: datetime | None,
        now: datetime | None = None,
    ) -> float:
        return source_claim_priority(record, claim, due_at, now)

    async def _review_context(self, item: ReviewQueueItem, label: str) -> dict:
        """Gather the concrete facts an explanation should cite for this item."""
        ctx: dict = {"label": label}
        if item.item_type == "source_claim_record":
            return await self._source_claim_review_context(item.item_id, ctx)
        if item.item_type == "raw_evidence":
            return await self._raw_evidence_review_context(item.item_id, ctx)
        if item.item_type == "shadow_experiment":
            experiment = await self.session.get(ShadowExperiment, item.item_id)
            if experiment is not None:
                context = (experiment.initial_portfolio_state_json or {}).get(
                    "experiment_context"
                ) or {}
                ctx["shadow_status"] = experiment.run_status
                ctx["opportunity_profile"] = context.get("discovery_profile") or {}
            return ctx
        if item.item_type != "position":
            return ctx

        pos = (
            await self.session.execute(
                select(Position).where(Position.id == item.item_id)
            )
        ).scalar_one_or_none()
        if pos is None:
            return ctx
        ctx["weight_pct"] = float(pos.weight_pct or 0.0)
        ctx["market_value"] = float(pos.market_value or 0.0)
        ctx["quantity"] = float(pos.quantity or 0.0)

        sec = (
            await self.session.execute(
                select(Security).where(Security.id == pos.security_id)
            )
        ).scalar_one_or_none()
        entity_id = getattr(sec, "entity_id", None)
        subjects = [("position", item.item_id)]
        if entity_id is not None:
            subjects.append(("entity", entity_id))

        for stype, sid in subjects:
            conc = (
                await self.session.execute(
                    select(ConclusionState)
                    .where(
                        ConclusionState.subject_type == stype,
                        ConclusionState.subject_id == sid,
                    )
                    .order_by(desc(ConclusionState.last_updated_at))
                    .limit(1)
                )
            ).scalar_one_or_none()
            if conc is not None:
                ctx["stance"] = conc.current_stance
                ctx["confidence"] = conc.confidence_band
                ctx["thesis_summary"] = conc.current_thesis_summary
                ctx["conclusion_updated"] = conc.last_updated_at
                ctx["conclusion_verified"] = conc.last_verified_at
                ctx["falsifiers"] = list(conc.what_would_falsify or [])
                ctx["top_support"] = await self._first_resolved(
                    conc.key_supporting_evidence_ids
                )
                ctx["top_contra"] = await self._first_resolved(
                    conc.key_contradicting_evidence_ids
                )
                break

        for stype, sid in subjects:
            cov = (
                await self.session.execute(
                    select(CoverageMap)
                    .where(
                        CoverageMap.subject_type == stype, CoverageMap.subject_id == sid
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            if cov is not None:
                ctx["high_tier"] = int(cov.high_tier_evidence_count or 0)
                ctx["total_ev"] = int(cov.total_evidence_count or 0)
                ctx["unresolved_contra"] = int(cov.unresolved_contradiction_count or 0)
                ctx["coverage_score"] = float(cov.overall_coverage_score or 0.0)
                ctx["coverage_computed"] = cov.last_computed_at
                missing = (
                    (
                        await self.session.execute(
                            select(MissingEvidenceClass.class_name).where(
                                MissingEvidenceClass.coverage_map_id == cov.id,
                                MissingEvidenceClass.resolved_at.is_(None),
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                ctx["missing_classes"] = [m.replace("_", " ") for m in missing]
                question = (
                    await self.session.execute(
                        select(UnresolvedQuestion)
                        .where(
                            UnresolvedQuestion.coverage_map_id == cov.id,
                            UnresolvedQuestion.status == "open",
                        )
                        .order_by(UnresolvedQuestion.created_at)
                        .limit(1)
                    )
                ).scalar_one_or_none()
                if question is not None:
                    ctx["open_question"] = {
                        "text": question.question_text,
                        "since": question.created_at,
                    }
                break
        return ctx

    async def _raw_evidence_review_context(self, item_id: UUID, ctx: dict) -> dict:
        raw = await self.session.get(RawEvidence, item_id)
        if raw is None:
            return ctx
        source = await self.session.get(Source, raw.source_id)
        signal_service = OwnershipSignalService(self.session)
        signal = signal_service.analyze_signal(
            raw,
            source,
            portfolio_weights=await signal_service.portfolio_weights_by_ticker(),
        )
        if signal is not None:
            ctx["ownership_signal"] = signal.to_context()
        return ctx

    async def _source_claim_review_context(self, item_id: UUID, ctx: dict) -> dict:
        record = await self.session.get(SourceClaimRecord, item_id)
        if record is None:
            return ctx
        claim = await self.session.get(Claim, record.claim_id)
        source = await self.session.get(Source, record.source_id)
        due_at = self._source_claim_due_at(record, claim)
        ctx.update(
            {
                "source_name": getattr(source, "name", None),
                "source_type": getattr(source, "source_type", None),
                "claim_statement": getattr(claim, "statement", None),
                "claim_time": record.claim_time,
                "claim_due_at": due_at,
                "claim_assessment": record.assessment,
                "claim_horizon": getattr(claim, "target_horizon", None),
                "claim_importance": getattr(claim, "importance", None),
                "claim_ticker": record.ticker,
                "is_original_claim": bool(getattr(claim, "is_original", False)),
            }
        )
        return ctx

    @staticmethod
    def _best_date(obj) -> datetime | None:
        """Pick the most decision-relevant timestamp: when it happened, else when known."""
        for attr in ("event_time", "public_time", "ingest_time", "created_at"):
            value = getattr(obj, attr, None)
            if value is not None:
                return value
        return None

    async def _first_resolved(self, evidence_ids) -> dict | None:
        """Resolve the first usable evidence id to its actual statement + date + tier."""
        for evidence_id in evidence_ids or []:
            for model in (Fact, Claim):
                row = (
                    await self.session.execute(
                        select(model).where(
                            model.id == evidence_id,
                            model.is_deprecated.is_(False),
                        )
                    )
                ).scalar_one_or_none()
                if row is not None:
                    return {
                        "statement": row.statement,
                        "date": self._best_date(row),
                        "tier": (row.tier or "").replace("_", " "),
                    }
            event = (
                await self.session.execute(
                    select(Event).where(
                        Event.id == evidence_id,
                        Event.is_deprecated.is_(False),
                    )
                )
            ).scalar_one_or_none()
            if event is not None:
                return {
                    "statement": event.title,
                    "date": self._best_date(event),
                    "tier": "",
                }
            raw = (
                await self.session.execute(
                    select(RawEvidence).where(RawEvidence.id == evidence_id)
                )
            ).scalar_one_or_none()
            if raw is not None:
                return {
                    "statement": raw.title or "an untitled source",
                    "date": self._best_date(raw),
                    "tier": "",
                }
        return None

    @staticmethod
    def _shorten(text: str | None, max_chars: int) -> str:
        collapsed = " ".join((text or "").split())
        if len(collapsed) <= max_chars:
            return collapsed
        return collapsed[: max_chars - 1].rsplit(" ", 1)[0] + "…"

    @staticmethod
    def _join_phrases(items: list[str]) -> str:
        items = [i for i in items if i]
        if not items:
            return ""
        if len(items) == 1:
            return items[0]
        return ", ".join(items[:-1]) + " and " + items[-1]

    @staticmethod
    def _size_clause(ctx: dict) -> str:
        weight = ctx.get("weight_pct")
        value = ctx.get("market_value")
        if weight:
            if value:
                return f"a {weight:.0f}% position (${value:,.0f})"
            return f"a {weight:.0f}% position"
        if value:
            return f"a ${value:,.0f} holding"
        return "a tracked holding"

    @staticmethod
    def _support_phrase(ctx: dict) -> str:
        """What concrete evidence the thesis rests on, with the strongest fact dated."""
        ht = ctx.get("high_tier")
        tot = ctx.get("total_ev")
        lead = ""
        if ht is not None:
            lead = f"rests on {ht} hard fact{'s' if ht != 1 else ''}"
            if tot:
                lead += f" out of {tot} piece{'s' if tot != 1 else ''} of evidence"
        support = ctx.get("top_support")
        if support and support.get("statement"):
            stmt = ReviewService._shorten(support["statement"], 140)
            detail = f'the strongest being "{stmt}" ({ReviewService._age_phrase(support.get("date"))})'
            return (
                f"{lead}, {detail}"
                if lead
                else f'leans mainly on "{stmt}" ({ReviewService._age_phrase(support.get("date"))})'
            )
        return lead or "has almost no hard evidence behind it"

    @staticmethod
    def _contra_phrase(ctx: dict) -> str:
        contra = ctx.get("top_contra")
        if contra and contra.get("statement"):
            stmt = ReviewService._shorten(contra["statement"], 140)
            return f'is contradicted by "{stmt}" ({ReviewService._age_phrase(contra.get("date"))})'
        n = ctx.get("unresolved_contra", 0)
        if n:
            return f"carries {n} unresolved contradiction{'s' if n != 1 else ''}"
        return ""

    @staticmethod
    def _thesis_sentence(ctx: dict) -> str:
        """The actual accepted view, quoted, with stance/confidence and verification date."""
        summary = ctx.get("thesis_summary")
        stance = ctx.get("stance")
        if not summary or not stance:
            return (
                "There is no accepted thesis on record yet, so there is no stated reason it is held; "
                f"it {ReviewService._support_phrase(ctx)}."
            )
        conf = (ctx.get("confidence") or "").replace("_", " ")
        verified = ctx.get("conclusion_verified")
        if verified is not None:
            when = f"was last verified {ReviewService._age_phrase(verified)}"
        else:
            when = (
                f"was written {ReviewService._age_phrase(ctx.get('conclusion_updated'))} "
                "and has never been independently verified"
            )
        sentence = (
            f'The accepted {stance} thesis — "{ReviewService._shorten(summary, 170)}" — '
            f"is {conf}-confidence and {when}; it {ReviewService._support_phrase(ctx)}"
        )
        contra = ReviewService._contra_phrase(ctx)
        if contra:
            sentence += f", and it {contra}"
        return sentence + "."

    @staticmethod
    def _gap_sentence(ctx: dict) -> str:
        """Concrete coverage holes and the oldest open question, dated."""
        bits: list[str] = []
        missing = ctx.get("missing_classes") or []
        if missing:
            bits.append(
                f"nothing yet covers {ReviewService._join_phrases(missing[:3])}"
            )
        question = ctx.get("open_question")
        if question and question.get("text"):
            bits.append(
                f'an open question — "{ReviewService._shorten(question["text"], 120)}" — '
                f"has gone unanswered since {ReviewService._age_phrase(question.get('since'))}"
            )
        if not bits:
            return ""
        joined = "; ".join(bits)
        return joined[0].upper() + joined[1:] + "."

    def _why_now_summary(self, item: ReviewQueueItem, ctx: dict | None = None) -> str:
        ctx = ctx or {}
        label = ctx.get("label") or "This item"
        if item.item_type == "source_claim_record":
            source = ctx.get("source_name") or "This source"
            statement = self._shorten(ctx.get("claim_statement"), 160) or label
            ticker = ctx.get("claim_ticker")
            ticker_clause = f" on {ticker}" if ticker else ""
            due_at = ctx.get("claim_due_at")
            return (
                f'{source} made the claim "{statement}"{ticker_clause} '
                f"{self._age_phrase(ctx.get('claim_time'))}. It became due for outcome assessment "
                f"{self._age_phrase(due_at)}; until it is marked correct, partially correct, "
                "incorrect, or indeterminate, source reliability and trust trajectory stay blind to the result."
            )
        if item.item_type == "raw_evidence" and ctx.get("ownership_signal"):
            signal = ctx["ownership_signal"]
            subject = signal.get("ticker") or signal.get("issuer") or label
            actor = signal.get("actor_name") or "the disclosed actor"
            lag = signal.get("disclosure_lag_days")
            lag_clause = (
                "with unknown disclosure lag"
                if lag is None
                else f"disclosed about {float(lag):.0f} day{'s' if round(float(lag)) != 1 else ''} after the event"
            )
            value = signal.get("transaction_value")
            value_clause = f" around ${float(value):,.0f}" if value else ""
            portfolio = ""
            if signal.get("is_portfolio_linked"):
                weight = signal.get("portfolio_weight_pct")
                portfolio = f" It matters because {subject} is portfolio-linked"
                if weight:
                    portfolio += f" at roughly {float(weight):.0f}% weight"
                portfolio += ", but the disclosure is still only a trigger until its source, timing, mechanism, and later outcome are checked."
            return (
                f"{label} is a {signal.get('source_kind')} on {subject}: {actor} "
                f"{signal.get('direction') or 'changed exposure'}{value_clause}, {lag_clause}."
                f"{portfolio}"
            )
        if item.item_type == "position":
            sentence = (
                f"{label} is {self._size_clause(ctx)}. {self._thesis_sentence(ctx)}"
            )
            gap = self._gap_sentence(ctx)
            if gap:
                sentence += f" {gap}"
            return sentence
        if item.item_type == "shadow_experiment":
            profile = ctx.get("opportunity_profile") or {}
            if profile:
                stage = str(profile.get("signal_stage") or "unclassified").replace(
                    "_", " "
                )
                priced = str(
                    profile.get("priced_in_assessment") or "uncertain"
                ).replace("_", " ")
                return (
                    f'The evidence-linked shadow candidate "{label}" is at the {stage} stage '
                    f"with a {priced} priced-in assessment. {profile.get('why_now') or profile.get('investable_thesis')}"
                )
            alpha = abs(float(item.thesis_drift or 0.0)) / 10.0
            return (
                f'The shadow run "{label}" diverged {alpha:.1%} from the real book '
                f"(flagged {self._age_phrase(item.created_at)}); the gap is worth a lesson."
            )
        if item.trigger_reason.startswith("Open research question:"):
            q = item.trigger_reason.split(":", 1)[1].strip()
            return (
                f'Open since {self._age_phrase(item.created_at)} and still unanswered: "{q}" — '
                "the system treats this as decision-relevant and is missing the answer."
            )
        if float(item.thesis_drift or 0.0) > 0:
            return (
                f"The stored thesis for {label} is drifting from the latest evidence "
                f"(flagged {self._age_phrase(item.created_at)}) and should be re-checked."
            )
        return (
            f"{label} is flagged because its current knowledge is not strong or stable enough "
            f"yet (queued {self._age_phrase(item.created_at)})."
        )

    def _next_action(self, item: ReviewQueueItem, ctx: dict | None = None) -> str:
        ctx = ctx or {}
        if item.item_type == "source_claim_record":
            return (
                "Find direct follow-up evidence, assess the claim as correct, partially correct, "
                "incorrect, or indeterminate, then let the source performance history recompute."
            )
        if item.item_type == "raw_evidence" and ctx.get("ownership_signal"):
            return ctx["ownership_signal"].get(
                "next_test",
                "Verify the disclosure, map it to a concrete portfolio mechanism, then score the later outcome before using it in source trust.",
            )
        if item.item_type == "position":
            missing = ctx.get("missing_classes") or []
            question = ctx.get("open_question") or {}
            contra = ctx.get("top_contra") or {}
            if contra.get("statement"):
                return (
                    f'Reconcile "{self._shorten(contra["statement"], 100)}" against the thesis, then '
                    "decide whether to narrow, change, or hold the view."
                )
            if float(item.coverage_weakness or 0.0) >= 8.0:
                if question.get("text"):
                    return (
                        f'Answer "{self._shorten(question["text"], 100)}", then promote the strongest '
                        "resulting fact into the accepted state."
                    )
                if missing:
                    return (
                        f"Pull {self._join_phrases(missing[:3])}, then promote the strongest "
                        "evidence into the accepted state."
                    )
                return "Run deeper research on this holding and promote the strongest evidence into the accepted state."
            if question.get("text"):
                return f'Research and answer "{self._shorten(question["text"], 100)}" to firm up the thesis.'
            if missing:
                return f"Pull {self._join_phrases(missing[:2])} to firm up the thesis."
            return "Open the profile, inspect the strongest missing evidence, and firm up the thesis."
        if item.item_type == "shadow_experiment":
            if ctx.get("opportunity_profile") or {}:
                return (
                    "Inspect the cited point-in-time evidence, priced-in assessment, confirmation gap, "
                    "and falsifiers; then supervise the paper trade without changing the real book."
                )
            return "Compare what the shadow expected against what the real portfolio did, then extract a lesson."
        if item.trigger_reason.startswith("Open research question:"):
            return "Run targeted research to answer the question, or lower its importance if it is no longer decision-relevant."
        return "Inspect the connected evidence and decide whether to research more, verify contradictions, or revise the view."

    def _signal_tags(self, item: ReviewQueueItem, ctx: dict | None = None) -> list[str]:
        ctx = ctx or {}
        tags: list[str] = []
        if item.item_type == "source_claim_record":
            tags.append("source outcome")
            if ctx.get("claim_horizon"):
                tags.append(str(ctx["claim_horizon"]).replace("_", " "))
            if ctx.get("claim_ticker"):
                tags.append(str(ctx["claim_ticker"]))
            if ctx.get("is_original_claim"):
                tags.append("original claim")
            return tags[:4]
        if item.item_type == "raw_evidence" and ctx.get("ownership_signal"):
            signal = ctx["ownership_signal"]
            tags.append(str(signal.get("source_kind") or "disclosure"))
            if signal.get("ticker"):
                tags.append(str(signal["ticker"]))
            if signal.get("direction"):
                tags.append(str(signal["direction"]))
            lag = signal.get("disclosure_lag_days")
            if lag is not None:
                tags.append(f"{float(lag):.0f}d lag")
            return tags[:4]
        if item.item_type == "position":
            weight = ctx.get("weight_pct")
            tags.append(f"{weight:.0f}% holding" if weight else "holding")
        if item.item_type == "shadow_experiment":
            profile = ctx.get("opportunity_profile") or {}
            if profile:
                tags.append("shadow opportunity")
                if profile.get("signal_stage"):
                    tags.append(str(profile["signal_stage"]).replace("_", " "))
            else:
                tags.append("shadow divergence")
        if item.trigger_reason.startswith("Open research question:"):
            tags.append("research gap")
        if not ctx.get("stance") and item.item_type == "position":
            tags.append("no accepted view")
        if float(item.coverage_weakness or 0.0) >= 8.0:
            tags.append("thin coverage")
        elif float(item.coverage_weakness or 0.0) >= 4.0:
            tags.append("coverage pressure")
        if float(item.contradiction_pressure or 0.0) > 0:
            n = ctx.get("unresolved_contra", 0)
            tags.append(f"{n} contradictions" if n else "contradictions")
        if float(item.thesis_drift or 0.0) > 0:
            tags.append("thesis drift")
        if float(item.catalyst_proximity or 0.0) > 0:
            tags.append("catalyst nearby")
        return tags[:4]

    async def _label(self, item_type: str, item_id: UUID) -> str:
        if item_type == "source_claim_record":
            record = await self.session.get(SourceClaimRecord, item_id)
            if record is not None:
                claim = await self.session.get(Claim, record.claim_id)
                source = await self.session.get(Source, record.source_id)
                statement = self._shorten(getattr(claim, "statement", None), 90)
                source_name = getattr(source, "name", None) or "Source claim"
                return source_name if not statement else f"{source_name}: {statement}"
        if item_type == "raw_evidence":
            raw = await self.session.get(RawEvidence, item_id)
            if raw is not None:
                return raw.title or "Raw evidence"
        if item_type == "position":
            position = (
                await self.session.execute(
                    select(Position).where(Position.id == item_id)
                )
            ).scalar_one_or_none()
            if position is not None:
                security = (
                    await self.session.execute(
                        select(Security).where(Security.id == position.security_id)
                    )
                ).scalar_one_or_none()
                if security is not None:
                    entity = (
                        await self.session.execute(
                            select(Entity).where(Entity.id == security.entity_id)
                        )
                    ).scalar_one_or_none()
                    return (
                        security.ticker
                        if entity is None
                        else f"{security.ticker} · {entity.name}"
                    )
        if item_type in {"entity", "theme"}:
            if item_type == "entity":
                entity = (
                    await self.session.execute(
                        select(Entity).where(Entity.id == item_id)
                    )
                ).scalar_one_or_none()
                if entity is not None:
                    return entity.name
            if item_type == "theme":
                theme = (
                    await self.session.execute(select(Theme).where(Theme.id == item_id))
                ).scalar_one_or_none()
                if theme is not None:
                    return theme.name
        if item_type == "shadow_experiment":
            experiment = (
                await self.session.execute(
                    select(ShadowExperiment).where(ShadowExperiment.id == item_id)
                )
            ).scalar_one_or_none()
            if experiment is not None:
                return experiment.name
        return str(item_id)
