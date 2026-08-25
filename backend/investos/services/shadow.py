from __future__ import annotations

import asyncio
import hashlib
import json
import re
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from investos.config import settings
from investos.core.llm import call_llm_json
from investos.db import async_session_maker
from investos.models.conclusion import ConclusionState
from investos.models.entity import Entity, Security
from investos.models.evidence import RawEvidence, SourceItem
from investos.models.knowledge import Claim, Event, Fact
from investos.models.lesson import Lesson, LessonObservation
from investos.models.portfolio import Position, Transaction
from investos.models.shadow import (
    ExperimentFamilyState,
    ExperimentResult,
    ShadowAccountEvent,
    ShadowAction,
    ShadowEvidenceEvent,
    ShadowExperiment,
    ShadowFill,
    ShadowOrder,
)
from investos.models.source import Source
from investos.schemas.shadow import ShadowExperimentCreate, ShadowOrderCreate
from investos.services.fundamentals import FundamentalMetricService
from investos.services.market_data import MarketDataService
from investos.services.market_setup import MarketSetupSignalService
from investos.services.paper_broker import (
    LocalPaperBroker,
    PaperAccountEventRequest,
    PaperBrokerExecution,
    PaperBrokerPolicy,
    PaperOrderRequest,
    PaperRecordedFillRequest,
)
from investos.services.portfolio_peers import PortfolioPeerContextService
from investos.services.review import ReviewService
from investos.services.runtime_settings import RuntimeSettingsStore

_EXPERIMENT_RUN_LOCKS: dict[UUID, asyncio.Lock] = {}
_SHADOW_LEARNING_SCHEMA_VERSION = 2

SHADOW_GUIDANCE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "guidance_mode": {
            "type": "string",
            "enum": [
                "follow_existing_policy",
                "defensive",
                "aggressive",
                "wait_for_confirmation",
                "concentration_control",
            ],
        },
        "guidance_summary": {"type": "string"},
        "cash_reserve_pct": {"type": "number"},
        "max_position_multiplier": {"type": "number"},
    },
    "required": [
        "guidance_mode",
        "guidance_summary",
        "cash_reserve_pct",
        "max_position_multiplier",
    ],
}

SHADOW_CHECKPOINT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "checkpoint_objective": {"type": "string"},
        "portfolio_view": {"type": "string"},
        "planned_posture": {"type": "string"},
        "why_now": {"type": "string"},
        "research_goal": {"type": ["string", "null"]},
        "monitoring_focus": {"type": "array", "items": {"type": "string"}},
        "what_would_change_mind": {"type": "array", "items": {"type": "string"}},
        "catalyst_tracker": {"type": "string"},
        "contingency_plan": {"type": "string"},
        "decisions": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "ticker": {"type": "string"},
                    "entity_name": {"type": ["string", "null"]},
                    "observed_signal": {"type": "string"},
                    "thesis_view": {"type": "string"},
                    "action": {"type": "string", "enum": ["buy", "sell", "hold"]},
                    "position_multiplier": {"type": "number"},
                    "target_weight_pct": {"type": "number"},
                    "expected_outcome": {"type": "string"},
                    "risk_guardrail": {"type": "string"},
                    "exit_or_adjustment_trigger": {"type": "string"},
                    "active_watcher": {
                        "type": ["object", "null"],
                        "additionalProperties": False,
                        "properties": {
                            "condition_type": {
                                "type": "string",
                                "description": (
                                    "Specific snake_case trigger type. Use price_above/price_below for quoted price levels, "
                                    "earnings_release/news_sentiment for those exact cases, or a precise catalyst, metric, "
                                    "filing, reminder, contradiction, or setup trigger when that is more accurate."
                                ),
                            },
                            "threshold": {"type": ["number", "null"]},
                            "deadline_hours": {"type": ["number", "null"]},
                        },
                        "required": ["condition_type", "threshold", "deadline_hours"],
                    },
                    "rationale": {"type": "string"},
                },
                "required": [
                    "ticker",
                    "entity_name",
                    "observed_signal",
                    "thesis_view",
                    "action",
                    "position_multiplier",
                    "target_weight_pct",
                    "expected_outcome",
                    "risk_guardrail",
                    "exit_or_adjustment_trigger",
                    "active_watcher",
                    "rationale",
                ],
            },
        },
    },
    "required": [
        "checkpoint_objective",
        "portfolio_view",
        "planned_posture",
        "why_now",
        "research_goal",
        "monitoring_focus",
        "what_would_change_mind",
        "catalyst_tracker",
        "contingency_plan",
        "decisions",
    ],
}

SHADOW_DISCOVERY_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "should_launch": {"type": "boolean"},
        "name": {"type": "string"},
        "family_key": {"type": "string"},
        "family_description": {"type": "string"},
        "opportunity_type": {"type": "string"},
        "priority_score": {"type": "number"},
        "signal_stage": {"type": "string"},
        "why_now": {"type": "string"},
        "priced_in_assessment": {"type": "string"},
        "investable_thesis": {"type": "string"},
        "portfolio_transmission": {"type": "string"},
        "expected_edge": {"type": "string"},
        "expected_relative_direction": {
            "type": "string",
            "enum": ["outperform", "underperform"],
        },
        "leading_indicators": {"type": "array", "items": {"type": "string"}},
        "lagging_confirmations": {"type": "array", "items": {"type": "string"}},
        "evidence_refs": {"type": "array", "items": {"type": "string"}},
        "evidence_to_check": {"type": "array", "items": {"type": "string"}},
        "falsification_tests": {"type": "array", "items": {"type": "string"}},
        "risk_controls": {"type": "array", "items": {"type": "string"}},
        "assumptions": {"type": "array", "items": {"type": "string"}},
        "uncertainties": {"type": "array", "items": {"type": "string"}},
        "policy": {"type": "string"},
        "operator_prompt": {"type": "string"},
        "horizon": {"type": "string"},
        "horizon_days": {"type": "integer", "minimum": 1, "maximum": 1825},
        "no_launch_reason": {"type": "string"},
    },
    "required": [
        "should_launch",
        "name",
        "family_key",
        "family_description",
        "opportunity_type",
        "priority_score",
        "signal_stage",
        "why_now",
        "priced_in_assessment",
        "investable_thesis",
        "portfolio_transmission",
        "expected_edge",
        "expected_relative_direction",
        "leading_indicators",
        "lagging_confirmations",
        "evidence_refs",
        "evidence_to_check",
        "falsification_tests",
        "risk_controls",
        "assumptions",
        "uncertainties",
        "policy",
        "operator_prompt",
        "horizon",
        "horizon_days",
        "no_launch_reason",
    ],
}


class ShadowService:
    def __init__(self, session: AsyncSession):
        self.session = session
        self._security_ticker_cache: dict[str, str] = {}

    @staticmethod
    def normalize_run_status(status: str | None) -> str:
        if status == "pending":
            return "queued"
        return status or "queued"

    async def list_experiments(self) -> list[ShadowExperiment]:
        return list(
            (
                await self.session.execute(
                    select(ShadowExperiment).order_by(
                        ShadowExperiment.created_at.desc()
                    )
                )
            )
            .scalars()
            .all()
        )

    async def get_experiment(self, experiment_id: UUID) -> ShadowExperiment | None:
        return (
            await self.session.execute(
                select(ShadowExperiment).where(ShadowExperiment.id == experiment_id)
            )
        ).scalar_one_or_none()

    async def create_experiment(
        self, payload: ShadowExperimentCreate
    ) -> ShadowExperiment:
        if payload.account_basis == "cash_only" and (
            payload.starting_cash is None or payload.starting_cash <= 0
        ):
            raise ValueError("Cash-only paper accounts require positive starting_cash.")
        if (
            payload.account_basis == "clone_portfolio"
            and payload.starting_cash is not None
        ):
            raise ValueError(
                "starting_cash is only valid for cash-only paper accounts."
            )
        now = datetime.now(UTC)
        start_point = payload.start_point or now
        profile_horizon_days = self._profile_horizon_days(payload.discovery_profile)
        end_point = payload.end_point or (
            start_point + timedelta(days=profile_horizon_days)
            if profile_horizon_days is not None
            else self._default_end_point(
                start_point=start_point,
                horizon_label=payload.horizon_label,
            )
        )
        if payload.auto_run and end_point <= start_point:
            raise ValueError(
                "Autonomous shadow experiments require an end_point after start_point."
            )
        operator_prompt = payload.operator_prompt or self._operator_prompt_from_policy(
            policy_description=payload.policy_description,
            trigger_reason=payload.trigger_reason,
        )
        family_state = await self._get_or_create_family_state(
            name=payload.name,
            policy_description=payload.policy_description,
            trigger_reason=payload.trigger_reason,
            discovery_profile=payload.discovery_profile,
        )
        initial_state = await self._portfolio_snapshot(
            trigger_type=payload.trigger_type or "manual",
            trigger_reason=payload.trigger_reason,
            horizon_label=payload.horizon_label,
            initiated_by=payload.initiated_by or "user",
            policy_description=payload.policy_description,
            operator_prompt=operator_prompt,
            discovery_profile=payload.discovery_profile,
            subject_refs=payload.subject_refs,
        )
        initial_state["experiment_context"]["execution_mode"] = (
            "autonomous" if payload.auto_run else "manual"
        )
        initial_state["experiment_context"]["account_basis"] = payload.account_basis
        if payload.account_basis == "cash_only":
            initial_state["shadow_state"] = {
                "cash": round(float(payload.starting_cash), 2),
                "cash_reserved": 0.0,
                "positions": [],
            }
            initial_state["snapshot_summary"]["paper_starting_cash"] = round(
                float(payload.starting_cash), 2
            )
        initial_state["run_details"] = {
            "paper_account": self._paper_account_summary(initial_state["shadow_state"])
        }
        experiment = ShadowExperiment(
            family_id=None if family_state is None else family_state.id,
            name=payload.name,
            policy_description=payload.policy_description,
            start_point=start_point,
            end_point=end_point,
            initial_portfolio_state_json=initial_state,
            run_status="queued" if payload.auto_run else "manual",
        )
        self.session.add(experiment)
        await self.session.commit()
        await self.session.refresh(experiment)
        return experiment

    async def queue_experiment_run(self, experiment_id: UUID) -> ShadowExperiment:
        experiment = await self.get_experiment(experiment_id)
        if not experiment:
            raise ValueError("Shadow experiment not found")
        if self.normalize_run_status(experiment.run_status) == "running":
            return experiment
        initial_state = deepcopy(experiment.initial_portfolio_state_json or {})
        context = dict(initial_state.get("experiment_context") or {})
        context["execution_mode"] = "autonomous"
        initial_state["experiment_context"] = context
        experiment.initial_portfolio_state_json = initial_state
        experiment.run_status = "queued"
        experiment.skip_reason = None
        experiment.completed_at = None
        await self.session.commit()
        await self.session.refresh(experiment)
        return experiment

    @staticmethod
    def _normalize_subject_refs(
        subject_refs: list[dict] | None,
    ) -> list[dict[str, str]]:
        normalized: list[dict[str, str]] = []
        seen: set[tuple[str, str, str]] = set()
        for ref in subject_refs or []:
            subject_type = str(ref.get("subject_type") or "").strip().lower()
            subject_id = str(ref.get("subject_id") or "").strip()
            security_id = str(ref.get("security_id") or "").strip()
            if not subject_type or not subject_id:
                continue
            key = (subject_type, subject_id, security_id)
            if key in seen:
                continue
            seen.add(key)
            normalized.append(
                {
                    "subject_type": subject_type,
                    "subject_id": subject_id,
                    **({"security_id": security_id} if security_id else {}),
                }
            )
        return normalized

    @classmethod
    def _experiment_matches_subject(
        cls,
        experiment: ShadowExperiment,
        *,
        subject_type: str,
        subject_id: UUID,
        security_id: UUID | None,
    ) -> bool:
        context = (experiment.initial_portfolio_state_json or {}).get(
            "experiment_context"
        ) or {}
        expected_subject = str(subject_id)
        expected_security = None if security_id is None else str(security_id)
        for ref in cls._normalize_subject_refs(context.get("subject_refs")):
            if (
                ref["subject_type"] == subject_type
                and ref["subject_id"] == expected_subject
            ):
                return True
            if expected_security and ref.get("security_id") == expected_security:
                return True
        return False

    async def find_subject_experiment(
        self,
        *,
        subject_type: str,
        subject_id: UUID,
        security_id: UUID | None,
        statuses: set[str] | None = None,
    ) -> ShadowExperiment | None:
        statuses = statuses or {"queued", "running"}
        database_statuses = set(statuses)
        if "queued" in statuses:
            database_statuses.add("pending")
        experiments = list(
            (
                await self.session.execute(
                    select(ShadowExperiment)
                    .where(ShadowExperiment.run_status.in_(database_statuses))
                    .order_by(ShadowExperiment.created_at.desc())
                )
            )
            .scalars()
            .all()
        )
        return next(
            (
                experiment
                for experiment in experiments
                if self._experiment_matches_subject(
                    experiment,
                    subject_type=subject_type,
                    subject_id=subject_id,
                    security_id=security_id,
                )
            ),
            None,
        )

    async def queue_subject_evidence_event(
        self,
        *,
        experiment: ShadowExperiment,
        subject_type: str,
        subject_id: UUID,
        security_id: UUID | None,
        trigger_reason: str,
        raw_evidence_id: UUID | None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        key_material = (
            str(raw_evidence_id)
            if raw_evidence_id is not None
            else hashlib.sha256(trigger_reason.encode("utf-8")).hexdigest()
        )
        idempotency_key = hashlib.sha256(
            (
                f"{experiment.id}|{subject_type}|{subject_id}|"
                f"{security_id or ''}|{key_material}"
            ).encode("utf-8")
        ).hexdigest()
        event_id = uuid4()
        inserted_id = (
            await self.session.execute(
                pg_insert(ShadowEvidenceEvent)
                .values(
                    id=event_id,
                    experiment_id=experiment.id,
                    raw_evidence_id=raw_evidence_id,
                    subject_type=subject_type,
                    subject_id=subject_id,
                    security_id=security_id,
                    trigger_reason=trigger_reason,
                    status="queued",
                    idempotency_key=idempotency_key,
                    metadata_json=metadata or {},
                )
                .on_conflict_do_nothing(
                    index_elements=[ShadowEvidenceEvent.idempotency_key]
                )
                .returning(ShadowEvidenceEvent.id)
            )
        ).scalar_one_or_none()
        event_id = inserted_id
        if event_id is None:
            event_id = (
                await self.session.execute(
                    select(ShadowEvidenceEvent.id).where(
                        ShadowEvidenceEvent.idempotency_key == idempotency_key
                    )
                )
            ).scalar_one()
        await self.session.commit()
        return {
            "event_id": str(event_id),
            "experiment_id": str(experiment.id),
            "queued": inserted_id is not None,
            "deduplicated": inserted_id is None,
        }

    async def attach_queued_evidence_events(
        self, *, limit: int | None = None
    ) -> dict[str, Any]:
        batch_limit = max(
            1,
            int(limit or settings.SHADOW_EVIDENCE_EVENT_BATCH_SIZE),
        )
        events = list(
            (
                await self.session.execute(
                    select(ShadowEvidenceEvent)
                    .where(ShadowEvidenceEvent.status == "queued")
                    .order_by(ShadowEvidenceEvent.created_at.asc())
                    .limit(batch_limit)
                    .with_for_update(skip_locked=True)
                )
            )
            .scalars()
            .all()
        )
        attached_experiment_ids: list[UUID] = []
        attached = 0
        skipped = 0
        for event in events:
            experiment = await self.get_experiment(event.experiment_id)
            if experiment is None or self.normalize_run_status(
                None if experiment is None else experiment.run_status
            ) not in {"queued", "running"}:
                event.status = "skipped"
                event.processing_detail = "experiment_not_active"
                event.processed_at = datetime.now(UTC)
                skipped += 1
                continue
            state = deepcopy(
                experiment.final_portfolio_state_json
                or experiment.initial_portfolio_state_json
                or {}
            )
            run_details = dict(state.get("run_details") or {})
            pending = list(run_details.get("pending_evidence_events") or [])
            if not any(str(item.get("event_id")) == str(event.id) for item in pending):
                pending.append(
                    {
                        "event_id": str(event.id),
                        "raw_evidence_id": (
                            None
                            if event.raw_evidence_id is None
                            else str(event.raw_evidence_id)
                        ),
                        "subject_type": event.subject_type,
                        "subject_id": str(event.subject_id),
                        "security_id": (
                            None
                            if event.security_id is None
                            else str(event.security_id)
                        ),
                        "trigger_reason": event.trigger_reason,
                        "queued_at": event.created_at.isoformat(),
                        "metadata": event.metadata_json or {},
                    }
                )
            run_details["pending_evidence_events"] = pending
            state["run_details"] = run_details
            experiment.final_portfolio_state_json = state
            event.status = "attached"
            event.processing_detail = "attached_to_next_checkpoint"
            attached += 1
            if experiment.id not in attached_experiment_ids:
                attached_experiment_ids.append(experiment.id)
        await self.session.commit()
        return {
            "scanned": len(events),
            "attached": attached,
            "skipped": skipped,
            "experiment_ids": attached_experiment_ids,
        }

    async def discover_and_queue_experiments(self) -> int:
        """
        Autonomous discovery loop: builds a point-in-time candidate packet and
        launches one evidence-linked shadow experiment when warranted.
        Returns the number of experiments created.
        """
        captured_at = datetime.now(UTC)
        discovery_context = await self._build_discovery_context(captured_at=captured_at)
        if not discovery_context["candidates"]:
            return 0

        try:
            discovery_profile, is_actionable, skip_reason = (
                await self.evaluate_discovery_context(discovery_context)
            )
            if is_actionable:
                family_state = await self._get_or_create_family_state(
                    name=str(discovery_profile["name"]),
                    policy_description=str(discovery_profile.get("policy") or ""),
                    trigger_reason=str(discovery_profile.get("trigger_reason") or ""),
                    discovery_profile=discovery_profile,
                )
                if family_state is not None and not self._family_ready_for_new_run(
                    family_state
                ):
                    return 0
                experiment = await self.create_experiment(
                    ShadowExperimentCreate(
                        name=discovery_profile["name"],
                        policy_description=discovery_profile["policy"],
                        trigger_type="autonomous_discovery",
                        trigger_reason=discovery_profile["trigger_reason"],
                        horizon_label=discovery_profile["horizon"],
                        initiated_by="shadow_investor",
                        operator_prompt=discovery_profile.get("operator_prompt")
                        or None,
                        discovery_profile=discovery_profile,
                        subject_refs=self._discovery_subject_refs(
                            discovery_profile=discovery_profile,
                            candidates=discovery_context["candidates"],
                        ),
                    )
                )
                return 1
            if skip_reason:
                import logging

                logging.getLogger(__name__).info(
                    "Shadow discovery skipped: %s", skip_reason
                )
        except Exception as exc:
            import logging

            logging.getLogger(__name__).exception("Shadow discovery failed")
            raise

        return 0

    async def evaluate_discovery_context(
        self,
        discovery_context: dict[str, Any],
    ) -> tuple[dict[str, Any], bool, str | None]:
        """Evaluate a dated packet without mutating accepted or portfolio state."""

        discovery_result = await call_llm_json(
            system_prompt=self._shadow_discovery_prompt(),
            user_prompt=json.dumps(
                {
                    "point_in_time_opportunity_packet": discovery_context,
                    "instruction": (
                        "Identify at most one paper-trading opportunity or risk-control experiment. "
                        "Use only information whose evidence_ref is present in this packet."
                    ),
                },
                ensure_ascii=True,
                default=str,
            ),
            schema=SHADOW_DISCOVERY_SCHEMA,
        )
        profile = self._normalize_discovery_profile(discovery_result)
        captured_at = str(discovery_context.get("captured_at") or "")
        profile["captured_at"] = captured_at
        profile["portfolio_snapshot"] = discovery_context.get("portfolio") or {}
        evidence_registry = {
            str(item["ref"]): item
            for item in discovery_context.get("evidence_registry") or []
            if item.get("ref")
        }
        profile["evidence_snapshot"] = [
            evidence_registry[ref]
            for ref in profile.get("evidence_refs") or []
            if ref in evidence_registry
        ]
        actionable, reason = self._discovery_profile_is_actionable(
            profile,
            available_evidence_refs=set(evidence_registry),
            portfolio_value=(
                float(
                    (discovery_context.get("portfolio") or {}).get("total_market_value")
                    or 0.0
                )
                + float(
                    (discovery_context.get("portfolio") or {}).get(
                        "remaining_buying_power"
                    )
                    or 0.0
                )
            ),
        )
        return profile, actionable, reason

    @classmethod
    def _discovery_subject_refs(
        cls,
        *,
        discovery_profile: dict[str, Any],
        candidates: list[dict[str, Any]],
    ) -> list[dict[str, str]]:
        evidence_tickers = {
            str(item.get("ticker") or "").strip().upper()
            for item in discovery_profile.get("evidence_snapshot") or []
            if item.get("ticker")
        }
        refs = [
            {
                "subject_type": "entity",
                "subject_id": str(candidate["entity_id"]),
                "security_id": str(candidate["security_id"]),
            }
            for candidate in candidates
            if str(candidate.get("ticker") or "").strip().upper() in evidence_tickers
            and candidate.get("entity_id")
            and candidate.get("security_id")
        ]
        return cls._normalize_subject_refs(refs)

    @staticmethod
    def _shadow_discovery_prompt() -> str:
        return (
            "Evaluate the supplied point-in-time packet for one shadow paper-trading experiment. "
            "Do not role-play, predict with certainty, or launch just because an idea is interesting. "
            "Return should_launch=true only when the opportunity is specific, investable, falsifiable, and relevant to current holdings or cash/risk posture. "
            "Look for a change in an underlying mechanism before lagging reported revenue, earnings, or margins make the change obvious, but never infer such a change from price momentum alone. "
            "Separate observed leading indicators from later confirmation that has not happened yet. Assess what the current price and investor setup may already discount. "
            "Cite exact evidence_refs from the packet for every launch; use only facts available by captured_at and treat stale, weak, or contradictory evidence accordingly. "
            "Do not invent dollar notionals, quantities, leverage, or portfolio weights. Use only supplied values or express sizing as a bounded percentage of an existing position. "
            "A launchable idea must name why now, the portfolio transmission route, expected edge, evidence references, falsification tests, uncertainty, and risk controls. "
            "Use should_launch=false when the portfolio state is too thin, the idea lacks a measurable edge, or the experiment would be generic. "
            "Use priority_score for supervision and sorting, not as a substitute for the explicit evidence/falsifier/risk-control contract. "
            "family_key must be a concise, stable, open-ended label for the underlying hypothesis family rather than a one-off headline; family_description should state the reusable mechanism. "
            "Prior shadow learning is hypothesis context, not fresh evidence. Use provisional or mixed lessons to design checks, never to justify a launch by themselves. "
            "State whether the named security is expected to outperform or underperform the configured benchmark, and provide an exact horizon_days before outcomes are known. "
            "opportunity_type, signal_stage, priced_in_assessment, and horizon remain open labels; choose natural descriptions for the actual setup instead of forcing preset buckets. "
            "The policy should define the paper-trading behavior; operator_prompt should define checkpoint behavior, sizing discipline, monitoring, and exit/adjustment triggers."
        )

    async def build_discovery_context(
        self,
        *,
        captured_at: datetime,
        additional_security_ids: list[UUID] | None = None,
    ) -> dict[str, Any]:
        rows = (
            await self.session.execute(
                select(Position, Security, Entity)
                .join(Security, Position.security_id == Security.id)
                .join(Entity, Security.entity_id == Entity.id)
                .where(Position.list_type.in_(["holding", "watchlist", "considering"]))
                .order_by(Position.market_value.desc(), Position.added_at.desc())
                .limit(14)
            )
        ).all()
        subject_rows: list[tuple[Position | None, Security, Entity]] = list(rows)
        present_security_ids = {security.id for _, security, _ in rows}
        requested_security_ids = (
            set(additional_security_ids or []) - present_security_ids
        )
        if requested_security_ids:
            external_rows = (
                await self.session.execute(
                    select(Security, Entity)
                    .join(Entity, Security.entity_id == Entity.id)
                    .where(
                        Security.id.in_(requested_security_ids),
                        Security.is_active.is_(True),
                    )
                    .order_by(Security.ticker)
                )
            ).all()
            subject_rows.extend(
                (None, security, entity) for security, entity in external_rows
            )

        holdings = [
            position for position, _, _ in rows if position.list_type == "holding"
        ]
        total_market_value = sum(
            float(position.market_value or 0.0) for position in holdings
        )
        tickers = [
            security.ticker for _, security, _ in subject_rows if security.ticker
        ]
        runtime = RuntimeSettingsStore.load()
        tape_by_ticker = (
            await MarketDataService(self.session).fetch_signal_snapshots(tickers)
            if runtime.market_data.enabled
            and runtime.market_data.provider == "yahoo_finance"
            else {}
        )
        metric_service = FundamentalMetricService(self.session)
        setup_service = MarketSetupSignalService(self.session)
        evidence_registry: list[dict[str, Any]] = []
        candidates: list[dict[str, Any]] = []

        for position, security, entity in subject_rows:
            conclusion = await self._position_or_entity_conclusion(
                position=position, entity_id=entity.id
            )
            metrics = await metric_service.relevant_metrics(
                subject_type="position" if position is not None else "entity",
                subject_id=position.id if position is not None else entity.id,
                limit=4,
            )
            setups = await setup_service.relevant_signals(
                subject_type="position" if position is not None else "entity",
                subject_id=position.id if position is not None else entity.id,
                limit=4,
            )
            metrics = [
                item for item in metrics if self._available_by(item, captured_at)
            ]
            setups = [item for item in setups if self._available_by(item, captured_at)]
            metric_context = [
                self._with_evidence_ref(item, prefix="fundamental") for item in metrics
            ]
            setup_context = [
                self._with_evidence_ref(item, prefix="setup") for item in setups
            ]
            conclusion_evidence = await self._conclusion_evidence_context(
                conclusion,
                captured_at=captured_at,
            )
            tape = tape_by_ticker.get(str(security.ticker or "").upper())
            if tape and tape.get("signal_ref"):
                evidence_registry.append(
                    {
                        "ref": tape["signal_ref"],
                        "kind": "market_tape",
                        "ticker": security.ticker,
                        "as_of": tape.get("as_of"),
                        "summary": {
                            key: value
                            for key, value in tape.items()
                            if key not in {"signal_ref", "ticker", "as_of"}
                        },
                    }
                )
            for item in [*metric_context, *setup_context, *conclusion_evidence]:
                evidence_registry.append(
                    self._registry_entry(item, ticker=security.ticker)
                )

            market_value = float(position.market_value or 0.0) if position else 0.0
            candidates.append(
                {
                    "position_id": str(position.id) if position is not None else None,
                    "entity_id": str(entity.id),
                    "security_id": str(security.id),
                    "ticker": security.ticker,
                    "entity_name": entity.name,
                    "list_type": (
                        position.list_type
                        if position is not None
                        else "opportunity_universe"
                    ),
                    "sector": entity.sector,
                    "industry": entity.industry,
                    "market_value": market_value,
                    "weight_pct": (
                        round((market_value / total_market_value) * 100.0, 2)
                        if position is not None
                        and position.list_type == "holding"
                        and total_market_value > 0
                        else (
                            float(position.weight_pct or 0.0)
                            if position is not None
                            else 0.0
                        )
                    ),
                    "stance": conclusion.current_stance if conclusion else "no_view",
                    "confidence_band": (
                        conclusion.confidence_band if conclusion else "very_low"
                    ),
                    "thesis_summary": (
                        conclusion.current_thesis_summary if conclusion else None
                    ),
                    "what_would_strengthen": (
                        list(conclusion.what_would_strengthen or [])
                        if conclusion
                        else []
                    ),
                    "what_would_falsify": (
                        list(conclusion.what_would_falsify or []) if conclusion else []
                    ),
                    "market_tape": tape,
                    "fundamental_metrics": metric_context,
                    "market_setup_signals": setup_context,
                    "accepted_state_evidence": conclusion_evidence,
                }
            )

        deduped_registry = {
            str(item["ref"]): item for item in evidence_registry if item.get("ref")
        }
        buying_power = float(runtime.portfolio.remaining_buying_power or 0.0)
        return {
            "captured_at": captured_at.isoformat(),
            "portfolio": {
                "holding_count": len(holdings),
                "tracked_count": len(subject_rows),
                "portfolio_subject_count": len(rows),
                "opportunity_universe_subject_count": len(subject_rows) - len(rows),
                "total_market_value": round(total_market_value, 2),
                "remaining_buying_power": round(buying_power, 2),
                "pct_capital_deployed": (
                    round(
                        (total_market_value / (total_market_value + buying_power))
                        * 100.0,
                        2,
                    )
                    if total_market_value + buying_power > 0
                    else 0.0
                ),
            },
            "candidates": candidates,
            "peer_exposures": await PortfolioPeerContextService(
                self.session
            ).peer_exposures(limit=12),
            "prior_shadow_learning": await self._shadow_learning_context(
                candidates=candidates,
                captured_at=captured_at,
            ),
            "evidence_registry": list(deduped_registry.values()),
            "available_evidence_refs": sorted(deduped_registry),
        }

    async def _build_discovery_context(
        self, *, captured_at: datetime
    ) -> dict[str, Any]:
        return await self.build_discovery_context(captured_at=captured_at)

    async def _shadow_learning_context(
        self,
        *,
        candidates: list[dict[str, Any]],
        captured_at: datetime,
    ) -> list[dict[str, Any]]:
        limit = max(0, int(settings.SHADOW_LESSON_CONTEXT_LIMIT))
        if limit == 0:
            return []
        lessons = list(
            (
                await self.session.execute(
                    select(Lesson)
                    .where(
                        Lesson.lesson_type == "shadow_policy_outcome",
                    )
                    .order_by(
                        Lesson.confidence_score.desc(),
                        Lesson.last_validated_at.desc(),
                        Lesson.created_at.desc(),
                    )
                    .limit(max(limit * 4, limit))
                )
            )
            .scalars()
            .all()
        )
        if not lessons:
            return []

        portfolio_terms = {
            term
            for candidate in candidates
            for term in re.findall(
                r"[a-z0-9]{2,}",
                " ".join(
                    str(candidate.get(key) or "").lower()
                    for key in ("ticker", "entity_name", "sector", "industry")
                ),
            )
        }
        ranked: list[tuple[float, Lesson]] = []
        maturity_weight = {
            "validated": 2.0,
            "emerging": 1.2,
            "mixed": 0.8,
            "provisional": 0.4,
        }
        for lesson in lessons:
            if lesson.stale_after is not None and lesson.stale_after <= captured_at:
                continue
            lesson_terms = set(
                re.findall(r"[a-z0-9]{2,}", f"{lesson.title} {lesson.summary}".lower())
            )
            overlap = len(portfolio_terms & lesson_terms)
            score = (
                maturity_weight.get(lesson.maturity_status, 0.2)
                + float(lesson.confidence_score or 0.0)
                + min(2.0, overlap * 0.25)
            )
            ranked.append((score, lesson))
        ranked.sort(key=lambda item: (item[0], item[1].created_at), reverse=True)
        return [
            {
                "lesson_id": str(lesson.id),
                "title": lesson.title,
                "summary": lesson.summary,
                "maturity_status": lesson.maturity_status,
                "confidence_score": round(float(lesson.confidence_score or 0.0), 3),
                "observation_counts": {
                    "supporting": int(lesson.supporting_observations or 0),
                    "contradicting": int(lesson.contradicting_observations or 0),
                    "neutral": int(lesson.neutral_observations or 0),
                },
                "use_policy": (
                    "Treat as a tested policy lesson. Re-check current mechanism and falsifiers."
                    if lesson.maturity_status == "validated"
                    else "Treat as a hypothesis seed only; do not use it as launch evidence."
                ),
            }
            for _, lesson in ranked[:limit]
        ]

    async def _position_or_entity_conclusion(
        self,
        *,
        position: Position | None,
        entity_id: UUID,
    ) -> ConclusionState | None:
        subjects = [("entity", entity_id)]
        if position is not None:
            subjects.insert(0, ("position", position.id))
        for subject_type, subject_id in subjects:
            conclusion = (
                await self.session.execute(
                    select(ConclusionState)
                    .where(
                        ConclusionState.subject_type == subject_type,
                        ConclusionState.subject_id == subject_id,
                    )
                    .order_by(ConclusionState.last_updated_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if conclusion is not None:
                return conclusion
        return None

    async def _conclusion_evidence_context(
        self,
        conclusion: ConclusionState | None,
        *,
        captured_at: datetime,
    ) -> list[dict[str, Any]]:
        if conclusion is None:
            return []
        rows: list[dict[str, Any]] = []
        candidates = [
            *(
                ("supporting", item)
                for item in (conclusion.key_supporting_evidence_ids or [])[:3]
            ),
            *(
                ("contradicting", item)
                for item in (conclusion.key_contradicting_evidence_ids or [])[:3]
            ),
        ]
        for role, evidence_id in candidates:
            resolved = None
            kind = None
            for model, label in ((Fact, "fact"), (Claim, "claim"), (Event, "event")):
                item = await self.session.get(model, evidence_id)
                if item is not None:
                    resolved = item
                    kind = label
                    break
            if resolved is None or kind is None:
                continue
            payload = {
                "id": str(resolved.id),
                "evidence_ref": f"evidence:{resolved.id}",
                "kind": kind,
                "role": role,
                "text": getattr(resolved, "statement", None)
                or getattr(resolved, "description", None)
                or getattr(resolved, "title", None),
                "tier": getattr(resolved, "tier", None),
                "confidence": self._bounded_float(
                    getattr(resolved, "confidence", None), 0.0, 1.0
                ),
                "event_time": self._iso(getattr(resolved, "event_time", None)),
                "public_time": self._iso(getattr(resolved, "public_time", None)),
                "eligible_action_time": self._iso(
                    getattr(resolved, "eligible_action_time", None)
                ),
            }
            payload.update(await self._knowledge_source_context(resolved))
            if self._available_by(payload, captured_at):
                rows.append(payload)
        return rows

    async def _knowledge_source_context(
        self, item: Fact | Claim | Event
    ) -> dict[str, Any]:
        source_item_id = getattr(item, "source_item_id", None)
        if source_item_id is None:
            return {}
        source_item = await self.session.get(SourceItem, source_item_id)
        if source_item is None:
            return {}
        raw = await self.session.get(RawEvidence, source_item.raw_evidence_id)
        if raw is None:
            return {}
        source = await self.session.get(Source, raw.source_id)
        return {
            "source_name": getattr(source, "name", None),
            "source_type": getattr(source, "source_type", None),
            "evidence_title": raw.title,
            "url": raw.url,
        }

    @staticmethod
    def _with_evidence_ref(item: dict[str, Any], *, prefix: str) -> dict[str, Any]:
        payload = dict(item)
        payload["evidence_ref"] = f"{prefix}:{item['id']}"
        return payload

    @staticmethod
    def _registry_entry(item: dict[str, Any], *, ticker: str | None) -> dict[str, Any]:
        return {
            "ref": item.get("evidence_ref"),
            "kind": item.get("kind")
            or item.get("metric_family")
            or item.get("signal_family")
            or "evidence",
            "ticker": ticker,
            "as_of": item.get("public_time")
            or item.get("as_of")
            or item.get("event_time"),
            "source": item.get("source_name") or item.get("source_kind"),
            "url": item.get("url"),
            "summary": item.get("text")
            or item.get("investment_relevance")
            or item.get("setup_context")
            or item.get("value_text"),
        }

    @staticmethod
    def _available_by(item: dict[str, Any], captured_at: datetime) -> bool:
        for key in ("eligible_action_time", "public_time"):
            value = item.get(key)
            if not value:
                continue
            try:
                available_at = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            except ValueError:
                continue
            if available_at.tzinfo is None:
                available_at = available_at.replace(tzinfo=UTC)
            return available_at <= captured_at
        return True

    @staticmethod
    def _iso(value: Any) -> str | None:
        return value.isoformat() if isinstance(value, datetime) else None

    @staticmethod
    def _normalize_discovery_profile(result: dict[str, Any] | None) -> dict[str, Any]:
        result = result or {}
        name = " ".join(str(result.get("name") or "").split())
        family_key = " ".join(str(result.get("family_key") or "").split())
        family_description = " ".join(
            str(result.get("family_description") or "").split()
        )
        thesis = " ".join(str(result.get("investable_thesis") or "").split())
        transmission = " ".join(str(result.get("portfolio_transmission") or "").split())
        expected_edge = " ".join(str(result.get("expected_edge") or "").split())
        policy = " ".join(str(result.get("policy") or "").split())
        operator_prompt = " ".join(str(result.get("operator_prompt") or "").split())
        no_launch_reason = " ".join(str(result.get("no_launch_reason") or "").split())
        profile = {
            "should_launch": bool(result.get("should_launch")),
            "name": name,
            "family_key": family_key,
            "family_description": family_description,
            "opportunity_type": str(result.get("opportunity_type") or "no_action"),
            "priority_score": ShadowService._bounded_float(
                result.get("priority_score"), 0.0, 1.0
            ),
            "signal_stage": " ".join(
                str(result.get("signal_stage") or "unclassified").split()
            ),
            "why_now": " ".join(str(result.get("why_now") or "").split()),
            "priced_in_assessment": " ".join(
                str(result.get("priced_in_assessment") or "uncertain").split()
            ),
            "investable_thesis": thesis,
            "portfolio_transmission": transmission,
            "expected_edge": expected_edge,
            "expected_relative_direction": str(
                result.get("expected_relative_direction") or "unscored"
            )
            .strip()
            .lower(),
            "leading_indicators": ShadowService._clean_string_list(
                result.get("leading_indicators")
            ),
            "lagging_confirmations": ShadowService._clean_string_list(
                result.get("lagging_confirmations")
            ),
            "evidence_refs": ShadowService._clean_string_list(
                result.get("evidence_refs"), limit=12
            ),
            "evidence_to_check": ShadowService._clean_string_list(
                result.get("evidence_to_check")
            ),
            "falsification_tests": ShadowService._clean_string_list(
                result.get("falsification_tests")
            ),
            "risk_controls": ShadowService._clean_string_list(
                result.get("risk_controls")
            ),
            "assumptions": ShadowService._clean_string_list(result.get("assumptions")),
            "uncertainties": ShadowService._clean_string_list(
                result.get("uncertainties")
            ),
            "policy": policy,
            "operator_prompt": operator_prompt,
            "horizon": str(result.get("horizon") or "adaptive"),
            "horizon_days": int(
                ShadowService._bounded_float(
                    (
                        result.get("horizon_days")
                        if result.get("horizon_days") is not None
                        else ShadowService._horizon_days(result.get("horizon"))
                    ),
                    1.0,
                    1825.0,
                )
            ),
            "no_launch_reason": no_launch_reason,
        }
        profile["trigger_reason"] = ShadowService._discovery_trigger_reason(profile)
        return profile

    @staticmethod
    def _discovery_profile_is_actionable(
        profile: dict[str, Any],
        *,
        available_evidence_refs: set[str] | None = None,
        portfolio_value: float | None = None,
    ) -> tuple[bool, str | None]:
        if not profile.get("should_launch"):
            return False, str(profile.get("no_launch_reason") or "discovery_declined")
        required_text = [
            "name",
            "signal_stage",
            "why_now",
            "priced_in_assessment",
            "investable_thesis",
            "portfolio_transmission",
            "expected_edge",
            "expected_relative_direction",
            "policy",
            "operator_prompt",
        ]
        missing_text = [
            key for key in required_text if not str(profile.get(key) or "").strip()
        ]
        if missing_text:
            return (
                False,
                f"missing_required_shadow_discovery_fields:{','.join(missing_text)}",
            )
        if profile.get("expected_relative_direction") not in {
            "outperform",
            "underperform",
        }:
            return False, "invalid_expected_relative_direction"
        required_lists = [
            "leading_indicators",
            "lagging_confirmations",
            "evidence_refs",
            "evidence_to_check",
            "falsification_tests",
            "risk_controls",
            "uncertainties",
        ]
        missing_lists = [key for key in required_lists if not profile.get(key)]
        if missing_lists:
            return (
                False,
                f"missing_required_shadow_discovery_lists:{','.join(missing_lists)}",
            )
        refs = {str(item) for item in profile.get("evidence_refs") or []}
        if available_evidence_refs is not None:
            unknown_refs = sorted(refs - available_evidence_refs)
            if unknown_refs:
                return False, "unknown_shadow_discovery_evidence_refs:" + ",".join(
                    unknown_refs[:4]
                )
        if not any(not ref.startswith("market:") for ref in refs):
            return (
                False,
                "shadow_discovery_requires_source_backed_evidence_beyond_market_tape",
            )
        if portfolio_value is not None and portfolio_value > 0:
            mentioned = ShadowService._money_mentions(profile)
            if mentioned and max(mentioned) > portfolio_value * 1.05:
                return (
                    False,
                    "shadow_discovery_notional_exceeds_point_in_time_portfolio",
                )
        return True, None

    @staticmethod
    def _money_mentions(profile: dict[str, Any]) -> list[float]:
        fields: list[Any] = [
            profile.get("portfolio_transmission"),
            profile.get("policy"),
            profile.get("operator_prompt"),
            *(profile.get("risk_controls") or []),
        ]
        values: list[float] = []
        pattern = re.compile(r"\$\s*([0-9]+(?:\.[0-9]+)?)\s*([kmb])?\b", re.I)
        scales = {"": 1.0, "k": 1_000.0, "m": 1_000_000.0, "b": 1_000_000_000.0}
        for field in fields:
            for amount, suffix in pattern.findall(str(field or "")):
                values.append(float(amount) * scales[str(suffix or "").lower()])
        return values

    @staticmethod
    def _discovery_trigger_reason(profile: dict[str, Any]) -> str:
        parts = [
            f"type={profile.get('opportunity_type')}",
            f"priority={float(profile.get('priority_score') or 0.0):.2f}",
            f"stage={profile.get('signal_stage')}",
            f"priced_in={profile.get('priced_in_assessment')}",
            f"why_now={profile.get('why_now')}",
            f"thesis={profile.get('investable_thesis')}",
            f"transmission={profile.get('portfolio_transmission')}",
            f"edge={profile.get('expected_edge')}",
        ]
        return "Autonomous shadow opportunity: " + " | ".join(
            part for part in parts if part and not part.endswith("=None")
        )

    @staticmethod
    def _clean_string_list(value: Any, *, limit: int = 6) -> list[str]:
        if not isinstance(value, list):
            return []
        cleaned: list[str] = []
        for item in value:
            text = " ".join(str(item or "").split())
            if text:
                cleaned.append(text[:320])
        return cleaned[:limit]

    @staticmethod
    def _bounded_float(value: Any, floor: float, ceiling: float) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return floor
        return max(floor, min(ceiling, number))

    @staticmethod
    def _experiment_run_lock(experiment_id: UUID) -> asyncio.Lock:
        lock = _EXPERIMENT_RUN_LOCKS.get(experiment_id)
        if lock is None:
            lock = asyncio.Lock()
            _EXPERIMENT_RUN_LOCKS[experiment_id] = lock
        return lock

    @classmethod
    def experiment_run_is_active(cls, experiment_id: UUID) -> bool:
        return cls._experiment_run_lock(experiment_id).locked()

    async def run_experiment(self, experiment_id: UUID) -> ShadowExperiment:
        lock = self._experiment_run_lock(experiment_id)
        if lock.locked():
            experiment = await self.get_experiment(experiment_id)
            if experiment is None:
                raise ValueError("Shadow experiment not found")
            return experiment
        async with lock:
            return await self._run_experiment_unlocked(experiment_id)

    async def _run_experiment_unlocked(self, experiment_id: UUID) -> ShadowExperiment:
        experiment = await self.get_experiment(experiment_id)
        if not experiment:
            raise ValueError("Shadow experiment not found")

        positions = await self._positions_with_stance()
        if not positions:
            experiment.run_status = "skipped"
            experiment.skip_reason = "No positions available to simulate."
            await self.session.commit()
            return experiment
        normalized_status = self.normalize_run_status(experiment.run_status)
        context = (experiment.initial_portfolio_state_json or {}).get(
            "experiment_context"
        ) or {}
        current_state = (
            experiment.final_portfolio_state_json
            or experiment.initial_portfolio_state_json
            or {}
        )
        run_details = current_state.get("run_details") or {}
        progress = run_details.get("progress") or {}
        pending_evidence_events = list(run_details.get("pending_evidence_events") or [])

        if normalized_status == "running" and not self._checkpoint_is_due(
            progress=progress,
            pending_evidence_events=pending_evidence_events,
            now=datetime.now(UTC),
        ):
            return experiment

        if normalized_status in {"queued", "failed", "skipped"}:
            await self._reset_experiment_state(experiment)
            current_state = experiment.initial_portfolio_state_json or {}
            run_details = current_state.get("run_details") or {}
            if pending_evidence_events:
                run_details["pending_evidence_events"] = pending_evidence_events
            progress = run_details.get("progress") or {}
            experiment.skip_reason = None
            experiment.run_status = "running"
            progress = {
                "phase": "monitoring",
                "step_count": 0,
                "target_steps": self._target_step_count(context.get("horizon_label")),
                "started_at": datetime.now(UTC).isoformat(),
                "last_updated_at": None,
            }
            run_details["progress"] = progress
            current_state["run_details"] = run_details
            experiment.final_portfolio_state_json = current_state
            await self.session.commit()
            await self.session.refresh(experiment)

        try:
            policy = experiment.policy_description.lower()
            guidance = await self._resolve_guidance(
                operator_prompt=context.get("operator_prompt")
                or self._operator_prompt_from_policy(
                    policy_description=experiment.policy_description,
                    trigger_reason=context.get("trigger_reason"),
                ),
                snapshot_summary=(experiment.initial_portfolio_state_json or {}).get(
                    "snapshot_summary"
                )
                or {},
                policy_description=experiment.policy_description,
            )
            now = datetime.now(UTC)
            prior_log = list(run_details.get("run_log") or [])
            checkpoint_log = list(run_details.get("checkpoint_log") or [])
            decision_history = list(run_details.get("decision_history") or [])
            initial_state = experiment.initial_portfolio_state_json or {}
            shadow_state = self._coerce_shadow_state(
                deepcopy(
                    current_state.get("shadow_state")
                    or initial_state.get("shadow_state")
                    or {}
                ),
                fallback_snapshot=initial_state,
            )
            paper_runtime = RuntimeSettingsStore.load().paper_trading
            paper_broker = self._paper_broker()
            step_number = int(progress.get("step_count") or 0) + 1
            reserve_target = float(shadow_state.get("cash") or 0.0) * max(
                0.0, min(1.0, guidance.get("cash_reserve_pct", 0.0))
            )
            ticker_by_security = {
                str(position.security_id): await self._security_ticker(
                    position.security_id
                )
                for position, _ in positions
            }
            quotes = (
                await MarketDataService(self.session).fetch_quotes(
                    list(ticker_by_security.values())
                )
                if paper_runtime.enabled
                else {}
            )
            pending_run_log: list[dict[str, Any]] = []
            if paper_runtime.enabled:
                shadow_state, pending_run_log = (
                    await self._process_pending_paper_orders(
                        experiment=experiment,
                        shadow_state=shadow_state,
                        quotes=quotes,
                        ticker_by_security=ticker_by_security,
                        paper_broker=paper_broker,
                        account_equity=Decimal(
                            str(self._shadow_portfolio_total(shadow_state, positions))
                        ),
                        cash_reserve=Decimal(str(reserve_target)),
                        step_number=step_number,
                    )
                )
            run_log: list[dict[str, Any]] = list(pending_run_log)
            buying_power = self._paper_buying_power(shadow_state)
            target_steps = int(
                progress.get("target_steps")
                or self._target_step_count(context.get("horizon_label"))
            )
            actual_start_total = self._snapshot_total_value(initial_state)
            checkpoint_plan = await self._build_checkpoint_plan(
                experiment=experiment,
                positions=positions,
                guidance=guidance,
                checkpoint_log=checkpoint_log,
                decision_history=decision_history,
                step_number=step_number,
            )
            checkpoint_used_provider = (
                checkpoint_plan.get("_checkpoint_source") == "provider"
            )
            planned_by_ticker = {
                str(item.get("ticker") or "").upper(): item
                for item in checkpoint_plan.get("decisions", [])
            }
            open_order_security_ids = {
                str(item.security_id)
                for item in (
                    await self.session.execute(
                        select(ShadowOrder).where(
                            ShadowOrder.experiment_id == experiment.id,
                            ShadowOrder.status == "accepted",
                        )
                    )
                )
                .scalars()
                .all()
            }
            prior_realization = self._prior_realization_summary(
                checkpoint_log=checkpoint_log
            )
            shadow_research = await self._maybe_run_shadow_research(
                experiment=experiment,
                checkpoint_plan=checkpoint_plan,
                step_number=step_number,
            )

            # Register active watchers from checkpoint plan
            for dec in checkpoint_plan.get("decisions", []):
                w_data = dec.get("active_watcher")
                if w_data:
                    try:
                        from investos.services.watcher import WatcherService

                        deadline = None
                        if w_data.get("deadline_hours"):
                            deadline = datetime.now(UTC) + timedelta(
                                hours=float(w_data["deadline_hours"])
                            )

                        await WatcherService(self.session).register_watcher(
                            source="shadow_experiment",
                            source_id=experiment.id,
                            ticker=dec.get("ticker"),
                            condition_type=w_data["condition_type"],
                            condition_params={"threshold": w_data.get("threshold")},
                            objective=f"Shadow: {dec.get('expected_outcome')}",
                            adjustment_plan=dec.get("exit_or_adjustment_trigger")
                            or "re-evaluate",
                            deadline=deadline,
                        )
                    except Exception:
                        import logging

                        logging.getLogger(__name__).exception(
                            "Failed to register active watcher from shadow checkpoint"
                        )

            for position, conclusion in positions:
                live_quantity = float(position.quantity or 0.0)
                ticker = ticker_by_security[str(position.security_id)]
                entity_name = await self._security_name(position.security_id)
                quote = quotes.get(ticker.upper()) or {}
                price = float(quote.get("price") or 0.0)
                quote_session = str(quote.get("session") or "unavailable")
                plan_decision = planned_by_ticker.get(ticker.upper())
                shadow_position = self._shadow_position_for_security(
                    shadow_state, position.security_id
                )
                shadow_quantity_before = float(shadow_position.get("quantity") or 0.0)

                if plan_decision is not None:
                    action = str(plan_decision.get("action") or "hold")
                    multiplier = max(
                        0.0,
                        min(
                            float(guidance.get("max_position_multiplier", 1.0)),
                            float(plan_decision.get("position_multiplier", 1.0)),
                        ),
                    )
                    rationale = str(
                        plan_decision.get("rationale")
                        or "Checkpoint plan selected this action."
                    )
                else:
                    action = "hold"
                    multiplier = 1.0
                    rationale = (
                        "No validated checkpoint decision was returned for this security; "
                        "the paper broker preserved the existing position."
                    )
                account_equity = self._shadow_portfolio_total(shadow_state, positions)
                desired_quantity = self._desired_shadow_trade_quantity(
                    current_quantity=shadow_quantity_before,
                    live_quantity=live_quantity,
                    action=action,
                    multiplier=multiplier,
                    target_weight_pct=(
                        None
                        if plan_decision is None
                        else plan_decision.get("target_weight_pct")
                    ),
                    account_equity=account_equity,
                    reference_price=price,
                )
                trade_quantity = 0.0
                order_status = "not_submitted"
                order_rejection_reason = None
                size_adjustments: list[str] = []
                if not paper_runtime.enabled:
                    order_status = "paper_execution_disabled"
                elif str(position.security_id) in open_order_security_ids:
                    order_status = "accepted"
                    rationale = f"{rationale} An earlier paper order is still awaiting an executable regular-session quote."
                elif action in {"buy", "sell"} and desired_quantity > 0:
                    executable_quantity, size_adjustments = (
                        paper_broker.estimate_quantity(
                            state=shadow_state,
                            security_id=position.security_id,
                            side=action,
                            desired_quantity=Decimal(str(desired_quantity)),
                            reference_price=Decimal(str(price)),
                            account_equity=Decimal(str(account_equity)),
                            cash_reserve=Decimal(str(reserve_target)),
                        )
                    )
                    quantity_to_submit = (
                        executable_quantity
                        if executable_quantity > 0
                        else Decimal(str(desired_quantity))
                    )
                    account_before = deepcopy(shadow_state)
                    execution = paper_broker.submit_market_order(
                        state=shadow_state,
                        request=PaperOrderRequest(
                            security_id=position.security_id,
                            ticker=ticker,
                            side=action,
                            quantity=quantity_to_submit,
                            reference_price=Decimal(str(price)),
                            quote_session=quote_session,
                            quote_time=self._quote_time(quote.get("quote_time")),
                            submitted_at=now,
                            rationale=rationale,
                            checkpoint_index=step_number,
                            client_order_id=(
                                f"shadow:{experiment.id}:{step_number}:{position.security_id}:{action}"
                            ),
                            evidence_refs=tuple(
                                (context.get("discovery_profile") or {}).get(
                                    "evidence_refs"
                                )
                                or []
                            ),
                            source_decision={
                                "model_action": action,
                                "model_position_multiplier": multiplier,
                                "model_target_weight_pct": (
                                    None
                                    if plan_decision is None
                                    else plan_decision.get("target_weight_pct")
                                ),
                                "desired_quantity": desired_quantity,
                                "deterministic_quantity": float(quantity_to_submit),
                                "size_adjustments": size_adjustments,
                                "observed_signal": (
                                    None
                                    if plan_decision is None
                                    else plan_decision.get("observed_signal")
                                ),
                            },
                        ),
                        account_equity=Decimal(str(account_equity)),
                        cash_reserve=Decimal(str(reserve_target)),
                    )
                    shadow_state = execution.state
                    await self._persist_paper_execution(
                        experiment=experiment,
                        security_id=position.security_id,
                        execution=execution,
                        account_before=account_before,
                    )
                    order_status = execution.status
                    order_rejection_reason = execution.rejection_reason
                    if execution.fill is not None:
                        trade_quantity = float(execution.fill["quantity"])
                        price = float(execution.fill["price"])
                    if size_adjustments:
                        rationale = f"{rationale} {' '.join(size_adjustments)}"
                    if execution.status == "rejected":
                        rationale = f"{rationale} Paper broker rejected the order: {execution.rejection_reason}."
                    elif execution.status == "accepted":
                        rationale = f"{rationale} Paper broker accepted the order and is waiting for a regular-session quote."
                buying_power = self._paper_buying_power(shadow_state)
                stance = None if conclusion is None else conclusion.current_stance
                confidence = None if conclusion is None else conclusion.confidence_band
                run_log.append(
                    {
                        "step_index": step_number,
                        "observed_at": now.isoformat(),
                        "ticker": ticker,
                        "entity_name": entity_name,
                        "action": action,
                        "quantity": round(trade_quantity, 4),
                        "price": round(price, 4),
                        "order_status": order_status,
                        "order_rejection_reason": order_rejection_reason,
                        "quote_session": quote_session,
                        "desired_quantity": round(desired_quantity, 4),
                        "size_adjustments": size_adjustments,
                        "rationale": rationale,
                        "observed_signal": (
                            None
                            if plan_decision is None
                            else plan_decision.get("observed_signal")
                        ),
                        "thesis_view": (
                            None
                            if plan_decision is None
                            else plan_decision.get("thesis_view")
                        ),
                        "expected_outcome": (
                            None
                            if plan_decision is None
                            else plan_decision.get("expected_outcome")
                        ),
                        "risk_guardrail": (
                            None
                            if plan_decision is None
                            else plan_decision.get("risk_guardrail")
                        ),
                        "stance": stance or "no_view",
                        "confidence_band": confidence or "very_low",
                        "thesis_summary": (
                            None
                            if conclusion is None
                            else conclusion.current_thesis_summary
                        ),
                        "actual_market_value": round(
                            float(position.market_value or 0.0), 2
                        ),
                        "shadow_quantity_before": round(shadow_quantity_before, 4),
                        "shadow_quantity_after": round(
                            float(
                                self._shadow_position_for_security(
                                    shadow_state, position.security_id
                                ).get("quantity")
                                or 0.0
                            ),
                            4,
                        ),
                        "actual_weight_pct": round(
                            (
                                0.0
                                if float(
                                    (current_state.get("snapshot_summary") or {}).get(
                                        "total_market_value"
                                    )
                                    or 0.0
                                )
                                <= 0
                                else (
                                    float(position.market_value or 0.0)
                                    / float(
                                        (
                                            current_state.get("snapshot_summary") or {}
                                        ).get("total_market_value")
                                        or 1.0
                                    )
                                )
                                * 100.0
                            ),
                            2,
                        ),
                        "post_trade_buying_power": round(buying_power, 2),
                    }
                )

            shadow_total_value = self._shadow_portfolio_total(shadow_state, positions)
            actual_total_value = self._actual_portfolio_total(positions)
            shadow_return = self._portfolio_total_return(
                current_total=shadow_total_value,
                starting_total=actual_start_total,
            )
            actual_return = self._portfolio_total_return(
                current_total=actual_total_value,
                starting_total=actual_start_total,
            )
            checkpoint_log.append(
                {
                    "step_index": step_number,
                    "captured_at": now.isoformat(),
                    "actual_return": round(actual_return, 6),
                    "shadow_return": round(shadow_return, 6),
                    "alpha": round(shadow_return - actual_return, 6),
                    "buying_power": round(buying_power, 2),
                    "guidance_mode": guidance.get("guidance_mode"),
                    "checkpoint_objective": checkpoint_plan.get("checkpoint_objective"),
                    "planned_posture": checkpoint_plan.get("planned_posture"),
                    "research_goal": checkpoint_plan.get("research_goal"),
                    "summary": self._checkpoint_summary(
                        step_index=step_number,
                        target_steps=target_steps,
                        actual_return=actual_return,
                        shadow_return=shadow_return,
                        alpha=shadow_return - actual_return,
                        run_log=run_log,
                    ),
                    "shadow_total_value": round(shadow_total_value, 2),
                    "actual_total_value": round(actual_total_value, 2),
                }
            )
            alpha = shadow_return - actual_return
            decision_history.append(
                {
                    "step_index": step_number,
                    "observed_at": now.isoformat(),
                    "checkpoint_objective": checkpoint_plan.get("checkpoint_objective"),
                    "portfolio_view": checkpoint_plan.get("portfolio_view"),
                    "planned_posture": checkpoint_plan.get("planned_posture"),
                    "why_now": checkpoint_plan.get("why_now"),
                    "research_goal": checkpoint_plan.get("research_goal"),
                    "monitoring_focus": checkpoint_plan.get("monitoring_focus") or [],
                    "what_would_change_mind": checkpoint_plan.get(
                        "what_would_change_mind"
                    )
                    or [],
                    "prior_realization": prior_realization,
                    "shadow_research": shadow_research,
                    "baseline_comparison": {
                        "shadow_return": round(shadow_return, 6),
                        "real_portfolio_return": round(actual_return, 6),
                        "alpha": round(alpha, 6),
                    },
                    "decisions": [
                        {
                            "ticker": entry.get("ticker"),
                            "entity_name": entry.get("entity_name"),
                            "action": entry.get("action"),
                            "observed_signal": entry.get("observed_signal"),
                            "thesis_view": entry.get("thesis_view"),
                            "expected_outcome": entry.get("expected_outcome"),
                            "risk_guardrail": entry.get("risk_guardrail"),
                            "rationale": entry.get("rationale"),
                        }
                        for entry in run_log
                    ],
                }
            )
            evidence_event_log = list(run_details.get("evidence_event_log") or [])
            consumed_evidence_events: list[dict[str, Any]] = []
            remaining_evidence_events = list(pending_evidence_events)
            if checkpoint_used_provider and pending_evidence_events:
                consumed_evidence_events = [
                    {
                        **event,
                        "consumed_at": now.isoformat(),
                        "checkpoint_index": step_number,
                    }
                    for event in pending_evidence_events
                ]
                evidence_event_log.extend(consumed_evidence_events)
                remaining_evidence_events = []
            elif remaining_evidence_events and step_number >= target_steps:
                target_steps = step_number + 1
            experiment.final_portfolio_state_json = await self._portfolio_snapshot(
                trigger_type=context.get("trigger_type", "manual"),
                trigger_reason=context.get("trigger_reason"),
                horizon_label=context.get("horizon_label"),
                initiated_by=context.get("initiated_by", "user"),
                policy_description=experiment.policy_description,
                operator_prompt=context.get("operator_prompt"),
                discovery_profile=context.get("discovery_profile"),
                subject_refs=context.get("subject_refs"),
            )
            experiment.final_portfolio_state_json["shadow_state"] = shadow_state
            progress = {
                "phase": "monitoring" if step_number < target_steps else "evaluation",
                "step_count": step_number,
                "target_steps": target_steps,
                "started_at": progress.get("started_at") or now.isoformat(),
                "last_updated_at": now.isoformat(),
                "next_checkpoint_at": (
                    self._next_checkpoint_at(
                        experiment=experiment,
                        target_steps=target_steps,
                        now=now,
                    ).isoformat()
                    if step_number < target_steps
                    else None
                ),
            }
            experiment.final_portfolio_state_json["run_details"] = {
                "guidance": guidance,
                "starting_buying_power": float(
                    (initial_state.get("shadow_state") or {}).get("cash") or 0.0
                ),
                "ending_buying_power": round(buying_power, 2),
                "reserve_target": round(reserve_target, 2),
                "paper_account": {
                    "provider": paper_runtime.provider,
                    "cash": round(float(shadow_state.get("cash") or 0.0), 2),
                    "cash_reserved": round(
                        float(shadow_state.get("cash_reserved") or 0.0), 2
                    ),
                    "buying_power": round(self._paper_buying_power(shadow_state), 2),
                    "position_count": sum(
                        1
                        for item in shadow_state.get("positions") or []
                        if float(item.get("quantity") or 0.0) > 0
                    ),
                    "slippage_bps": paper_runtime.slippage_bps,
                    "fee_per_order": paper_runtime.fee_per_order,
                    "max_buy_order_pct_equity": paper_runtime.max_buy_order_pct_equity,
                    "regular_session_only": paper_runtime.require_regular_session,
                },
                "starting_total_value": round(actual_start_total, 2),
                "ending_shadow_total_value": round(shadow_total_value, 2),
                "ending_actual_total_value": round(actual_total_value, 2),
                "run_log": prior_log + run_log,
                "checkpoint_log": checkpoint_log,
                "decision_history": decision_history,
                "pending_evidence_events": remaining_evidence_events,
                "evidence_event_log": evidence_event_log,
                "progress": progress,
            }
            if consumed_evidence_events:
                consumed_ids = {
                    UUID(str(event["event_id"]))
                    for event in consumed_evidence_events
                    if event.get("event_id")
                }
                evidence_rows = list(
                    (
                        await self.session.execute(
                            select(ShadowEvidenceEvent).where(
                                ShadowEvidenceEvent.id.in_(consumed_ids)
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                for evidence_event in evidence_rows:
                    evidence_event.status = "processed"
                    evidence_event.processing_detail = (
                        f"consumed_at_checkpoint:{step_number}"
                    )
                    evidence_event.processed_at = now
            experiment.final_portfolio_state_json["report"] = (
                self._build_experiment_report(
                    experiment=experiment,
                    result=self._transient_result(
                        experiment.id, shadow_return, actual_return, alpha
                    ),
                    run_log=prior_log + run_log,
                )
            )
            if step_number < target_steps:
                experiment.run_status = "running"
                experiment.completed_at = None
                await self.session.commit()
                await self.session.refresh(experiment)
                return experiment

            existing_result = (
                await self.session.execute(
                    select(ExperimentResult).where(
                        ExperimentResult.experiment_id == experiment.id
                    )
                )
            ).scalar_one_or_none()
            if existing_result is None:
                result = ExperimentResult(
                    experiment_id=experiment.id,
                    shadow_return=shadow_return,
                    actual_return=actual_return,
                    alpha=alpha,
                    max_drawdown=min(0.0, shadow_return),
                    sharpe_ratio=alpha if alpha != 0 else 0.0,
                    reasoning=(
                        f"{guidance.get('guidance_summary', 'Shadow experiment followed the stated policy.')} "
                        "The run used current deterministic position state, accepted conclusion stances, "
                        "configured remaining buying power, and multiple live checkpoints before completion."
                    ),
                )
                self.session.add(result)
            else:
                result = existing_result
                result.shadow_return = shadow_return
                result.actual_return = actual_return
                result.alpha = alpha
                result.max_drawdown = min(0.0, shadow_return)
                result.sharpe_ratio = alpha if alpha != 0 else 0.0
                result.reasoning = (
                    f"{guidance.get('guidance_summary', 'Shadow experiment followed the stated policy.')} "
                    "The run used current deterministic position state, accepted conclusion stances, "
                    "configured remaining buying power, and multiple live checkpoints before completion."
                )

            experiment.final_portfolio_state_json["run_details"]["progress"][
                "phase"
            ] = "completed"
            experiment.run_status = "completed"
            experiment.completed_at = now
            if experiment.family_id is not None:
                family_state = (
                    await self.session.execute(
                        select(ExperimentFamilyState).where(
                            ExperimentFamilyState.id == experiment.family_id
                        )
                    )
                ).scalar_one_or_none()
                if family_state is not None:
                    family_state.total_runs = int(family_state.total_runs or 0) + 1
                    family_state.last_run_at = now
            await self.session.commit()
            await self._ensure_experiment_lesson(experiment, result)
            await ReviewService(self.session).refresh_queue()
            await self.session.refresh(experiment)
            return experiment
        except Exception as exc:
            experiment.run_status = "failed"
            experiment.skip_reason = str(exc)
            experiment.completed_at = datetime.now(UTC)
            await self.session.commit()
            raise

    async def _reset_experiment_state(self, experiment: ShadowExperiment) -> None:
        for fill in (
            (
                await self.session.execute(
                    select(ShadowFill).where(ShadowFill.experiment_id == experiment.id)
                )
            )
            .scalars()
            .all()
        ):
            await self.session.delete(fill)
        for order in (
            (
                await self.session.execute(
                    select(ShadowOrder).where(
                        ShadowOrder.experiment_id == experiment.id
                    )
                )
            )
            .scalars()
            .all()
        ):
            await self.session.delete(order)
        for action in (
            (
                await self.session.execute(
                    select(ShadowAction).where(
                        ShadowAction.experiment_id == experiment.id
                    )
                )
            )
            .scalars()
            .all()
        ):
            await self.session.delete(action)
        existing_result = (
            await self.session.execute(
                select(ExperimentResult).where(
                    ExperimentResult.experiment_id == experiment.id
                )
            )
        ).scalar_one_or_none()
        if existing_result:
            await self.session.delete(existing_result)
        experiment.final_portfolio_state_json = None
        await self.session.flush()

    def _transient_result(
        self,
        experiment_id: UUID,
        shadow_return: float,
        actual_return: float,
        alpha: float,
    ) -> ExperimentResult:
        return ExperimentResult(
            experiment_id=experiment_id,
            shadow_return=shadow_return,
            actual_return=actual_return,
            alpha=alpha,
            max_drawdown=min(0.0, shadow_return),
            sharpe_ratio=alpha if alpha != 0 else 0.0,
            reasoning="Interim shadow checkpoint.",
        )

    def _target_step_count(self, horizon_label: str | None) -> int:
        normalized = (horizon_label or "adaptive").lower()
        if normalized == "short_term":
            return 3
        if normalized == "medium_term":
            return 5
        if normalized == "long_term":
            return 7
        return 4

    @staticmethod
    def _horizon_days(horizon_label: str | None) -> int:
        normalized = (horizon_label or "adaptive").lower()
        configured = {
            "short_term": settings.SHADOW_HORIZON_SHORT_DAYS,
            "adaptive": settings.SHADOW_HORIZON_ADAPTIVE_DAYS,
            "medium_term": settings.SHADOW_HORIZON_MEDIUM_DAYS,
            "long_term": settings.SHADOW_HORIZON_LONG_DAYS,
        }
        return max(1, int(configured.get(normalized, configured["adaptive"])))

    @staticmethod
    def _profile_horizon_days(profile: dict[str, Any] | None) -> int | None:
        if not profile or profile.get("horizon_days") is None:
            return None
        try:
            value = int(profile["horizon_days"])
        except (TypeError, ValueError):
            return None
        return max(1, min(value, 1825))

    @classmethod
    def _default_end_point(
        cls, *, start_point: datetime, horizon_label: str | None
    ) -> datetime:
        return start_point + timedelta(days=cls._horizon_days(horizon_label))

    @staticmethod
    def _checkpoint_is_due(
        *,
        progress: dict[str, Any],
        pending_evidence_events: list[dict[str, Any]],
        now: datetime,
    ) -> bool:
        if pending_evidence_events:
            return True
        value = progress.get("next_checkpoint_at")
        if not value:
            return True
        try:
            checkpoint_at = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return True
        if checkpoint_at.tzinfo is None:
            checkpoint_at = checkpoint_at.replace(tzinfo=UTC)
        return checkpoint_at <= now

    @staticmethod
    def _next_checkpoint_at(
        *, experiment: ShadowExperiment, target_steps: int, now: datetime
    ) -> datetime:
        observation_intervals = max(1, int(target_steps) - 1)
        horizon_seconds = max(
            0.0, (experiment.end_point - experiment.start_point).total_seconds()
        )
        interval_seconds = max(
            float(settings.SHADOW_MIN_CHECKPOINT_INTERVAL_SECONDS),
            horizon_seconds / observation_intervals,
        )
        return min(experiment.end_point, now + timedelta(seconds=interval_seconds))

    @staticmethod
    def _structured_llm_timeout_seconds() -> int:
        return max(15, int(settings.SHADOW_LLM_TIMEOUT_SECONDS))

    def _checkpoint_summary(
        self,
        *,
        step_index: int,
        target_steps: int,
        actual_return: float,
        shadow_return: float,
        alpha: float,
        run_log: list[dict[str, Any]],
    ) -> str:
        top_actions = (
            ", ".join(
                f"{entry.get('ticker')}: {entry.get('action')}"
                for entry in sorted(
                    run_log,
                    key=lambda item: abs(float(item.get("actual_market_value") or 0.0)),
                    reverse=True,
                )[:3]
            )
            or "no material actions"
        )
        return (
            f"Checkpoint {step_index} of {target_steps}: shadow return {shadow_return:+.2%}, "
            f"real portfolio {actual_return:+.2%}, alpha {alpha:+.2%}. "
            f"Most material actions this step: {top_actions}."
        )

    async def _build_checkpoint_plan(
        self,
        *,
        experiment: ShadowExperiment,
        positions: list[tuple[Position, ConclusionState | None]],
        guidance: dict[str, Any],
        checkpoint_log: list[dict[str, Any]],
        decision_history: list[dict[str, Any]],
        step_number: int,
    ) -> dict[str, Any]:
        context = (experiment.initial_portfolio_state_json or {}).get(
            "experiment_context"
        ) or {}
        snapshot = (experiment.initial_portfolio_state_json or {}).get(
            "snapshot_summary"
        ) or {}
        snapshot_total = float(snapshot.get("total_market_value") or 0.0) + float(
            snapshot.get("remaining_buying_power") or 0.0
        )
        runtime_state = (
            getattr(experiment, "final_portfolio_state_json", None)
            or experiment.initial_portfolio_state_json
            or {}
        )
        pending_evidence_events = list(
            (runtime_state.get("run_details") or {}).get("pending_evidence_events")
            or []
        )
        holdings_payload = []
        for position, conclusion in positions[:8]:
            holdings_payload.append(
                {
                    "ticker": await self._security_ticker(position.security_id),
                    "entity_name": await self._security_name(position.security_id),
                    "quantity": float(position.quantity or 0.0),
                    "market_value": float(position.market_value or 0.0),
                    "current_weight_pct": (
                        0.0
                        if snapshot_total <= 0
                        else (float(position.market_value or 0.0) / snapshot_total)
                        * 100.0
                    ),
                    "current_price": float(position.current_price or 0.0),
                    "stance": None if conclusion is None else conclusion.current_stance,
                    "confidence_band": (
                        None if conclusion is None else conclusion.confidence_band
                    ),
                    "thesis_summary": (
                        None
                        if conclusion is None
                        else conclusion.current_thesis_summary
                    ),
                }
            )
        fallback = {
            "_checkpoint_source": "fallback",
            "checkpoint_objective": self._policy_objective(
                policy_description=experiment.policy_description,
                guidance_mode=guidance.get("guidance_mode"),
                trigger_reason=context.get("trigger_reason"),
            ),
            "portfolio_view": "The shadow is monitoring the live portfolio and trying to improve on the user’s actual baseline without knowing future outcomes.",
            "planned_posture": guidance.get("guidance_summary")
            or "Maintain the selected policy posture.",
            "why_now": context.get("trigger_reason") or "manual review",
            "research_goal": None,
            "monitoring_focus": [
                "concentration risk",
                "thesis drift",
                "new contradictory evidence",
            ],
            "what_would_change_mind": [
                "fresh official evidence",
                "benchmark or macro confounders materially changing the thesis",
            ],
            "catalyst_tracker": "No LLM checkpoint plan was available; monitor stored active watches, fresh official evidence, and thesis-drift signals before changing the shadow posture.",
            "contingency_plan": "Keep the shadow close to the live baseline until a specific catalyst or contradiction justifies a measurable deviation.",
            "decisions": [
                {
                    "ticker": item["ticker"],
                    "entity_name": item["entity_name"],
                    "observed_signal": item.get("thesis_summary")
                    or "Live state remains broadly unchanged.",
                    "thesis_view": item.get("stance") or "no_view",
                    "action": "hold",
                    "position_multiplier": 1.0,
                    "target_weight_pct": item.get("current_weight_pct") or 0.0,
                    "expected_outcome": "Maintain exposure while waiting for stronger evidence or a clearer edge.",
                    "risk_guardrail": "Do not oversize uncertainty.",
                    "exit_or_adjustment_trigger": "Change only if new source-backed evidence, a watcher trigger, or a material price/estimate reaction changes the thesis.",
                    "active_watcher": None,
                    "rationale": "Fallback checkpoint plan kept the existing exposure.",
                }
                for item in holdings_payload[:5]
            ],
        }
        try:
            timeout_seconds = self._structured_llm_timeout_seconds()
            result = await asyncio.wait_for(
                call_llm_json(
                    system_prompt=(
                        "Analyze the captured paper account and propose the next checkpoint decisions. "
                        "State what the evidence supports, what should be tested next, and any intended exposure change. "
                        "Treat this as a real-time decision: you do not know future outcomes. "
                        "For each decision, provide a target portfolio weight as intent, not shares or dollars. "
                        "A deterministic paper broker independently derives and validates every order; do not claim that a trade filled. "
                        "Identify the adaptive catalyst, contingency plan, and specific exit or adjustment triggers."
                    ),
                    user_prompt=(
                        f"Experiment name: {experiment.name}\n"
                        f"Policy description: {experiment.policy_description}\n"
                        f"Experiment context: {context}\n"
                        f"Portfolio snapshot: {snapshot}\n"
                        f"Guidance: {guidance}\n"
                        f"Step number: {step_number}\n"
                        f"Prior checkpoints: {checkpoint_log[-3:]}\n"
                        f"Prior decision history: {decision_history[-2:]}\n"
                        f"New evidence events awaiting reassessment: {pending_evidence_events}\n"
                        f"Current holdings and thesis state: {holdings_payload}\n"
                    ),
                    schema=SHADOW_CHECKPOINT_SCHEMA,
                    timeout_seconds=timeout_seconds,
                ),
                timeout=timeout_seconds,
            )
            result["_checkpoint_source"] = "provider"
            return result
        except Exception as exc:
            import logging

            logging.getLogger(__name__).exception("Masked failure caught")
            return fallback

    async def _maybe_run_shadow_research(
        self,
        *,
        experiment: ShadowExperiment,
        checkpoint_plan: dict[str, Any],
        step_number: int,
    ) -> dict[str, Any] | None:
        research_goal = checkpoint_plan.get("research_goal")
        if not research_goal:
            return None
        try:
            from investos.services.research import ResearchService

            result = await ResearchService(self.session).run_ad_hoc_request(
                query=str(research_goal),
                title=f"Shadow research: {experiment.name} checkpoint {step_number}",
                metadata_json={
                    "requested_via": "shadow_checkpoint",
                    "experiment_id": str(experiment.id),
                    "step_number": step_number,
                },
                process_after_ingest=True,
            )
            return {
                "started": result.started,
                "reason": result.reason,
                "title": result.title,
                "query": result.query,
                "processed": result.processed,
                "evidence_id": (
                    None if result.evidence_id is None else str(result.evidence_id)
                ),
            }
        except Exception as exc:
            import logging

            logging.getLogger(__name__).exception("Masked failure caught")
            return {
                "started": False,
                "reason": "shadow_research_failed",
                "title": f"Shadow research: {experiment.name} checkpoint {step_number}",
                "query": str(research_goal),
                "processed": False,
                "evidence_id": None,
            }

    def _prior_realization_summary(
        self, *, checkpoint_log: list[dict[str, Any]]
    ) -> str | None:
        if not checkpoint_log:
            return None
        previous = checkpoint_log[-1]
        return (
            f"The previous checkpoint finished with real return {float(previous.get('actual_return') or 0.0):+.2%}, "
            f"shadow return {float(previous.get('shadow_return') or 0.0):+.2%}, and alpha {float(previous.get('alpha') or 0.0):+.2%}. "
            "The current checkpoint should learn from that gap."
        )

    async def cancel_paper_order(
        self,
        experiment_id: UUID,
        order_id: UUID,
    ) -> ShadowExperiment:
        experiment = await self.get_experiment(experiment_id)
        if experiment is None:
            raise ValueError("Shadow experiment not found")
        order = (
            await self.session.execute(
                select(ShadowOrder).where(
                    ShadowOrder.id == order_id,
                    ShadowOrder.experiment_id == experiment_id,
                )
            )
        ).scalar_one_or_none()
        if order is None:
            raise ValueError("Paper order not found")
        if order.status != "accepted":
            raise ValueError(
                f"Paper order is not cancelable from status '{order.status}'."
            )

        state_container = (
            experiment.final_portfolio_state_json
            or experiment.initial_portfolio_state_json
            or {}
        )
        shadow_state = self._coerce_shadow_state(
            deepcopy(state_container.get("shadow_state") or {}),
            fallback_snapshot=experiment.initial_portfolio_state_json or {},
        )
        shadow_state["cash_reserved"] = max(
            0.0,
            float(shadow_state.get("cash_reserved") or 0.0)
            - float(order.reserved_notional or 0.0),
        )
        order.status = "canceled"
        order.canceled_at = datetime.now(UTC)
        order.reserved_notional = 0.0
        order.account_snapshot_after_json = deepcopy(shadow_state)
        if experiment.final_portfolio_state_json:
            final_state = deepcopy(experiment.final_portfolio_state_json)
            final_state["shadow_state"] = shadow_state
            run_details = dict(final_state.get("run_details") or {})
            paper_account = dict(run_details.get("paper_account") or {})
            paper_account["cash_reserved"] = round(
                float(shadow_state.get("cash_reserved") or 0.0), 2
            )
            paper_account["buying_power"] = round(
                self._paper_buying_power(shadow_state), 2
            )
            run_details["paper_account"] = paper_account
            final_state["run_details"] = run_details
            experiment.final_portfolio_state_json = final_state
        else:
            initial_state = deepcopy(experiment.initial_portfolio_state_json or {})
            initial_state["shadow_state"] = shadow_state
            experiment.initial_portfolio_state_json = initial_state
        await self.session.commit()
        await self.session.refresh(experiment)
        return experiment

    async def submit_manual_paper_order(
        self,
        experiment_id: UUID,
        payload: ShadowOrderCreate,
    ) -> ShadowExperiment:
        experiment = await self.get_experiment(experiment_id)
        if experiment is None:
            raise ValueError("Shadow experiment not found")
        context = (experiment.initial_portfolio_state_json or {}).get(
            "experiment_context"
        ) or {}
        if context.get("execution_mode") != "manual":
            raise ValueError(
                "Manual paper orders require a manual paper account. Autonomous experiments are model-directed."
            )
        if self.experiment_run_is_active(experiment_id):
            raise ValueError("The paper account is currently processing another order.")
        runtime = RuntimeSettingsStore.load().paper_trading
        if not runtime.enabled or runtime.provider != "local_simulator":
            raise ValueError("The local paper broker is not enabled and ready.")

        ticker = payload.ticker.strip().upper()
        security = (
            await self.session.execute(
                select(Security).where(Security.ticker == ticker)
            )
        ).scalar_one_or_none()
        if security is None:
            raise ValueError(
                f"Ticker '{ticker}' is not in the tracked security catalog."
            )

        lock = self._experiment_run_lock(experiment_id)
        async with lock:
            state_container = (
                experiment.final_portfolio_state_json
                or experiment.initial_portfolio_state_json
                or {}
            )
            shadow_state = self._coerce_shadow_state(
                deepcopy(state_container.get("shadow_state") or {}),
                fallback_snapshot=experiment.initial_portfolio_state_json or {},
            )
            quote = await MarketDataService(self.session).get_live_price(ticker)
            positions = await self._positions_with_stance()
            account_equity = Decimal(
                str(self._shadow_portfolio_total(shadow_state, positions))
            )
            submitted_at = datetime.now(UTC)
            account_before = deepcopy(shadow_state)
            execution = self._paper_broker().submit_market_order(
                state=shadow_state,
                request=PaperOrderRequest(
                    security_id=security.id,
                    ticker=ticker,
                    side=payload.side,
                    quantity=Decimal(str(payload.quantity)),
                    reference_price=Decimal(str(quote.get("price") or 0)),
                    quote_session=str(quote.get("session") or "unavailable"),
                    quote_time=self._quote_time(quote.get("quote_time")),
                    submitted_at=submitted_at,
                    rationale=payload.rationale,
                    checkpoint_index=0,
                    client_order_id=f"manual-{uuid4()}",
                    source_decision={"source": "manual_order_ticket"},
                ),
                account_equity=account_equity,
                cash_reserve=Decimal("0"),
            )
            await self._persist_paper_execution(
                experiment=experiment,
                security_id=security.id,
                execution=execution,
                account_before=account_before,
            )
            self._store_paper_account_state(
                experiment=experiment,
                shadow_state=execution.state,
                state_container=state_container,
            )
            await self.session.commit()
            await self.session.refresh(experiment)
            return experiment

    async def rebuild_shadow_account_timelines(
        self,
        *,
        experiment_ids: set[UUID] | None = None,
    ) -> dict[str, int]:
        """Rebuild corrected paper histories from capture state and immutable fills."""
        query = (
            select(ShadowExperiment)
            .join(
                ShadowAccountEvent,
                ShadowAccountEvent.experiment_id == ShadowExperiment.id,
            )
            .where(ShadowAccountEvent.status == "needs_reconciliation")
            .distinct()
        )
        if experiment_ids is not None:
            if not experiment_ids:
                return {"attempted": 0, "rebuilt": 0, "failed": 0}
            query = query.where(ShadowExperiment.id.in_(experiment_ids))
        experiments = list((await self.session.execute(query)).scalars().all())
        attempted = rebuilt = failed = 0
        broker = self._paper_broker()
        for experiment in experiments:
            if self.experiment_run_is_active(experiment.id):
                continue
            attempted += 1
            lock = self._experiment_run_lock(experiment.id)
            async with lock:
                captured_at = self._shadow_account_capture_time(experiment)
                now = datetime.now(UTC)
                existing_events = list(
                    (
                        await self.session.execute(
                            select(ShadowAccountEvent).where(
                                ShadowAccountEvent.experiment_id == experiment.id
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                fills = (
                    await self.session.execute(
                        select(ShadowFill, ShadowOrder.ticker)
                        .join(ShadowOrder, ShadowOrder.id == ShadowFill.order_id)
                        .where(
                            ShadowFill.experiment_id == experiment.id,
                            ShadowFill.filled_at >= captured_at,
                        )
                        .order_by(ShadowFill.filled_at.asc(), ShadowFill.id.asc())
                    )
                ).all()
                initial_state = experiment.initial_portfolio_state_json or {}
                shadow_state = self._coerce_shadow_state(
                    deepcopy(initial_state.get("shadow_state") or {}),
                    fallback_snapshot=initial_state,
                )
                shadow_state["cash_reserved"] = 0.0
                relevant_security_ids = self._paper_security_ids(shadow_state)
                relevant_security_ids.update(fill.security_id for fill, _ in fills)
                event_rows: list[Any] = []
                if relevant_security_ids:
                    event_rows = (
                        await self.session.execute(
                            select(Transaction, Position.security_id, Security.ticker)
                            .join(Position, Position.id == Transaction.position_id)
                            .join(Security, Security.id == Position.security_id)
                            .where(
                                Position.security_id.in_(relevant_security_ids),
                                Transaction.action.in_(["split", "dividend"]),
                                Transaction.status == "settled",
                                Transaction.superseded_by_id.is_(None),
                                Transaction.executed_at >= captured_at,
                                Transaction.executed_at <= now,
                            )
                            .order_by(
                                Transaction.executed_at.asc(), Transaction.id.asc()
                            )
                        )
                    ).all()

                position_ids = {
                    transaction.position_id
                    for transaction, _, _ in event_rows
                    if transaction.position_id is not None
                }
                transaction_history: dict[UUID, list[Transaction]] = {}
                if position_ids:
                    history = list(
                        (
                            await self.session.execute(
                                select(Transaction)
                                .where(
                                    Transaction.position_id.in_(position_ids),
                                    Transaction.status == "settled",
                                    Transaction.superseded_by_id.is_(None),
                                    Transaction.executed_at <= now,
                                )
                                .order_by(
                                    Transaction.position_id.asc(),
                                    Transaction.executed_at.asc(),
                                    Transaction.id.asc(),
                                )
                            )
                        )
                        .scalars()
                        .all()
                    )
                    for transaction in history:
                        if transaction.position_id is not None:
                            transaction_history.setdefault(
                                transaction.position_id, []
                            ).append(transaction)

                timeline: list[tuple[datetime, int, str, str, Any]] = []
                for row in event_rows:
                    transaction = row[0]
                    timeline.append(
                        (
                            transaction.executed_at,
                            0,
                            str(transaction.id),
                            "event",
                            row,
                        )
                    )
                for fill, ticker in fills:
                    timeline.append(
                        (fill.filled_at, 1, str(fill.id), "fill", (fill, ticker))
                    )
                timeline.sort(key=lambda item: (item[0], item[1], item[2]))

                replayed_events: dict[
                    UUID, tuple[Transaction, UUID, str, Any, dict[str, Any], str]
                ] = {}
                failure_detail: str | None = None
                for _, _, _, kind, payload in timeline:
                    if kind == "event":
                        transaction, security_id, ticker = payload
                        before = deepcopy(shadow_state)
                        per_share, derivation = self._dividend_terms(
                            transaction=transaction,
                            history=transaction_history.get(
                                transaction.position_id, []
                            ),
                        )
                        execution = broker.apply_account_event(
                            state=shadow_state,
                            request=PaperAccountEventRequest(
                                security_id=security_id,
                                ticker=ticker,
                                event_type=transaction.action,
                                occurred_at=transaction.executed_at,
                                source_transaction_id=transaction.id,
                                split_ratio=(
                                    Decimal(str(transaction.quantity))
                                    if transaction.action == "split"
                                    else None
                                ),
                                dividend_per_share=per_share,
                                derivation=derivation,
                            ),
                        )
                        shadow_state = execution.state
                        replayed_events[transaction.id] = (
                            transaction,
                            security_id,
                            ticker.upper(),
                            execution,
                            before,
                            derivation,
                        )
                        continue

                    fill, ticker = payload
                    replay = broker.replay_recorded_fill(
                        state=shadow_state,
                        request=PaperRecordedFillRequest(
                            security_id=fill.security_id,
                            ticker=ticker,
                            side=fill.side,
                            quantity=Decimal(str(fill.quantity)),
                            price=Decimal(str(fill.price)),
                            gross_notional=Decimal(str(fill.gross_notional)),
                            fee=Decimal(str(fill.fee)),
                            filled_at=fill.filled_at,
                        ),
                    )
                    if replay.status != "applied":
                        failure_detail = (
                            f"Timeline rebuild stopped at paper fill {fill.id}: "
                            f"{replay.detail}"
                        )
                        break
                    shadow_state = replay.state

                if failure_detail is not None:
                    for event in existing_events:
                        if event.status == "needs_reconciliation":
                            event.detail = failure_detail
                    failed += 1
                    await self.session.commit()
                    continue

                accepted_reserved = sum(
                    Decimal(str(value or 0))
                    for value in (
                        await self.session.execute(
                            select(ShadowOrder.reserved_notional).where(
                                ShadowOrder.experiment_id == experiment.id,
                                ShadowOrder.status == "accepted",
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                shadow_state["cash_reserved"] = float(accepted_reserved)
                existing_by_source = {
                    event.source_transaction_id: event for event in existing_events
                }
                authoritative_ids = set(replayed_events)
                for event in existing_events:
                    if event.source_transaction_id not in authoritative_ids:
                        event.status = "superseded"
                        event.detail = (
                            "Removed from the current paper account by a deterministic "
                            "timeline rebuild after its source transaction was corrected."
                        )
                rebuilt_at = datetime.now(UTC)
                for source_id, values in replayed_events.items():
                    transaction, security_id, ticker, execution, before, derivation = (
                        values
                    )
                    event = existing_by_source.get(source_id)
                    if event is None:
                        event = ShadowAccountEvent(
                            experiment_id=experiment.id,
                            source_transaction_id=source_id,
                            security_id=security_id,
                            ticker=ticker,
                            event_type=transaction.action,
                            status=execution.status,
                            occurred_at=transaction.executed_at,
                            quantity_before=execution.quantity_before,
                            quantity_after=execution.quantity_after,
                            cash_before=execution.cash_before,
                            cash_after=execution.cash_after,
                            amount=execution.amount,
                            derivation=derivation,
                            account_snapshot_before_json=before,
                            account_snapshot_after_json=deepcopy(execution.state),
                        )
                        self.session.add(event)
                    event.security_id = security_id
                    event.ticker = ticker
                    event.event_type = transaction.action
                    event.status = execution.status
                    event.occurred_at = transaction.executed_at
                    event.applied_at = rebuilt_at
                    event.quantity_before = execution.quantity_before
                    event.quantity_after = execution.quantity_after
                    event.cash_before = execution.cash_before
                    event.cash_after = execution.cash_after
                    event.amount = execution.amount
                    event.derivation = derivation
                    event.detail = execution.detail
                    event.source_payload_json = self._shadow_account_event_payload(
                        transaction,
                        timeline_rebuilt_at=rebuilt_at,
                    )
                    event.account_snapshot_before_json = before
                    event.account_snapshot_after_json = deepcopy(execution.state)

                state_container = (
                    experiment.final_portfolio_state_json
                    or experiment.initial_portfolio_state_json
                    or {}
                )
                self._store_paper_account_state(
                    experiment=experiment,
                    shadow_state=shadow_state,
                    state_container=state_container,
                )
                rebuilt += 1
                await self.session.commit()
        return {"attempted": attempted, "rebuilt": rebuilt, "failed": failed}

    async def apply_portfolio_events_to_shadow_accounts(self) -> dict[str, int]:
        """Apply settled dividends and splits once to each active paper account."""
        now = datetime.now(UTC)
        experiments = list(
            (
                await self.session.execute(
                    select(ShadowExperiment).where(
                        ShadowExperiment.run_status.in_(
                            ["queued", "pending", "running", "manual"]
                        )
                    )
                )
            )
            .scalars()
            .all()
        )
        active_experiment_ids = {experiment.id for experiment in experiments}
        reconciliation_experiment_ids: set[UUID] = set()
        if active_experiment_ids:
            invalid_events = (
                await self.session.execute(
                    select(ShadowAccountEvent, Transaction)
                    .join(
                        Transaction,
                        Transaction.id == ShadowAccountEvent.source_transaction_id,
                    )
                    .where(
                        ShadowAccountEvent.experiment_id.in_(active_experiment_ids),
                        ShadowAccountEvent.status.not_in(
                            ["needs_reconciliation", "superseded"]
                        ),
                        (
                            (Transaction.status != "settled")
                            | Transaction.superseded_by_id.is_not(None)
                        ),
                    )
                )
            ).all()
            for event, transaction in invalid_events:
                event.status = "needs_reconciliation"
                event.detail = (
                    "The authoritative portfolio transaction was corrected after this "
                    "event was applied. Further automatic account events are paused for "
                    "this paper account until its timeline is rebuilt."
                )
                reconciliation_experiment_ids.add(event.experiment_id)
            existing_reconciliation_ids = set(
                (
                    await self.session.execute(
                        select(ShadowAccountEvent.experiment_id).where(
                            ShadowAccountEvent.experiment_id.in_(active_experiment_ids),
                            ShadowAccountEvent.status == "needs_reconciliation",
                        )
                    )
                )
                .scalars()
                .all()
            )
            reconciliation_experiment_ids.update(existing_reconciliation_ids)
            if invalid_events:
                await self.session.commit()
            rebuild = await self.rebuild_shadow_account_timelines(
                experiment_ids=reconciliation_experiment_ids
            )
            reconciliation_experiment_ids = set(
                (
                    await self.session.execute(
                        select(ShadowAccountEvent.experiment_id).where(
                            ShadowAccountEvent.experiment_id.in_(active_experiment_ids),
                            ShadowAccountEvent.status == "needs_reconciliation",
                        )
                    )
                )
                .scalars()
                .all()
            )
        else:
            rebuild = {"attempted": 0, "rebuilt": 0, "failed": 0}
        scanned = recorded = applied = 0
        for experiment in experiments:
            if experiment.id in reconciliation_experiment_ids:
                continue
            if self.experiment_run_is_active(experiment.id):
                continue
            lock = self._experiment_run_lock(experiment.id)
            async with lock:
                captured_at = self._shadow_account_capture_time(experiment)
                state_container = (
                    experiment.final_portfolio_state_json
                    or experiment.initial_portfolio_state_json
                    or {}
                )
                shadow_state = self._coerce_shadow_state(
                    deepcopy(state_container.get("shadow_state") or {}),
                    fallback_snapshot=experiment.initial_portfolio_state_json or {},
                )
                relevant_security_ids = self._paper_security_ids(shadow_state)
                if not relevant_security_ids:
                    continue
                rows = (
                    await self.session.execute(
                        select(Transaction, Position.security_id, Security.ticker)
                        .join(Position, Position.id == Transaction.position_id)
                        .join(Security, Security.id == Position.security_id)
                        .where(
                            Position.security_id.in_(relevant_security_ids),
                            Transaction.action.in_(["split", "dividend"]),
                            Transaction.status == "settled",
                            Transaction.superseded_by_id.is_(None),
                            Transaction.executed_at >= captured_at,
                            Transaction.executed_at <= now,
                        )
                        .order_by(Transaction.executed_at.asc(), Transaction.id.asc())
                    )
                ).all()
                scanned += len(rows)
                if not rows:
                    continue
                existing_ids = set(
                    (
                        await self.session.execute(
                            select(ShadowAccountEvent.source_transaction_id).where(
                                ShadowAccountEvent.experiment_id == experiment.id
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                candidates = [row for row in rows if row[0].id not in existing_ids]
                if not candidates:
                    continue

                position_ids = {
                    transaction.position_id
                    for transaction, _, _ in candidates
                    if transaction.position_id is not None
                }
                transaction_history: dict[UUID, list[Transaction]] = {}
                if position_ids:
                    history = list(
                        (
                            await self.session.execute(
                                select(Transaction)
                                .where(
                                    Transaction.position_id.in_(position_ids),
                                    Transaction.status == "settled",
                                    Transaction.superseded_by_id.is_(None),
                                    Transaction.executed_at <= now,
                                )
                                .order_by(
                                    Transaction.position_id.asc(),
                                    Transaction.executed_at.asc(),
                                    Transaction.id.asc(),
                                )
                            )
                        )
                        .scalars()
                        .all()
                    )
                    for transaction in history:
                        if transaction.position_id is not None:
                            transaction_history.setdefault(
                                transaction.position_id, []
                            ).append(transaction)

                broker = self._paper_broker()
                for transaction, security_id, ticker in candidates:
                    before = deepcopy(shadow_state)
                    per_share, derivation = self._dividend_terms(
                        transaction=transaction,
                        history=transaction_history.get(transaction.position_id, []),
                    )
                    execution = broker.apply_account_event(
                        state=shadow_state,
                        request=PaperAccountEventRequest(
                            security_id=security_id,
                            ticker=ticker,
                            event_type=transaction.action,
                            occurred_at=transaction.executed_at,
                            source_transaction_id=transaction.id,
                            split_ratio=(
                                Decimal(str(transaction.quantity))
                                if transaction.action == "split"
                                else None
                            ),
                            dividend_per_share=per_share,
                            derivation=derivation,
                        ),
                    )
                    shadow_state = execution.state
                    self.session.add(
                        ShadowAccountEvent(
                            experiment_id=experiment.id,
                            source_transaction_id=transaction.id,
                            security_id=security_id,
                            ticker=ticker.upper(),
                            event_type=transaction.action,
                            status=execution.status,
                            occurred_at=transaction.executed_at,
                            quantity_before=execution.quantity_before,
                            quantity_after=execution.quantity_after,
                            cash_before=execution.cash_before,
                            cash_after=execution.cash_after,
                            amount=execution.amount,
                            derivation=derivation,
                            detail=execution.detail,
                            source_payload_json=self._shadow_account_event_payload(
                                transaction
                            ),
                            account_snapshot_before_json=before,
                            account_snapshot_after_json=deepcopy(shadow_state),
                        )
                    )
                    recorded += 1
                    if execution.status == "applied":
                        applied += 1
                self._store_paper_account_state(
                    experiment=experiment,
                    shadow_state=shadow_state,
                    state_container=state_container,
                )
                await self.session.commit()
        return {
            "scanned": scanned,
            "recorded": recorded,
            "applied": applied,
            "reconciliation_required": len(reconciliation_experiment_ids),
            "timelines_rebuilt": rebuild["rebuilt"],
            "timeline_rebuild_failures": rebuild["failed"],
        }

    @staticmethod
    def _shadow_account_capture_time(experiment: ShadowExperiment) -> datetime:
        raw = (experiment.initial_portfolio_state_json or {}).get("captured_at")
        parsed = ShadowService._quote_time(raw)
        return parsed or experiment.created_at or experiment.start_point

    @staticmethod
    def _paper_security_ids(shadow_state: dict[str, Any]) -> set[UUID]:
        security_ids: set[UUID] = set()
        for item in shadow_state.get("positions") or []:
            try:
                security_ids.add(UUID(str(item.get("security_id"))))
            except (TypeError, ValueError):
                continue
        return security_ids

    @staticmethod
    def _shadow_account_event_payload(
        transaction: Transaction,
        *,
        timeline_rebuilt_at: datetime | None = None,
    ) -> dict[str, Any]:
        payload = {
            "action": transaction.action,
            "quantity": float(transaction.quantity or 0),
            "price": (None if transaction.price is None else float(transaction.price)),
            "executed_at": transaction.executed_at.isoformat(),
            "notes": transaction.notes,
            "provenance": transaction.provenance_json or {},
        }
        if timeline_rebuilt_at is not None:
            payload["timeline_rebuilt_at"] = timeline_rebuilt_at.isoformat()
        return payload

    @classmethod
    def _dividend_terms(
        cls,
        *,
        transaction: Transaction,
        history: list[Transaction],
    ) -> tuple[Decimal | None, str]:
        if transaction.action != "dividend":
            return None, "settled_transaction_split_ratio"
        explicit = cls._find_numeric_provenance_value(
            transaction.provenance_json or {},
            keys={"dividendpershare", "amountpershare", "cashpershare"},
        )
        if explicit is not None and explicit >= 0:
            return explicit, "explicit_per_share_provenance"
        live_quantity = cls._live_quantity_before_transaction(history, transaction)
        total_cash = Decimal(str(transaction.price or 0))
        if live_quantity > 0 and total_cash >= 0:
            return total_cash / live_quantity, "derived_from_settled_transaction_ledger"
        return None, "missing_per_share_dividend_basis"

    @classmethod
    def _find_numeric_provenance_value(
        cls,
        value: Any,
        *,
        keys: set[str],
    ) -> Decimal | None:
        if isinstance(value, dict):
            for key, item in value.items():
                normalized_key = re.sub(r"[^a-z0-9]", "", str(key).lower())
                if normalized_key in keys:
                    try:
                        candidate = Decimal(str(item))
                    except Exception:
                        candidate = Decimal("NaN")
                    if candidate.is_finite():
                        return candidate
                nested = cls._find_numeric_provenance_value(item, keys=keys)
                if nested is not None:
                    return nested
        elif isinstance(value, list):
            for item in value:
                nested = cls._find_numeric_provenance_value(item, keys=keys)
                if nested is not None:
                    return nested
        return None

    @staticmethod
    def _live_quantity_before_transaction(
        history: list[Transaction],
        target: Transaction,
    ) -> Decimal:
        quantity = Decimal("0")
        for transaction in history:
            if transaction.id == target.id:
                break
            amount = Decimal(str(transaction.quantity or 0))
            action = str(transaction.action or "").lower()
            if action == "buy":
                quantity += amount
            elif action == "sell":
                quantity = max(Decimal("0"), quantity - amount)
            elif action == "split" and amount > 0:
                quantity *= amount
            elif action == "expire":
                quantity = Decimal("0")
        return quantity

    async def refresh_pending_paper_orders(self) -> int:
        experiment_ids = list(
            (
                await self.session.execute(
                    select(ShadowOrder.experiment_id).where(
                        ShadowOrder.status == "accepted"
                    )
                )
            )
            .scalars()
            .all()
        )
        transitioned = 0
        for experiment_id in dict.fromkeys(experiment_ids):
            if self.experiment_run_is_active(experiment_id):
                continue
            lock = self._experiment_run_lock(experiment_id)
            async with lock:
                experiment = await self.get_experiment(experiment_id)
                if experiment is None:
                    continue
                pending = list(
                    (
                        await self.session.execute(
                            select(ShadowOrder).where(
                                ShadowOrder.experiment_id == experiment_id,
                                ShadowOrder.status == "accepted",
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                if not pending:
                    continue
                ticker_by_security = {
                    str(order.security_id): order.ticker.upper() for order in pending
                }
                quotes = await MarketDataService(self.session).fetch_quotes(
                    list(dict.fromkeys(ticker_by_security.values()))
                )
                state_container = (
                    experiment.final_portfolio_state_json
                    or experiment.initial_portfolio_state_json
                    or {}
                )
                shadow_state = self._coerce_shadow_state(
                    deepcopy(state_container.get("shadow_state") or {}),
                    fallback_snapshot=experiment.initial_portfolio_state_json or {},
                )
                positions = await self._positions_with_stance()
                account_equity = Decimal(
                    str(self._shadow_portfolio_total(shadow_state, positions))
                )
                reserve_target = Decimal(
                    str(
                        (state_container.get("run_details") or {}).get("reserve_target")
                        or 0
                    )
                )
                next_state, run_log = await self._process_pending_paper_orders(
                    experiment=experiment,
                    shadow_state=shadow_state,
                    quotes=quotes,
                    ticker_by_security=ticker_by_security,
                    paper_broker=self._paper_broker(),
                    account_equity=account_equity,
                    cash_reserve=reserve_target,
                    step_number=0,
                )
                self._store_paper_account_state(
                    experiment=experiment,
                    shadow_state=next_state,
                    state_container=state_container,
                    run_log=run_log,
                )
                transitioned += len(run_log)
                await self.session.commit()
        return transitioned

    async def refresh_paper_account_marks(self) -> int:
        experiments = list(
            (
                await self.session.execute(
                    select(ShadowExperiment).where(
                        ShadowExperiment.run_status.in_(["manual", "running"])
                    )
                )
            )
            .scalars()
            .all()
        )
        updated_positions = 0
        for experiment in experiments:
            if self.experiment_run_is_active(experiment.id):
                continue
            lock = self._experiment_run_lock(experiment.id)
            async with lock:
                state_container = (
                    experiment.final_portfolio_state_json
                    or experiment.initial_portfolio_state_json
                    or {}
                )
                shadow_state = self._coerce_shadow_state(
                    deepcopy(state_container.get("shadow_state") or {}),
                    fallback_snapshot=experiment.initial_portfolio_state_json or {},
                )
                held = [
                    item
                    for item in shadow_state.get("positions") or []
                    if float(item.get("quantity") or 0.0) > 0
                    and item.get("security_id")
                ]
                if not held:
                    continue
                security_ids = [UUID(str(item["security_id"])) for item in held]
                securities = list(
                    (
                        await self.session.execute(
                            select(Security).where(Security.id.in_(security_ids))
                        )
                    )
                    .scalars()
                    .all()
                )
                ticker_by_id = {
                    str(item.id): item.ticker.upper() for item in securities
                }
                quotes = await MarketDataService(self.session).fetch_quotes(
                    list(dict.fromkeys(ticker_by_id.values()))
                )
                for item in held:
                    ticker = ticker_by_id.get(str(item["security_id"]))
                    quote = quotes.get(ticker or "")
                    if not quote or not quote.get("price"):
                        continue
                    item["current_price"] = float(quote["price"])
                    item["marked_at"] = (
                        self._quote_time(quote.get("quote_time")) or datetime.now(UTC)
                    ).isoformat()
                    updated_positions += 1
                self._store_paper_account_state(
                    experiment=experiment,
                    shadow_state=shadow_state,
                    state_container=state_container,
                )
                await self.session.commit()
        return updated_positions

    async def serialize_experiment(
        self,
        experiment: ShadowExperiment,
        *,
        include_details: bool = True,
    ) -> dict[str, Any]:
        actions: list[ShadowAction] = []
        orders: list[ShadowOrder] = []
        fills: list[ShadowFill] = []
        account_events: list[ShadowAccountEvent] = []
        if include_details:
            actions = list(
                (
                    await self.session.execute(
                        select(ShadowAction).where(
                            ShadowAction.experiment_id == experiment.id
                        )
                    )
                )
                .scalars()
                .all()
            )
            orders = list(
                (
                    await self.session.execute(
                        select(ShadowOrder)
                        .where(ShadowOrder.experiment_id == experiment.id)
                        .order_by(ShadowOrder.submitted_at.asc())
                    )
                )
                .scalars()
                .all()
            )
            fills = list(
                (
                    await self.session.execute(
                        select(ShadowFill)
                        .where(ShadowFill.experiment_id == experiment.id)
                        .order_by(ShadowFill.filled_at.asc())
                    )
                )
                .scalars()
                .all()
            )
            account_events = list(
                (
                    await self.session.execute(
                        select(ShadowAccountEvent)
                        .where(ShadowAccountEvent.experiment_id == experiment.id)
                        .order_by(
                            ShadowAccountEvent.occurred_at.asc(),
                            ShadowAccountEvent.applied_at.asc(),
                        )
                    )
                )
                .scalars()
                .all()
            )
        result = (
            await self.session.execute(
                select(ExperimentResult).where(
                    ExperimentResult.experiment_id == experiment.id
                )
            )
        ).scalar_one_or_none()
        lesson = None
        if result is not None:
            lesson = (
                await self.session.execute(
                    select(Lesson)
                    .join(
                        LessonObservation,
                        LessonObservation.lesson_id == Lesson.id,
                    )
                    .where(LessonObservation.experiment_result_id == result.id)
                    .limit(1)
                )
            ).scalar_one_or_none()
            if lesson is None:
                lesson = (
                    await self.session.execute(
                        select(Lesson)
                        .where(Lesson.originating_experiment_result_id == result.id)
                        .limit(1)
                    )
                ).scalar_one_or_none()
        paper_positions = (
            await self._serialize_paper_positions(experiment) if include_details else []
        )
        context = (experiment.initial_portfolio_state_json or {}).get(
            "experiment_context"
        ) or {}
        snapshot_summary = (experiment.initial_portfolio_state_json or {}).get(
            "snapshot_summary"
        ) or {}
        current_state = (
            experiment.final_portfolio_state_json
            or experiment.initial_portfolio_state_json
            or {}
        )
        run_details = deepcopy(current_state.get("run_details") or {})
        display_shadow_state = self._paper_state_for_serialization(experiment)
        if include_details and display_shadow_state:
            run_details["paper_account"] = {
                **dict(run_details.get("paper_account") or {}),
                **self._paper_account_summary(display_shadow_state),
            }
        report = deepcopy(current_state.get("report") or {})
        if not include_details:
            run_details = {
                key: run_details[key]
                for key in ("progress", "guidance")
                if key in run_details
            }
            report = {
                key: report[key]
                for key in (
                    "opportunity_summary",
                    "policy_assessment",
                    "key_lesson",
                    "learning_summary",
                )
                if key in report
            }
        if lesson is not None:
            learning_summary = dict(report.get("learning_summary") or {})
            learning_summary.update(
                {
                    "maturity_status": lesson.maturity_status,
                    "confidence_score": float(lesson.confidence_score or 0.0),
                    "supporting_observations": int(lesson.supporting_observations or 0),
                    "contradicting_observations": int(
                        lesson.contradicting_observations or 0
                    ),
                    "neutral_observations": int(lesson.neutral_observations or 0),
                    "lesson_id": str(lesson.id),
                }
            )
            report["learning_summary"] = learning_summary
        return {
            "id": experiment.id,
            "name": experiment.name,
            "policy_description": experiment.policy_description,
            "start_point": experiment.start_point,
            "end_point": experiment.end_point,
            "trigger_type": context.get("trigger_type"),
            "trigger_reason": context.get("trigger_reason"),
            "horizon_label": context.get("horizon_label"),
            "initiated_by": context.get("initiated_by"),
            "execution_mode": context.get("execution_mode") or "autonomous",
            "discovery_profile": context.get("discovery_profile"),
            "operator_prompt": context.get("operator_prompt")
            or self._operator_prompt_from_policy(
                policy_description=experiment.policy_description,
                trigger_reason=context.get("trigger_reason"),
            ),
            "guidance_mode": (run_details.get("guidance") or {}).get("guidance_mode"),
            "guidance_summary": (run_details.get("guidance") or {}).get(
                "guidance_summary"
            ),
            "snapshot_summary": snapshot_summary,
            "run_details": run_details,
            "report": report,
            "run_status": self.normalize_run_status(experiment.run_status),
            "skip_reason": experiment.skip_reason,
            "created_at": experiment.created_at,
            "completed_at": experiment.completed_at,
            "actions": actions,
            "orders": orders,
            "fills": fills,
            "account_events": account_events,
            "paper_positions": paper_positions,
            "result": result,
            "lesson": lesson,
        }

    async def _portfolio_snapshot(
        self,
        *,
        trigger_type: str,
        trigger_reason: str | None,
        horizon_label: str | None,
        initiated_by: str,
        policy_description: str,
        operator_prompt: str | None,
        discovery_profile: dict[str, Any] | None = None,
        subject_refs: list[dict] | None = None,
    ) -> dict[str, Any]:
        positions = list(
            (
                await self.session.execute(
                    select(Position).where(
                        Position.list_type.in_(["holding", "watchlist", "considering"])
                    )
                )
            )
            .scalars()
            .all()
        )
        holdings = [
            position for position in positions if position.list_type == "holding"
        ]
        buying_power = RuntimeSettingsStore.load().portfolio.remaining_buying_power
        return {
            "captured_at": datetime.now(UTC).isoformat(),
            "experiment_context": {
                "trigger_type": trigger_type,
                "trigger_reason": trigger_reason,
                "horizon_label": horizon_label or "unspecified",
                "initiated_by": initiated_by,
                "policy_description": policy_description,
                "operator_prompt": operator_prompt,
                "discovery_profile": discovery_profile,
                "subject_refs": self._normalize_subject_refs(subject_refs),
            },
            "snapshot_summary": {
                "holding_count": len(holdings),
                "tracked_count": len(positions),
                "total_market_value": round(
                    sum(float(position.market_value or 0.0) for position in holdings), 2
                ),
                "remaining_buying_power": round(float(buying_power or 0.0), 2),
            },
            "positions": [
                {
                    "id": str(position.id),
                    "security_id": str(position.security_id),
                    "quantity": float(position.quantity or 0.0),
                    "avg_cost_basis": float(position.avg_cost_basis or 0.0),
                    "current_price": float(position.current_price or 0.0),
                    "market_value": float(position.market_value or 0.0),
                    "list_type": position.list_type,
                }
                for position in positions
            ],
            "shadow_state": {
                "cash": round(float(buying_power or 0.0), 2),
                "cash_reserved": 0.0,
                "positions": [
                    {
                        "security_id": str(position.security_id),
                        "quantity": float(position.quantity or 0.0),
                        "avg_cost_basis": float(position.avg_cost_basis or 0.0),
                        "current_price": float(position.current_price or 0.0),
                        "list_type": position.list_type,
                    }
                    for position in positions
                ],
            },
        }

    async def _positions_with_stance(
        self,
    ) -> list[tuple[Position, ConclusionState | None]]:
        positions = list(
            (
                await self.session.execute(
                    select(Position).where(
                        Position.list_type.in_(["holding", "watchlist", "considering"])
                    )
                )
            )
            .scalars()
            .all()
        )
        output: list[tuple[Position, ConclusionState | None]] = []
        for position in positions:
            conclusion = (
                await self.session.execute(
                    select(ConclusionState).where(
                        ConclusionState.subject_type == "position",
                        ConclusionState.subject_id == position.id,
                    )
                )
            ).scalar_one_or_none()
            output.append((position, conclusion))
        return output

    async def _ensure_experiment_lesson(
        self,
        experiment: ShadowExperiment,
        result: ExperimentResult,
    ) -> Lesson | None:
        lesson = None
        if experiment.family_id is not None:
            lesson = (
                await self.session.execute(
                    select(Lesson)
                    .where(
                        Lesson.experiment_family_id == experiment.family_id,
                        Lesson.lesson_type == "shadow_policy_outcome",
                    )
                    .order_by(Lesson.created_at.asc())
                    .limit(1)
                )
            ).scalar_one_or_none()
        if lesson is None:
            lesson = (
                await self.session.execute(
                    select(Lesson).where(
                        Lesson.originating_experiment_result_id == result.id
                    )
                )
            ).scalar_one_or_none()

        now = datetime.now(UTC)
        final_state = dict(experiment.final_portfolio_state_json or {})
        run_log = list(final_state.get("run_log") or [])
        final_state["report"] = self._build_experiment_report(
            experiment=experiment,
            result=result,
            run_log=run_log,
        )
        experiment.final_portfolio_state_json = final_state
        if lesson is None:
            lesson = Lesson(
                title=f"Shadow policy outcome: {experiment.name}",
                summary="This shadow outcome has not been aggregated yet.",
                lesson_type="shadow_policy_outcome",
                originating_experiment_result_id=result.id,
                experiment_family_id=experiment.family_id,
                maturity_status="provisional",
                confidence_score=0.0,
                last_validated_at=now,
                stale_after=now
                + timedelta(days=max(1, int(settings.SHADOW_LESSON_STALE_DAYS))),
                metadata_json={},
            )
            self.session.add(lesson)
            await self.session.flush()

        alpha = float(result.alpha or 0.0)
        material_alpha = max(0.0, float(settings.SHADOW_LESSON_MATERIAL_ALPHA))
        relationship = self._lesson_observation_relationship(
            alpha=alpha,
            material_alpha=material_alpha,
        )
        existing_observation = (
            await self.session.execute(
                select(LessonObservation).where(
                    LessonObservation.experiment_result_id == result.id
                )
            )
        ).scalar_one_or_none()
        observation_window_is_valid = self._experiment_has_valid_observation_window(
            experiment
        )
        observation_evidence = {
            "experiment_id": str(experiment.id),
            "experiment_name": experiment.name,
            "policy_description": experiment.policy_description,
            "eligible_for_calibration": observation_window_is_valid,
            "exclusion_reason": (
                None
                if observation_window_is_valid
                else "zero_duration_observation_window"
            ),
        }
        if existing_observation is None and observation_window_is_valid:
            report = (experiment.final_portfolio_state_json or {}).get("report") or {}
            observation = LessonObservation(
                lesson_id=lesson.id,
                experiment_result_id=result.id,
                relationship=relationship,
                observed_alpha=alpha,
                rationale=(
                    f"Shadow alpha was {alpha:+.2%} versus the real portfolio; "
                    f"the configured materiality boundary was {material_alpha:.2%}."
                ),
                evidence_json={
                    **observation_evidence,
                    "expected_outcome": (report.get("expected_outcome") or {}).get(
                        "summary"
                    ),
                    "actual_outcome": (report.get("actual_outcome") or {}).get(
                        "summary"
                    ),
                    "completed_at": (
                        experiment.completed_at.isoformat()
                        if experiment.completed_at
                        else None
                    ),
                },
                observed_at=experiment.completed_at or now,
            )
            self.session.add(observation)
            await self.session.flush()
        elif existing_observation is not None:
            existing_observation.evidence_json = {
                **(existing_observation.evidence_json or {}),
                **observation_evidence,
            }

        if experiment.family_id is not None:
            all_family_rows = (
                await self.session.execute(
                    select(ExperimentResult, ShadowExperiment)
                    .join(
                        ShadowExperiment,
                        ExperimentResult.experiment_id == ShadowExperiment.id,
                    )
                    .where(
                        ShadowExperiment.family_id == experiment.family_id,
                        ShadowExperiment.run_status == "completed",
                    )
                )
            ).all()
        else:
            all_family_rows = [(result, experiment)]

        family_rows = [
            (family_result, family_experiment)
            for family_result, family_experiment in all_family_rows
            if self._experiment_has_valid_observation_window(family_experiment)
        ]
        excluded_observations = len(all_family_rows) - len(family_rows)

        alphas = [float(item.alpha or 0.0) for item, _ in family_rows]
        minimum_runs = max(1, int(settings.SHADOW_LESSON_MIN_RUNS))
        validation_consistency = self._bounded_float(
            settings.SHADOW_LESSON_VALIDATION_CONSISTENCY,
            0.5,
            1.0,
        )
        learning_state = self._shadow_lesson_state(
            alphas=alphas,
            material_alpha=material_alpha,
            minimum_runs=minimum_runs,
            validation_consistency=validation_consistency,
        )
        supporting = learning_state["supporting"]
        contradicting = learning_state["contradicting"]
        neutral = learning_state["neutral"]
        consistency = learning_state["consistency"]
        average_alpha = learning_state["average_alpha"]
        confidence = learning_state["confidence"]
        maturity = learning_state["maturity"]
        direction = learning_state["direction"]
        family_state = (
            await self.session.get(ExperimentFamilyState, experiment.family_id)
            if experiment.family_id is not None
            else None
        )
        family_label = (
            family_state.description if family_state is not None else experiment.name
        )
        lesson.title = f"Shadow policy learning: {family_label}"
        lesson.summary = (
            f"Across {len(alphas)} eligible time-separated run{'s' if len(alphas) != 1 else ''}, "
            f"this policy family averaged {average_alpha:+.2%} alpha versus the real portfolio. "
            f"Observed outcomes: {supporting} supportive, {contradicting} contradictory, "
            f"and {neutral} immaterial. Maturity is {maturity}; "
            f"{excluded_observations} legacy zero-duration run{'s were' if excluded_observations != 1 else ' was'} "
            "retained for audit but excluded from calibration."
            if alphas
            else (
                "No eligible time-separated shadow run has completed for this policy family. "
                f"{excluded_observations} legacy zero-duration run"
                f"{'s were' if excluded_observations != 1 else ' was'} retained for audit but excluded from calibration."
            )
        )
        lesson.lesson_type = "shadow_policy_outcome"
        lesson.experiment_family_id = experiment.family_id
        lesson.maturity_status = maturity
        lesson.confidence_score = round(confidence, 4)
        lesson.supporting_observations = supporting
        lesson.contradicting_observations = contradicting
        lesson.neutral_observations = neutral
        lesson.last_validated_at = now
        lesson.stale_after = now + timedelta(
            days=max(1, int(settings.SHADOW_LESSON_STALE_DAYS))
        )
        lesson.metadata_json = {
            "learning_schema_version": _SHADOW_LEARNING_SCHEMA_VERSION,
            "direction": direction,
            "average_alpha": average_alpha,
            "consistency": consistency,
            "minimum_runs_for_validation": minimum_runs,
            "material_alpha": material_alpha,
            "family_name": (
                family_state.family_name if family_state is not None else None
            ),
            "latest_experiment_id": str(experiment.id),
            "eligible_observation_count": len(alphas),
            "excluded_zero_duration_observation_count": excluded_observations,
        }
        await self.session.commit()
        await self.session.refresh(lesson)
        return lesson

    @staticmethod
    def _lesson_observation_relationship(*, alpha: float, material_alpha: float) -> str:
        if alpha > material_alpha:
            return "supports_outperformance"
        if alpha < -material_alpha:
            return "supports_underperformance"
        return "neutral"

    @staticmethod
    def _experiment_has_valid_observation_window(
        experiment: ShadowExperiment,
    ) -> bool:
        start_point = getattr(experiment, "start_point", None)
        end_point = getattr(experiment, "end_point", None)
        return bool(start_point and end_point and end_point > start_point)

    @staticmethod
    def _shadow_lesson_state(
        *,
        alphas: list[float],
        material_alpha: float,
        minimum_runs: int,
        validation_consistency: float,
    ) -> dict[str, Any]:
        supporting = sum(item > material_alpha for item in alphas)
        contradicting = sum(item < -material_alpha for item in alphas)
        neutral = len(alphas) - supporting - contradicting
        directional = supporting + contradicting
        dominant_count = max(supporting, contradicting)
        consistency = dominant_count / directional if directional else 0.0
        average_alpha = sum(alphas) / len(alphas) if alphas else 0.0
        sample_progress = min(1.0, len(alphas) / max(1, minimum_runs))
        confidence = (
            consistency * sample_progress if directional else 0.1 * sample_progress
        )
        if len(alphas) < max(1, minimum_runs):
            maturity = "provisional"
        elif directional == 0 or consistency < validation_consistency:
            maturity = "mixed"
        else:
            maturity = "validated"
        direction = (
            "outperformance"
            if supporting > contradicting
            else "underperformance" if contradicting > supporting else "inconclusive"
        )
        return {
            "supporting": supporting,
            "contradicting": contradicting,
            "neutral": neutral,
            "consistency": consistency,
            "average_alpha": average_alpha,
            "confidence": confidence,
            "maturity": maturity,
            "direction": direction,
        }

    async def reconcile_shadow_learning(
        self, *, limit: int | None = None
    ) -> dict[str, int]:
        batch_size = max(
            1,
            int(
                settings.SHADOW_LESSON_RECONCILE_BATCH_SIZE if limit is None else limit
            ),
        )
        rows = (
            await self.session.execute(
                select(ShadowExperiment, ExperimentResult)
                .join(
                    ExperimentResult,
                    ExperimentResult.experiment_id == ShadowExperiment.id,
                )
                .where(ShadowExperiment.run_status == "completed")
                .order_by(ShadowExperiment.completed_at.asc())
                .limit(batch_size * 4)
            )
        ).all()
        reconciled = 0
        for experiment, result in rows:
            existing_observation = (
                await self.session.execute(
                    select(LessonObservation).where(
                        LessonObservation.experiment_result_id == result.id
                    )
                )
            ).scalar_one_or_none()
            if existing_observation is not None:
                existing_lesson = await self.session.get(
                    Lesson, existing_observation.lesson_id
                )
                metadata = (
                    existing_lesson.metadata_json if existing_lesson is not None else {}
                ) or {}
                if (
                    int(metadata.get("learning_schema_version") or 0)
                    >= _SHADOW_LEARNING_SCHEMA_VERSION
                ):
                    continue
            await self._ensure_experiment_lesson(experiment, result)
            reconciled += 1
            if reconciled >= batch_size:
                break
        return {"scanned": len(rows), "reconciled": reconciled}

    def _build_experiment_report(
        self,
        *,
        experiment: ShadowExperiment,
        result: ExperimentResult,
        run_log: list[dict[str, Any]],
    ) -> dict[str, Any]:
        context = (experiment.initial_portfolio_state_json or {}).get(
            "experiment_context"
        ) or {}
        discovery_profile = context.get("discovery_profile") or {}
        snapshot = (experiment.initial_portfolio_state_json or {}).get(
            "snapshot_summary"
        ) or {}
        run_details = (experiment.final_portfolio_state_json or {}).get(
            "run_details"
        ) or {}
        guidance = run_details.get("guidance") or {}
        decision_history = run_details.get("decision_history") or []
        alpha = float(result.alpha or 0.0)
        performance_direction = self._performance_direction(alpha)
        outperformed = performance_direction == "outperformed"
        strongest_actions = sorted(
            run_log,
            key=lambda entry: abs(float(entry.get("actual_market_value") or 0.0)),
            reverse=True,
        )[:5]
        ending_buying_power = float(
            (
                (
                    (experiment.final_portfolio_state_json or {}).get("run_details")
                    or {}
                ).get("ending_buying_power")
            )
            or 0.0
        )
        objective = self._policy_objective(
            policy_description=experiment.policy_description,
            guidance_mode=guidance.get("guidance_mode"),
            trigger_reason=context.get("trigger_reason"),
        )
        expected_summary = self._expected_outcome_summary(
            guidance_mode=guidance.get("guidance_mode"),
            shadow_return=float(result.shadow_return or 0.0),
            alpha=alpha,
            run_log=run_log,
        )
        actual_summary = self._actual_outcome_summary(
            actual_return=float(result.actual_return or 0.0),
            shadow_return=float(result.shadow_return or 0.0),
            alpha=alpha,
        )
        decision_summary = [
            {
                "step_index": entry.get("step_index"),
                "checkpoint_objective": entry.get("checkpoint_objective"),
                "planned_posture": entry.get("planned_posture"),
                "why_now": entry.get("why_now"),
                "research_goal": entry.get("research_goal"),
                "prior_realization": entry.get("prior_realization"),
                "baseline_comparison": entry.get("baseline_comparison"),
            }
            for entry in decision_history
        ]
        thesis_context = [
            {
                "ticker": entry.get("ticker"),
                "entity_name": entry.get("entity_name"),
                "stance": entry.get("stance"),
                "confidence_band": entry.get("confidence_band"),
                "thesis_summary": entry.get("thesis_summary"),
                "action": entry.get("action"),
                "rationale": entry.get("rationale"),
            }
            for entry in strongest_actions
        ]
        performance_summary = (
            "matched the real portfolio baseline at the displayed precision"
            if performance_direction == "matched"
            else f"{performance_direction} the real portfolio baseline by {alpha:+.2%}"
        )
        policy_assessment = (
            f"The experiment {performance_summary}. "
            f"Policy mode: {guidance.get('guidance_mode', 'follow_existing_policy')}. "
            f"Starting buying power was ${float(snapshot.get('remaining_buying_power') or 0.0):.2f} and ending buying power was "
            f"${ending_buying_power:.2f}."
        )
        if performance_direction == "outperformed":
            key_lesson = "The tested policy improved on the real book, so its action thresholds and sizing discipline are worth reviewing."
        elif performance_direction == "underperformed":
            key_lesson = "The tested policy lagged the real book, so its action thresholds or sizing logic were too weak for this state."
        else:
            key_lesson = "The tested policy matched the real book at the displayed precision, so this run does not yet support promoting or demoting it."
        open_questions = [
            "Did the accepted-state confidence justify the simulated action sizes?",
            "Would benchmark or macro context have changed the shadow policy choice?",
            "Should this policy become a reusable trigger family or stay a one-off review?",
        ]
        return {
            "trigger_summary": {
                "trigger_type": context.get("trigger_type"),
                "trigger_reason": context.get("trigger_reason"),
                "initiated_by": context.get("initiated_by"),
                "horizon_label": context.get("horizon_label"),
            },
            "opportunity_summary": discovery_profile,
            "baseline_summary": {
                "holding_count": snapshot.get("holding_count"),
                "total_market_value": snapshot.get("total_market_value"),
                "remaining_buying_power": snapshot.get("remaining_buying_power"),
            },
            "policy_summary": {
                "policy_description": experiment.policy_description,
                "operator_prompt": context.get("operator_prompt"),
                "guidance_mode": guidance.get("guidance_mode"),
                "guidance_summary": guidance.get("guidance_summary"),
                "objective": objective,
            },
            "expected_outcome": {
                "summary": expected_summary,
                "expected_shadow_return": float(result.shadow_return or 0.0),
                "expected_alpha_vs_baseline": alpha,
            },
            "actual_outcome": {
                "summary": actual_summary,
                "actual_portfolio_return": float(result.actual_return or 0.0),
                "shadow_portfolio_return": float(result.shadow_return or 0.0),
                "alpha_vs_real_portfolio": alpha,
                "outperformed_baseline": (
                    True
                    if performance_direction == "outperformed"
                    else False if performance_direction == "underperformed" else None
                ),
            },
            "outcome_summary": {
                "shadow_return": float(result.shadow_return or 0.0),
                "actual_return": float(result.actual_return or 0.0),
                "alpha": alpha,
                "max_drawdown": float(result.max_drawdown or 0.0),
                "reasoning": result.reasoning,
            },
            "learning_summary": {
                "baseline_used": "user_portfolio",
                "baseline_description": "The user’s actual portfolio performance over the same live state was used as the baseline.",
                "why_this_matters": (
                    "This run is one auditable observation. It can only mature into a policy lesson "
                    "after repeated family outcomes preserve both support and counterexamples."
                ),
                "lesson_direction": (
                    "observed_outperformance"
                    if performance_direction == "outperformed"
                    else (
                        "observed_underperformance"
                        if performance_direction == "underperformed"
                        else "observed_match"
                    )
                ),
            },
            "decision_history_summary": decision_summary,
            "policy_assessment": policy_assessment,
            "thesis_context": thesis_context,
            "key_lesson": key_lesson,
            "open_questions": open_questions,
        }

    async def _get_or_create_family_state(
        self,
        *,
        name: str,
        policy_description: str,
        trigger_reason: str | None,
        discovery_profile: dict[str, Any] | None = None,
    ) -> ExperimentFamilyState | None:
        family_name = self._family_name(
            name=name,
            policy_description=policy_description,
            trigger_reason=trigger_reason,
            discovery_profile=discovery_profile,
        )
        if not family_name:
            return None
        existing = (
            await self.session.execute(
                select(ExperimentFamilyState).where(
                    ExperimentFamilyState.family_name == family_name
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing
        family = ExperimentFamilyState(
            family_name=family_name,
            description=(
                str((discovery_profile or {}).get("family_description") or "").strip()
                or f"Shadow hypothesis family for {name}"
            ),
            trigger_conditions_json={
                "name": name,
                "policy_description": policy_description,
                "trigger_reason": trigger_reason,
                "family_key": (discovery_profile or {}).get("family_key"),
                "opportunity_type": (discovery_profile or {}).get("opportunity_type"),
            },
        )
        self.session.add(family)
        await self.session.flush()
        return family

    def _family_name(
        self,
        *,
        name: str,
        policy_description: str,
        trigger_reason: str | None,
        discovery_profile: dict[str, Any] | None = None,
    ) -> str:
        profile = discovery_profile or {}
        family_key = str(profile.get("family_key") or "").strip().lower()
        evidence_tickers = sorted(
            {
                str(item.get("ticker") or "").strip().upper()
                for item in (profile.get("evidence_snapshot") or [])
                if item.get("ticker")
            }
        )
        if family_key:
            raw = "|".join([family_key, *evidence_tickers]).lower()
        else:
            raw = " ".join(
                part
                for part in [name, policy_description, trigger_reason or ""]
                if part and part.strip()
            ).lower()
        normalized = re.sub(r"[^a-z0-9]+", "-", raw).strip("-")
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]
        return f"{normalized[:72]}-{digest}" if normalized else digest

    def _operator_prompt_from_policy(
        self, *, policy_description: str, trigger_reason: str | None
    ) -> str:
        policy = " ".join(policy_description.split())
        trigger = trigger_reason or "current thesis change"
        return (
            "Operate the forked portfolio like a real investor from the cloned live state. "
            f"Test this hypothesis over time: {policy} "
            "Decide whether to trim, add, hedge, rotate, or wait as new checkpoints arrive. "
            "Keep a decision trail, compare against the real portfolio baseline, and explain what would confirm or falsify the view. "
            f"Trigger context: {trigger}."
        )

    def _family_ready_for_new_run(self, family_state: ExperimentFamilyState) -> bool:
        if not family_state.is_active:
            return False
        if family_state.last_run_at is None:
            return True
        elapsed = datetime.now(UTC) - family_state.last_run_at
        return elapsed.days >= int(family_state.cooldown_days or 0)

    def _snapshot_total_value(self, snapshot: dict[str, Any]) -> float:
        summary = snapshot.get("snapshot_summary") or {}
        return float(summary.get("total_market_value") or 0.0) + float(
            summary.get("remaining_buying_power") or 0.0
        )

    def _coerce_shadow_state(
        self, state: dict[str, Any], *, fallback_snapshot: dict[str, Any]
    ) -> dict[str, Any]:
        if state and isinstance(state.get("positions"), list):
            return {
                "cash": float(state.get("cash") or 0.0),
                "cash_reserved": float(state.get("cash_reserved") or 0.0),
                "positions": [
                    {
                        "security_id": str(item.get("security_id")),
                        "quantity": float(item.get("quantity") or 0.0),
                        "avg_cost_basis": float(item.get("avg_cost_basis") or 0.0),
                        "current_price": float(item.get("current_price") or 0.0),
                        "marked_at": item.get("marked_at"),
                        "list_type": item.get("list_type") or "holding",
                    }
                    for item in state.get("positions") or []
                    if item.get("security_id")
                ],
            }
        return {
            "cash": float(
                (
                    (fallback_snapshot.get("snapshot_summary") or {}).get(
                        "remaining_buying_power"
                    )
                )
                or 0.0
            ),
            "cash_reserved": 0.0,
            "positions": [
                {
                    "security_id": str(item.get("security_id")),
                    "quantity": float(item.get("quantity") or 0.0),
                    "avg_cost_basis": float(item.get("avg_cost_basis") or 0.0),
                    "current_price": float(item.get("current_price") or 0.0),
                    "marked_at": item.get("marked_at"),
                    "list_type": item.get("list_type") or "holding",
                }
                for item in fallback_snapshot.get("positions") or []
                if item.get("security_id")
            ],
        }

    def _shadow_position_for_security(
        self, shadow_state: dict[str, Any], security_id: UUID
    ) -> dict[str, Any]:
        key = str(security_id)
        for item in shadow_state.get("positions") or []:
            if str(item.get("security_id")) == key:
                return item
        position = {
            "security_id": key,
            "quantity": 0.0,
            "avg_cost_basis": 0.0,
            "current_price": 0.0,
            "marked_at": None,
            "list_type": "holding",
        }
        shadow_state.setdefault("positions", []).append(position)
        return position

    @staticmethod
    def _desired_shadow_trade_quantity(
        *,
        current_quantity: float,
        live_quantity: float,
        action: str,
        multiplier: float,
        target_weight_pct: float | None = None,
        account_equity: float = 0.0,
        reference_price: float = 0.0,
    ) -> float:
        if target_weight_pct is not None and account_equity > 0 and reference_price > 0:
            target_quantity = (
                account_equity
                * max(0.0, min(100.0, float(target_weight_pct)))
                / 100.0
                / reference_price
            )
            if action == "buy":
                return max(0.0, target_quantity - current_quantity)
            if action == "sell":
                return max(0.0, current_quantity - target_quantity)
        if action == "buy":
            anchor = max(current_quantity, live_quantity)
            target_quantity = anchor * max(1.0, float(multiplier or 1.0))
            return max(0.0, target_quantity - current_quantity)
        if action == "sell":
            trim_fraction = max(0.0, min(1.0, float(multiplier or 0.0)))
            return max(0.0, current_quantity * trim_fraction)
        return 0.0

    @staticmethod
    def _paper_buying_power(shadow_state: dict[str, Any]) -> float:
        return max(
            0.0,
            float(shadow_state.get("cash") or 0.0)
            - float(shadow_state.get("cash_reserved") or 0.0),
        )

    @staticmethod
    def _quote_time(value: Any) -> datetime | None:
        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=UTC)
        if isinstance(value, str) and value.strip():
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
            except ValueError:
                return None
        return None

    @staticmethod
    def _paper_broker() -> LocalPaperBroker:
        runtime = RuntimeSettingsStore.load().paper_trading
        return LocalPaperBroker(
            PaperBrokerPolicy(
                slippage_bps=Decimal(str(runtime.slippage_bps)),
                fee_per_order=Decimal(str(runtime.fee_per_order)),
                max_buy_order_pct_equity=Decimal(str(runtime.max_buy_order_pct_equity)),
                allow_fractional=runtime.allow_fractional,
                require_regular_session=runtime.require_regular_session,
            )
        )

    def _paper_account_summary(self, shadow_state: dict[str, Any]) -> dict[str, Any]:
        runtime = RuntimeSettingsStore.load().paper_trading
        positions = shadow_state.get("positions") or []
        market_value = sum(
            float(item.get("quantity") or 0.0) * float(item.get("current_price") or 0.0)
            for item in positions
        )
        cash = float(shadow_state.get("cash") or 0.0)
        return {
            "provider": runtime.provider,
            "cash": round(cash, 2),
            "cash_reserved": round(float(shadow_state.get("cash_reserved") or 0.0), 2),
            "buying_power": round(self._paper_buying_power(shadow_state), 2),
            "market_value": round(market_value, 2),
            "equity": round(cash + market_value, 2),
            "position_count": sum(
                1 for item in positions if float(item.get("quantity") or 0.0) > 0
            ),
            "slippage_bps": runtime.slippage_bps,
            "fee_per_order": runtime.fee_per_order,
            "max_buy_order_pct_equity": runtime.max_buy_order_pct_equity,
            "regular_session_only": runtime.require_regular_session,
        }

    async def _serialize_paper_positions(
        self,
        experiment: ShadowExperiment,
    ) -> list[dict[str, Any]]:
        state_container = self._paper_state_for_serialization(experiment)
        positions = [
            item
            for item in state_container.get("positions") or []
            if float(item.get("quantity") or 0.0) > 0 and item.get("security_id")
        ]
        if not positions:
            return []
        security_ids = [UUID(str(item["security_id"])) for item in positions]
        missing_ids = [
            security_id
            for security_id in security_ids
            if str(security_id) not in self._security_ticker_cache
        ]
        if missing_ids:
            securities = list(
                (
                    await self.session.execute(
                        select(Security).where(Security.id.in_(missing_ids))
                    )
                )
                .scalars()
                .all()
            )
            self._security_ticker_cache.update(
                {str(item.id): item.ticker for item in securities}
            )
        ticker_by_id = self._security_ticker_cache
        total_equity = float(state_container.get("cash") or 0.0)
        total_equity += sum(
            float(item.get("quantity") or 0.0) * float(item.get("current_price") or 0.0)
            for item in positions
        )
        output: list[dict[str, Any]] = []
        for item in positions:
            quantity = float(item.get("quantity") or 0.0)
            avg_cost = float(item.get("avg_cost_basis") or 0.0)
            current_price = float(item.get("current_price") or 0.0)
            market_value = quantity * current_price
            output.append(
                {
                    "security_id": item["security_id"],
                    "ticker": ticker_by_id.get(str(item["security_id"]), "UNKNOWN"),
                    "quantity": quantity,
                    "avg_cost_basis": avg_cost,
                    "current_price": current_price,
                    "market_value": market_value,
                    "unrealized_pnl": market_value - (quantity * avg_cost),
                    "weight_pct": (
                        0.0 if total_equity <= 0 else market_value / total_equity * 100
                    ),
                    "marked_at": self._quote_time(item.get("marked_at")),
                }
            )
        return sorted(output, key=lambda item: item["market_value"], reverse=True)

    def _paper_state_for_serialization(
        self,
        experiment: ShadowExperiment,
    ) -> dict[str, Any]:
        initial_state = experiment.initial_portfolio_state_json or {}
        current_state = experiment.final_portfolio_state_json or initial_state
        shadow_state = self._coerce_shadow_state(
            deepcopy(current_state.get("shadow_state") or {}),
            fallback_snapshot=initial_state,
        )
        fallback_by_security = {
            str(item.get("security_id")): item
            for item in initial_state.get("positions") or []
            if item.get("security_id")
        }
        captured_at = initial_state.get("captured_at")
        for item in shadow_state.get("positions") or []:
            fallback = fallback_by_security.get(str(item.get("security_id"))) or {}
            if float(item.get("current_price") or 0.0) <= 0:
                item["current_price"] = float(fallback.get("current_price") or 0.0)
            if not item.get("marked_at"):
                item["marked_at"] = captured_at
        return shadow_state

    def _store_paper_account_state(
        self,
        *,
        experiment: ShadowExperiment,
        shadow_state: dict[str, Any],
        state_container: dict[str, Any],
        run_log: list[dict[str, Any]] | None = None,
    ) -> None:
        final_state = deepcopy(state_container)
        final_state["shadow_state"] = shadow_state
        run_details = dict(final_state.get("run_details") or {})
        if run_log:
            run_details["run_log"] = list(run_details.get("run_log") or []) + run_log
        run_details["paper_account"] = self._paper_account_summary(shadow_state)
        final_state["run_details"] = run_details
        experiment.final_portfolio_state_json = final_state

    async def _persist_paper_execution(
        self,
        *,
        experiment: ShadowExperiment,
        security_id: UUID,
        execution: PaperBrokerExecution,
        account_before: dict[str, Any],
        existing_order: ShadowOrder | None = None,
    ) -> ShadowOrder:
        payload = execution.order
        order = existing_order or ShadowOrder(
            experiment_id=experiment.id,
            security_id=security_id,
            ticker=str(payload["ticker"]),
            client_order_id=str(payload["client_order_id"]),
            provider=RuntimeSettingsStore.load().paper_trading.provider,
            side=str(payload["side"]),
            order_type=str(payload["order_type"]),
            time_in_force=str(payload["time_in_force"]),
            status=str(payload["status"]),
            requested_quantity=float(payload["requested_quantity"]),
            filled_quantity=float(payload["filled_quantity"]),
            reference_price=float(payload["reference_price"]),
            filled_avg_price=payload.get("filled_avg_price"),
            reserved_notional=float(payload.get("reserved_notional") or 0.0),
            quote_session=payload.get("quote_session"),
            quote_time=payload.get("quote_time"),
            submitted_at=payload["submitted_at"],
            accepted_at=payload.get("accepted_at"),
            filled_at=payload.get("filled_at"),
            rejection_reason=(
                execution.rejection_reason if execution.status == "rejected" else None
            ),
            rationale=str(payload.get("rationale") or "Paper order submitted."),
            checkpoint_index=int(payload.get("checkpoint_index") or 0),
            evidence_refs_json=list(payload.get("evidence_refs") or []),
            source_decision_json=dict(payload.get("source_decision") or {}),
            account_snapshot_before_json=deepcopy(account_before),
            account_snapshot_after_json=deepcopy(execution.state),
        )
        if existing_order is not None:
            order.status = str(payload["status"])
            order.filled_quantity = float(payload["filled_quantity"])
            order.reference_price = float(payload["reference_price"])
            order.filled_avg_price = payload.get("filled_avg_price")
            order.reserved_notional = float(payload.get("reserved_notional") or 0.0)
            order.quote_session = payload.get("quote_session")
            order.quote_time = payload.get("quote_time")
            order.accepted_at = payload.get("accepted_at") or order.accepted_at
            order.filled_at = payload.get("filled_at")
            order.rejection_reason = (
                execution.rejection_reason if execution.status == "rejected" else None
            )
            order.account_snapshot_before_json = deepcopy(account_before)
            order.account_snapshot_after_json = deepcopy(execution.state)
        self.session.add(order)
        await self.session.flush()

        fill = execution.fill
        if fill is not None:
            existing_fill = (
                await self.session.execute(
                    select(ShadowFill).where(ShadowFill.order_id == order.id)
                )
            ).scalar_one_or_none()
            if existing_fill is None:
                self.session.add(
                    ShadowFill(
                        order_id=order.id,
                        experiment_id=experiment.id,
                        security_id=security_id,
                        side=str(fill["side"]),
                        quantity=float(fill["quantity"]),
                        price=float(fill["price"]),
                        gross_notional=float(fill["gross_notional"]),
                        fee=float(fill["fee"]),
                        slippage_bps=float(fill["slippage_bps"]),
                        filled_at=fill["filled_at"],
                        quote_time=fill.get("quote_time"),
                        quote_session=fill.get("quote_session"),
                        cash_after=float(fill["cash_after"]),
                        position_quantity_after=float(fill["position_quantity_after"]),
                    )
                )
                self.session.add(
                    ShadowAction(
                        experiment_id=experiment.id,
                        action=str(fill["side"]),
                        security_id=security_id,
                        quantity=float(fill["quantity"]),
                        price=float(fill["price"]),
                        simulated_timestamp=fill["filled_at"],
                        rationale=order.rationale,
                    )
                )
        return order

    async def _process_pending_paper_orders(
        self,
        *,
        experiment: ShadowExperiment,
        shadow_state: dict[str, Any],
        quotes: dict[str, dict[str, Any]],
        ticker_by_security: dict[str, str],
        paper_broker: LocalPaperBroker,
        account_equity: Decimal,
        cash_reserve: Decimal,
        step_number: int,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        pending = list(
            (
                await self.session.execute(
                    select(ShadowOrder).where(
                        ShadowOrder.experiment_id == experiment.id,
                        ShadowOrder.status == "accepted",
                    )
                )
            )
            .scalars()
            .all()
        )
        run_log: list[dict[str, Any]] = []
        state = shadow_state
        for order in pending:
            ticker = ticker_by_security.get(
                str(order.security_id)
            ) or await self._security_ticker(order.security_id)
            quote = quotes.get(ticker.upper()) or {}
            if not quote.get("price"):
                continue
            account_before = deepcopy(state)
            execution = paper_broker.submit_market_order(
                state=state,
                request=PaperOrderRequest(
                    security_id=order.security_id,
                    ticker=ticker,
                    side=order.side,
                    quantity=Decimal(str(order.requested_quantity)),
                    reference_price=Decimal(str(quote["price"])),
                    quote_session=str(quote.get("session") or "unavailable"),
                    quote_time=self._quote_time(quote.get("quote_time")),
                    submitted_at=order.submitted_at,
                    rationale=order.rationale,
                    checkpoint_index=order.checkpoint_index,
                    client_order_id=order.client_order_id,
                    evidence_refs=tuple(order.evidence_refs_json or []),
                    source_decision=dict(order.source_decision_json or {}),
                ),
                account_equity=account_equity,
                cash_reserve=cash_reserve,
                existing_reserved_notional=Decimal(str(order.reserved_notional or 0)),
            )
            state = execution.state
            await self._persist_paper_execution(
                experiment=experiment,
                security_id=order.security_id,
                execution=execution,
                account_before=account_before,
                existing_order=order,
            )
            if execution.status == "accepted":
                continue
            fill = execution.fill or {}
            run_log.append(
                {
                    "step_index": step_number,
                    "observed_at": datetime.now(UTC).isoformat(),
                    "ticker": ticker,
                    "entity_name": await self._security_name(order.security_id),
                    "action": order.side,
                    "quantity": round(float(fill.get("quantity") or 0.0), 4),
                    "price": round(
                        float(fill.get("price") or quote.get("price") or 0.0), 4
                    ),
                    "order_status": execution.status,
                    "order_rejection_reason": execution.rejection_reason,
                    "quote_session": quote.get("session"),
                    "desired_quantity": float(order.requested_quantity or 0.0),
                    "size_adjustments": (order.source_decision_json or {}).get(
                        "size_adjustments"
                    )
                    or [],
                    "rationale": (
                        order.rationale
                        if execution.status == "filled"
                        else f"{order.rationale} Paper broker rejected the waiting order: {execution.rejection_reason}."
                    ),
                    "observed_signal": (order.source_decision_json or {}).get(
                        "observed_signal"
                    ),
                    "thesis_view": None,
                    "expected_outcome": None,
                    "risk_guardrail": None,
                    "stance": "not_recomputed",
                    "confidence_band": "not_recomputed",
                    "thesis_summary": None,
                    "actual_market_value": None,
                    "shadow_quantity_before": None,
                    "shadow_quantity_after": float(
                        fill.get("position_quantity_after") or 0.0
                    ),
                    "actual_weight_pct": None,
                    "post_trade_buying_power": round(
                        self._paper_buying_power(state), 2
                    ),
                }
            )
        return state, run_log

    def _shadow_portfolio_total(
        self,
        shadow_state: dict[str, Any],
        positions: list[tuple[Position, ConclusionState | None]],
    ) -> float:
        live_price_by_security = {
            str(position.security_id): float(
                position.current_price or position.avg_cost_basis or 0.0
            )
            for position, _ in positions
        }
        total = float(shadow_state.get("cash") or 0.0)
        for item in shadow_state.get("positions") or []:
            price = live_price_by_security.get(
                str(item.get("security_id")),
                float(item.get("current_price") or 0.0),
            )
            total += float(item.get("quantity") or 0.0) * float(price or 0.0)
        return total

    def _actual_portfolio_total(
        self, positions: list[tuple[Position, ConclusionState | None]]
    ) -> float:
        return RuntimeSettingsStore.load().portfolio.remaining_buying_power + sum(
            float(position.market_value or 0.0)
            for position, _ in positions
            if position.list_type == "holding"
        )

    def _portfolio_total_return(
        self, *, current_total: float, starting_total: float
    ) -> float:
        if starting_total <= 0:
            return 0.0
        return (current_total - starting_total) / starting_total

    def _position_return(self, position: Position) -> float:
        market_value = float(position.market_value or 0.0)
        quantity = float(position.quantity or 0.0)
        cost = float(position.avg_cost_basis or 0.0)
        if quantity <= 0 or cost <= 0:
            return 0.0
        invested = quantity * cost
        if invested == 0:
            return 0.0
        return (market_value - invested) / invested

    def _policy_objective(
        self,
        *,
        policy_description: str,
        guidance_mode: str | None,
        trigger_reason: str | None,
    ) -> str:
        policy = policy_description.lower()
        guidance = (guidance_mode or "follow_existing_policy").replace("_", " ")
        if "inverse" in policy:
            return (
                f"This run was trying to stress test the opposite side of the current conviction map under {guidance} guidance, "
                f"looking for places where the real book may be too complacent. Trigger: {trigger_reason or 'manual review'}."
            )
        if "thesis" in policy or "conviction" in policy:
            return (
                f"This run was trying to concentrate more heavily into the strongest stored theses under {guidance} guidance, "
                f"to see whether clearer conviction would have improved the portfolio. Trigger: {trigger_reason or 'manual review'}."
            )
        return (
            f"This run was trying to use the real portfolio as the starting point and test whether {guidance} management choices "
            f"would have improved the outcome from the same live state. Trigger: {trigger_reason or 'manual review'}."
        )

    def _expected_outcome_summary(
        self,
        *,
        guidance_mode: str | None,
        shadow_return: float,
        alpha: float,
        run_log: list[dict[str, Any]],
    ) -> str:
        guidance = (guidance_mode or "follow_existing_policy").replace("_", " ")
        most_material = sorted(
            run_log,
            key=lambda entry: abs(float(entry.get("actual_market_value") or 0.0)),
            reverse=True,
        )[:3]
        names = (
            ", ".join(filter(None, [entry.get("ticker") for entry in most_material]))
            or "the main held names"
        )
        performance_direction = self._performance_direction(alpha)
        direction = (
            "match"
            if performance_direction == "matched"
            else "outperform" if performance_direction == "outperformed" else "lag"
        )
        return (
            f"The shadow expected its {guidance} policy to {direction} the real portfolio by reallocating or sizing exposure differently "
            f"across {names}. Its projected shadow return was {shadow_return:+.2%}, implying expected alpha of {alpha:+.2%} versus the user’s portfolio baseline."
        )

    def _actual_outcome_summary(
        self,
        *,
        actual_return: float,
        shadow_return: float,
        alpha: float,
    ) -> str:
        direction = self._performance_direction(alpha)
        comparison = (
            "at the displayed precision"
            if direction == "matched"
            else f"by {alpha:+.2%}"
        )
        return (
            f"In actuality, the user’s real portfolio returned {actual_return:+.2%} while the shadow finished at {shadow_return:+.2%}. "
            f"The shadow therefore {direction} the real book {comparison}."
        )

    @staticmethod
    def _performance_direction(alpha: float, *, displayed_digits: int = 2) -> str:
        displayed_alpha = round(float(alpha) * 100, displayed_digits)
        if displayed_alpha > 0:
            return "outperformed"
        if displayed_alpha < 0:
            return "underperformed"
        return "matched"

    async def _resolve_guidance(
        self,
        *,
        operator_prompt: str | None,
        snapshot_summary: dict[str, Any],
        policy_description: str,
    ) -> dict[str, Any]:
        if not operator_prompt or not operator_prompt.strip():
            return {
                "guidance_mode": "follow_existing_policy",
                "guidance_summary": "No manual guidance was provided, so the run followed the selected policy.",
                "cash_reserve_pct": 0.0,
                "max_position_multiplier": 1.0,
            }
        try:
            timeout_seconds = self._structured_llm_timeout_seconds()
            return await asyncio.wait_for(
                call_llm_json(
                    system_prompt=(
                        "You are shaping a shadow portfolio experiment for Prophet. "
                        "Convert the user's guidance into one bounded operating mode. "
                        "Prefer conservative interpretation over imaginative interpretation."
                    ),
                    user_prompt=(
                        f"Policy description: {policy_description}\n"
                        f"Snapshot summary: {snapshot_summary}\n"
                        f"Operator prompt: {operator_prompt}\n"
                    ),
                    schema=SHADOW_GUIDANCE_SCHEMA,
                    timeout_seconds=timeout_seconds,
                ),
                timeout=timeout_seconds,
            )
        except Exception as exc:
            import logging

            logging.getLogger(__name__).exception("Masked failure caught")
            return {
                "guidance_mode": "follow_existing_policy",
                "guidance_summary": "Manual guidance could not be structured cleanly, so the run followed the selected policy.",
                "cash_reserve_pct": 0.0,
                "max_position_multiplier": 1.0,
            }

    async def _security_ticker(self, security_id: UUID) -> str:
        cached = self._security_ticker_cache.get(str(security_id))
        if cached is not None:
            return cached
        security = (
            await self.session.execute(
                select(Security).where(Security.id == security_id)
            )
        ).scalar_one_or_none()
        ticker = "UNKNOWN" if security is None else security.ticker
        self._security_ticker_cache[str(security_id)] = ticker
        return ticker

    async def _security_name(self, security_id: UUID) -> str | None:
        security = (
            await self.session.execute(
                select(Security).where(Security.id == security_id)
            )
        ).scalar_one_or_none()
        if security is None:
            return None
        entity = (
            await self.session.execute(
                select(Entity).where(Entity.id == security.entity_id)
            )
        ).scalar_one_or_none()
        return None if entity is None else entity.name


async def execute_shadow_experiment(experiment_id: UUID) -> None:
    async with async_session_maker() as session:
        service = ShadowService(session)
        await service.run_experiment(experiment_id)
