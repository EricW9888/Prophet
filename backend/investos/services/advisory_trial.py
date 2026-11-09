"""Collect a bounded forward paper trial alongside ordinary risk captures."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import desc, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from investos.models.evidence import RawEvidence, SourceItem
from investos.models.graph import Edge
from investos.models.knowledge import Claim
from investos.models.quant import ScenarioAnalysis
from investos.services.advisory_policy import (
    decide,
    declared_policy,
    session_bars,
    settle,
)
from investos.services.portfolio_risk import clock, digest, freeze_packet
from investos.services.runtime_settings import RuntimeSettingsStore

TRIAL_NAME = "Portfolio advisory paper trial (v1)"
SOURCE_ROW_LIMIT = 200


def case_name(trial_id: UUID) -> str:
    return f"{TRIAL_NAME} / {trial_id}"


class AdvisoryTrialService:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def latest(self) -> ScenarioAnalysis | None:
        return await self.session.scalar(
            select(ScenarioAnalysis)
            .where(ScenarioAnalysis.name == TRIAL_NAME)
            .order_by(desc(ScenarioAnalysis.computed_at), desc(ScenarioAnalysis.id))
            .limit(1)
        )

    @staticmethod
    def phase(trial: ScenarioAnalysis, now: datetime) -> str:
        if trial.portfolio_impact_json.get("stopped_at"):
            return "stopped"
        declaration = trial.shock_parameters_json["declaration"]
        if now < clock(declaration["collect_until"]):
            return "collecting"
        if now < clock(declaration["score_until"]):
            return "settling"
        return "finished"

    async def active(self) -> ScenarioAnalysis | None:
        trial = await self.latest()
        if trial and self.phase(trial, datetime.now(UTC)) in {"collecting", "settling"}:
            return trial
        return None

    @staticmethod
    def benchmark_ticker() -> str:
        return (
            RuntimeSettingsStore.load()
            .portfolio.default_benchmark_ticker.strip()
            .upper()
        )

    async def start(self) -> dict:
        await self.session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:name))"), {"name": TRIAL_NAME}
        )
        if await self.active() is None:
            ticker = self.benchmark_ticker()
            if not ticker:
                raise ValueError(
                    "Choose a default benchmark before starting the trial."
                )
            policy = declared_policy(ticker)
            now = datetime.now(UTC)
            end = now + timedelta(days=policy["duration_days"])
            declaration = {
                "declared_at": now.isoformat(),
                "collect_until": end.isoformat(),
                "score_until": (
                    end + timedelta(days=policy["outcome_grace_days"])
                ).isoformat(),
                "policy": policy,
            }
            self.session.add(
                ScenarioAnalysis(
                    name=TRIAL_NAME,
                    scenario_description="Forward paper advisory comparison.",
                    shock_parameters_json={
                        "declaration": declaration,
                        "declaration_hash": digest(declaration),
                    },
                    portfolio_impact_json={},
                    total_portfolio_impact=0,
                    computed_at=now,
                )
            )
        await self.session.commit()
        return await self.state()

    async def stop(self, trial_id: UUID) -> dict:
        trial = await self._locked(trial_id)
        now = datetime.now(UTC)
        if trial and self.phase(trial, now) in {"collecting", "settling"}:
            trial.portfolio_impact_json = {
                **trial.portfolio_impact_json,
                "stopped_at": now.isoformat(),
            }
        await self.session.commit()
        return await self.state()

    async def _locked(self, trial_id: UUID) -> ScenarioAnalysis | None:
        return await self.session.scalar(
            select(ScenarioAnalysis)
            .where(ScenarioAnalysis.id == trial_id, ScenarioAnalysis.name == TRIAL_NAME)
            .execution_options(populate_existing=True)
            .with_for_update()
        )

    async def _case_summaries(self, trial_id: UUID):
        # Fetch full source/tape receipts only for a requested detail or an
        # outcome that is ready to settle.
        receipt = ScenarioAnalysis.shock_parameters_json["receipt"]
        return (
            await self.session.execute(
                select(
                    ScenarioAnalysis.id,
                    ScenarioAnalysis.computed_at,
                    ScenarioAnalysis.shock_parameters_json["receipt_hash"].astext.label(
                        "receipt_hash"
                    ),
                    receipt["decision"].label("decision"),
                    receipt["evidence"].label("evidence"),
                    ScenarioAnalysis.portfolio_impact_json["outcome"]
                    .op("-")("marks")
                    .label("outcome"),
                )
                .where(ScenarioAnalysis.name == case_name(trial_id))
                .order_by(ScenarioAnalysis.computed_at, ScenarioAnalysis.id)
            )
        ).all()

    async def state(self) -> dict:
        trial = await self.latest()
        if trial is None:
            return {"trial": None, "protocol": declared_policy(self.benchmark_ticker())}
        now = datetime.now(UTC)
        phase = self.phase(trial, now)
        observations = []
        for row in await self._case_summaries(trial.id):
            observations.append(
                {
                    "id": str(row.id),
                    "receipt_hash": row.receipt_hash,
                    "detected_at": row.computed_at.isoformat(),
                    "decision": row.decision,
                    "evidence": row.evidence,
                    "outcome": row.outcome,
                }
            )
        scored = [
            case["outcome"]
            for case in observations
            if case["outcome"] and case["outcome"]["status"] == "scored"
        ]
        comparisons = {}
        for policy in ("candidate", "benchmark"):
            comparisons[policy] = {
                "alerts": sum(
                    row["decision"][f"{policy}_alert"] for row in observations
                ),
                "paper_responses": sum(row[policy]["acted"] for row in scored),
                "missed_warnings": sum(row[policy]["missed_warning"] for row in scored),
                "unnecessary_alerts": sum(
                    row[policy]["unnecessary_alert"] for row in scored
                ),
                "mean_advantage_bps": (
                    round(
                        sum(row[policy]["advantage_bps"] for row in scored)
                        / len(scored),
                        2,
                    )
                    if scored
                    else None
                ),
                "lost_upside_bps": round(
                    sum(row[policy]["lost_upside_bps"] for row in scored), 2
                ),
                "turnover_pct": round(
                    sum(row[policy]["turnover_pct"] for row in scored), 2
                ),
            }
        return {
            "protocol": declared_policy(self.benchmark_ticker()),
            "trial": {
                "id": str(trial.id),
                "phase": phase,
                **trial.shock_parameters_json,
                **trial.portfolio_impact_json,
                "observations": observations,
                "scored_sessions": len(scored),
                "adverse_sessions": sum(row["adverse"] for row in scored),
                "comparisons": comparisons,
            },
        }

    async def receipt(self, observation_id: UUID) -> dict | None:
        row = await self.session.get(ScenarioAnalysis, observation_id)
        if row is None or not row.name.startswith(f"{TRIAL_NAME} / "):
            return None
        return {**row.shock_parameters_json, **row.portfolio_impact_json}

    async def observe(
        self, trial_id: UUID, packet: dict, benchmark_snapshot: dict
    ) -> None:
        trial = await self._locked(trial_id)
        now = datetime.now(UTC)
        if trial is None or self.phase(trial, now) not in {"collecting", "settling"}:
            await self.session.commit()
            return
        declaration = trial.shock_parameters_json["declaration"]
        policy = declaration["policy"]
        cutoff = clock(packet["cutoff"])
        if cutoff < clock(declaration["declared_at"]):
            await self.session.commit()
            return
        packet = freeze_packet(packet)
        benchmark_ticker = policy["benchmark_ticker"]
        benchmark = freeze_packet(
            {**packet, "snapshots": {benchmark_ticker: benchmark_snapshot}}
        )["snapshots"][benchmark_ticker]
        bars = (
            session_bars(benchmark)
            if benchmark.get("reported_symbol") == benchmark_ticker
            and benchmark.get("currency") == "USD"
            else {}
        )
        days = sorted(bars)
        cases = await self._case_summaries(trial.id)
        previous_sessions = set()
        alerted_sources = set()
        for summary in cases:
            session = summary.decision["session"]
            previous_sessions.add(session)
            source_hash = summary.decision["source_hash"]
            if source_hash:
                alerted_sources.add(source_hash)
            next_day = next((day for day in days if day > session), None)
            if next_day is None or summary.outcome is not None:
                continue
            case = await self.session.get(ScenarioAnalysis, summary.id)
            receipt = case.shock_parameters_json["receipt"]
            outcome = settle(receipt, packet, bars[next_day], policy)
            outcome["captured_at"] = packet["cutoff"]
            case.portfolio_impact_json = {
                "outcome": outcome,
                "outcome_hash": digest(outcome),
            }
        latest_day = days[-1] if days else None
        reason = "No completed benchmark session was available."
        opening = bars.get(latest_day, {}).get("session_open_at")
        collect_until = clock(declaration["collect_until"])
        if latest_day in previous_sessions:
            reason = "The current session already has a frozen observation."
        elif not opening:
            reason = (
                "A verified regular session opening is needed to date the observation."
            )
        elif clock(opening) < clock(declaration["declared_at"]):
            reason = "Waiting for a session that opened after the trial was declared."
        elif now >= collect_until:
            reason = "The observation window ended. Pending outcomes can still settle."
        else:
            sources, truncated = await self._sources(packet, policy)
            detected_at = datetime.now(UTC)
            decision = decide(packet, benchmark, sources, policy, alerted_sources)
            if truncated:
                decision.update(
                    status="unavailable",
                    candidate_alert=False,
                    source_hash=None,
                    weak_weight_pct=None,
                    affected_holdings=[],
                    reason="The bounded source scan exceeded its coverage limit.",
                )
            source = next(
                (
                    item
                    for item in sources
                    if item["content_hash"] == decision["source_hash"]
                ),
                None,
            )
            receipt = {
                "version": "prospective-advisory-observation-v1",
                "trial_id": str(trial.id),
                "declaration": declaration,
                "declaration_hash": trial.shock_parameters_json["declaration_hash"],
                "prior_alert_source_hashes": sorted(alerted_sources),
                "detected_at": detected_at.isoformat(),
                "delivery": {"status": "paper_only", "delivered_at": None},
                "market_packet": packet,
                "benchmark": benchmark,
                "sources": sources,
                "evidence": (
                    {"id": source["evidence_id"], "title": source["title"]}
                    if source
                    else None
                ),
                "source_coverage_truncated": truncated,
                "decision": decision,
            }
            self.session.add(
                ScenarioAnalysis(
                    id=uuid5(NAMESPACE_URL, f"{case_name(trial.id)}:{latest_day}"),
                    name=case_name(trial.id),
                    scenario_description="First decision capture for this benchmark session.",
                    shock_parameters_json={
                        "receipt": receipt,
                        "receipt_hash": digest(receipt),
                    },
                    portfolio_impact_json={},
                    total_portfolio_impact=0,
                    computed_at=detected_at,
                )
            )
            previous_sessions.add(latest_day)
            reason = decision["reason"]
        # A later benchmark download can identify session dates the host missed.
        # It never supplies a decision packet for those dates.
        start_day = clock(declaration["declared_at"]).date().isoformat()
        end_day = collect_until.date().isoformat()
        gaps = {
            day
            for day in days[:-1]
            if start_day < day < end_day and day not in previous_sessions
        }
        trial.portfolio_impact_json = {
            **trial.portfolio_impact_json,
            "last_checked_at": now.isoformat(),
            "last_capture_detail": reason,
            "missed_sessions": sorted(
                gaps | set(trial.portfolio_impact_json.get("missed_sessions", []))
            ),
        }
        await self.session.commit()

    async def _sources(self, packet: dict, policy: dict) -> tuple[list[dict], bool]:
        """Freeze source/claim/link versions as they actually exist at capture."""
        security_by_subject = {}
        for row in packet["positions"]:
            for kind in ("entity", "security"):
                key = (kind, UUID(row[f"{kind}_id"]))
                security_by_subject.setdefault(key, set()).add(row["security_id"])
        if not security_by_subject:
            return [], False
        cutoff = clock(packet["cutoff"])
        earliest = cutoff - timedelta(hours=policy["source_max_age_hours"])
        rows = (
            await self.session.execute(
                select(Claim, RawEvidence, Edge)
                .select_from(Claim)
                .join(SourceItem, SourceItem.id == Claim.source_item_id)
                .join(RawEvidence, RawEvidence.id == SourceItem.raw_evidence_id)
                .join(Edge, Edge.source_id == Claim.id)
                .where(
                    Edge.source_type == "claim",
                    Edge.relationship_type.in_(("supports", "contradicts")),
                    or_(
                        *(
                            (Edge.target_type == kind) & (Edge.target_id == subject_id)
                            for kind, subject_id in security_by_subject
                        )
                    ),
                    Edge.created_at <= cutoff,
                    Claim.is_deprecated.is_(False),
                    Claim.superseded_by_id.is_(None),
                    Claim.importance.in_(("critical", "high")),
                    Claim.confidence >= policy["minimum_claim_confidence"],
                    func.lower(Claim.sentiment).in_(("adverse", "bearish", "negative")),
                    Claim.created_at <= cutoff,
                    Claim.updated_at <= cutoff,
                    Claim.ingest_time <= cutoff,
                    or_(Claim.event_time.is_(None), Claim.event_time <= cutoff),
                    or_(
                        Claim.eligible_action_time.is_(None),
                        Claim.eligible_action_time <= cutoff,
                    ),
                    or_(Claim.valid_until.is_(None), Claim.valid_until >= cutoff),
                    or_(Claim.stale_after.is_(None), Claim.stale_after >= cutoff),
                    RawEvidence.public_time.between(earliest, cutoff),
                    RawEvidence.ingest_time <= cutoff,
                    RawEvidence.updated_at <= cutoff,
                    RawEvidence.content_hash.is_not(None),
                    RawEvidence.content_hash != "",
                    RawEvidence.url.is_not(None),
                    RawEvidence.url != "",
                )
                .order_by(
                    desc(RawEvidence.public_time), RawEvidence.id, Claim.id, Edge.id
                )
                .limit(SOURCE_ROW_LIMIT + 1)
            )
        ).all()
        sources = {}
        for claim, raw, edge in rows[:SOURCE_ROW_LIMIT]:
            source = sources.setdefault(
                raw.id,
                {
                    "evidence_id": str(raw.id),
                    "content_hash": raw.content_hash,
                    "title": raw.title,
                    "url": raw.url,
                    "published_at": raw.public_time.isoformat(),
                    "retrieved_at": raw.ingest_time.isoformat(),
                    "version_observed_at": raw.updated_at.isoformat(),
                    "security_ids": [],
                    "claims": [],
                },
            )
            security_ids = sorted(
                security_by_subject[(edge.target_type, edge.target_id)]
            )
            source["security_ids"] = sorted(
                set(source["security_ids"]) | set(security_ids)
            )
            source["claims"].append(
                {
                    "claim_id": str(claim.id),
                    "source_item_id": str(claim.source_item_id),
                    "edge_id": str(edge.id),
                    "security_ids": security_ids,
                    "statement": claim.statement,
                    "sentiment": claim.sentiment,
                    "confidence": float(claim.confidence),
                    "event_at": (
                        claim.event_time.isoformat() if claim.event_time else None
                    ),
                    "available_at": max(
                        raw.ingest_time,
                        raw.updated_at,
                        claim.updated_at,
                        claim.eligible_action_time or claim.created_at,
                        edge.created_at,
                    ).isoformat(),
                }
            )
        return list(sources.values()), len(rows) > SOURCE_ROW_LIMIT
