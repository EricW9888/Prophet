#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from sqlalchemy import func, select, text

from investos.config import settings
from investos.core.research_providers import configured_research_providers
from investos.db import async_session_maker
from investos.models.catalog import SourceClaimRecord
from investos.models.conclusion import ConclusionState
from investos.models.coverage import CoverageMap, Resolution, UnresolvedQuestion
from investos.models.evidence import RawEvidence, SourceItem
from investos.models.fundamental import FundamentalMetric
from investos.models.graph import Edge
from investos.models.knowledge import Claim, Event, Fact
from investos.models.knowledge_mutation import KnowledgeMutation
from investos.models.lesson import Lesson, LessonObservation
from investos.models.market_setup import MarketSetupSignal
from investos.models.portfolio import Position, Transaction
from investos.models.profile import Profile
from investos.models.reasoning import ReasoningRun
from investos.models.shadow import (
    ShadowAccountEvent,
    ShadowExperiment,
    ShadowFill,
    ShadowOrder,
)
from investos.models.source import Source
from investos.models.subject_alias import SubjectAlias
from investos.models.watcher import ActiveWatcher
from investos.services.runtime_settings import RuntimeSettingsStore


async def main() -> None:
    async with async_session_maker() as session:
        runtime = RuntimeSettingsStore.load()
        coverage_dupes = (
            await session.execute(
                select(
                    CoverageMap.subject_type,
                    CoverageMap.subject_id,
                    func.count(CoverageMap.id),
                )
                .group_by(CoverageMap.subject_type, CoverageMap.subject_id)
                .having(func.count(CoverageMap.id) > 1)
            )
        ).all()
        conclusion_dupes = (
            await session.execute(
                select(
                    ConclusionState.subject_type,
                    ConclusionState.subject_id,
                    func.count(ConclusionState.id),
                )
                .group_by(ConclusionState.subject_type, ConclusionState.subject_id)
                .having(func.count(ConclusionState.id) > 1)
            )
        ).all()
        counts = Counter(
            {
                "profiles_dossiers_table": await _count(session, Profile),
                "coverage_maps": await _count(session, CoverageMap),
                "conclusion_states": await _count(session, ConclusionState),
                "unresolved_questions_open": await _count(
                    session,
                    UnresolvedQuestion,
                    UnresolvedQuestion.status == "open",
                ),
                "unresolved_questions_investigating": await _count(
                    session,
                    UnresolvedQuestion,
                    UnresolvedQuestion.status == "investigating",
                ),
                "unresolved_questions_answered": await _count(
                    session,
                    UnresolvedQuestion,
                    UnresolvedQuestion.status == "answered",
                ),
                "unresolved_questions_obsolete": await _count(
                    session,
                    UnresolvedQuestion,
                    UnresolvedQuestion.status == "obsolete",
                ),
                "question_resolutions": await _count(session, Resolution),
                "active_watchers_pending": await _count(
                    session,
                    ActiveWatcher,
                    ActiveWatcher.is_active == True,
                    ActiveWatcher.status == "pending",
                ),
                "watchers_superseded": await _count(
                    session,
                    ActiveWatcher,
                    ActiveWatcher.status == "superseded",
                ),
                "watchers_triggered": await _count(
                    session,
                    ActiveWatcher,
                    ActiveWatcher.status == "triggered",
                ),
                "watchers_failed": await _count(
                    session,
                    ActiveWatcher,
                    ActiveWatcher.status == "failed",
                ),
                "positions": await _count(session, Position),
                "holding_positions": await _count(
                    session, Position, Position.list_type == "holding"
                ),
                "transactions": await _count(session, Transaction),
                "sources": await _count(session, Source),
                "source_claim_records": await _count(session, SourceClaimRecord),
                "source_claim_records_pending": await _count(
                    session,
                    SourceClaimRecord,
                    SourceClaimRecord.assessment == "pending",
                ),
                "source_claim_records_assessed": await _count(
                    session,
                    SourceClaimRecord,
                    SourceClaimRecord.assessment != "pending",
                ),
                "source_claim_records_assessment_deferred": await _count(
                    session,
                    SourceClaimRecord,
                    SourceClaimRecord.assessment == "pending",
                    SourceClaimRecord.next_assessment_at > datetime.now(UTC),
                ),
                "subject_aliases": await _count(session, SubjectAlias),
                "raw_evidence": await _count(session, RawEvidence),
                "source_items": await _count(session, SourceItem),
                "reasoning_runs": await _count(session, ReasoningRun),
                "lessons": await _count(session, Lesson),
                "shadow_lessons": await _count(
                    session,
                    Lesson,
                    Lesson.lesson_type == "shadow_policy_outcome",
                ),
                "shadow_lessons_provisional": await _count(
                    session,
                    Lesson,
                    Lesson.lesson_type == "shadow_policy_outcome",
                    Lesson.maturity_status == "provisional",
                ),
                "shadow_lessons_validated": await _count(
                    session,
                    Lesson,
                    Lesson.lesson_type == "shadow_policy_outcome",
                    Lesson.maturity_status == "validated",
                ),
                "shadow_lesson_observations": await _count(session, LessonObservation),
                "shadow_experiments": await _count(session, ShadowExperiment),
                "shadow_orders": await _count(session, ShadowOrder),
                "shadow_orders_accepted": await _count(
                    session, ShadowOrder, ShadowOrder.status == "accepted"
                ),
                "shadow_orders_rejected": await _count(
                    session, ShadowOrder, ShadowOrder.status == "rejected"
                ),
                "shadow_orders_canceled": await _count(
                    session, ShadowOrder, ShadowOrder.status == "canceled"
                ),
                "shadow_orders_filled": await _count(
                    session, ShadowOrder, ShadowOrder.status == "filled"
                ),
                "shadow_fills": await _count(session, ShadowFill),
                "shadow_account_events": await _count(session, ShadowAccountEvent),
                "shadow_account_events_needing_reconciliation": await _count(
                    session,
                    ShadowAccountEvent,
                    ShadowAccountEvent.status == "needs_reconciliation",
                ),
                "fundamental_metrics": await _count(session, FundamentalMetric),
                "fundamental_metrics_stale": await _count(
                    session,
                    FundamentalMetric,
                    FundamentalMetric.freshness_status == "stale",
                ),
                "market_setup_signals": await _count(session, MarketSetupSignal),
                "market_setup_signals_unscored": await _count(
                    session,
                    MarketSetupSignal,
                    MarketSetupSignal.outcome_status == "unscored",
                ),
                "market_setup_signals_validated": await _count(
                    session,
                    MarketSetupSignal,
                    MarketSetupSignal.outcome_status == "validated",
                ),
                "market_setup_signals_partially_validated": await _count(
                    session,
                    MarketSetupSignal,
                    MarketSetupSignal.outcome_status == "partially_validated",
                ),
                "market_setup_signals_invalidated": await _count(
                    session,
                    MarketSetupSignal,
                    MarketSetupSignal.outcome_status == "invalidated",
                ),
                "market_setup_signals_indeterminate": await _count(
                    session,
                    MarketSetupSignal,
                    MarketSetupSignal.outcome_status == "indeterminate",
                ),
                "market_setup_signals_assessment_deferred": await _count(
                    session,
                    MarketSetupSignal,
                    MarketSetupSignal.outcome_status == "unscored",
                    MarketSetupSignal.metadata_json["outcome_assessment_attempt"][
                        "next_retry_at"
                    ].astext
                    > datetime.now(UTC).isoformat(),
                ),
                "knowledge_mutations": await _count(session, KnowledgeMutation),
                "facts": await _count(session, Fact),
                "claims": await _count(session, Claim),
                "events": await _count(session, Event),
                "edges": await _count(session, Edge),
            }
        )
        abnormal_queries = {
            "duplicate_holding_positions_by_security": text("""
                select p.security_id, count(*) as c
                from positions p
                where p.list_type = 'holding'
                group by p.security_id
                having count(*) > 1
                """),
            "processed_evidence_missing_source_item": text("""
                select count(*)
                from raw_evidence re
                left join source_items si on si.raw_evidence_id = re.id
                where re.is_processed = true
                  and si.id is null
                  and re.source_item_type not in ('conversation_turn', 'manual_note')
                  and coalesce((re.metadata_json ->> 'skip_extraction')::boolean, false) = false
                  and coalesce((re.metadata_json ->> 'skipped')::boolean, false) = false
                """),
            "source_items_missing_raw_evidence": text("""
                select count(*)
                from source_items si
                left join raw_evidence re on re.id = si.raw_evidence_id
                where re.id is null
                """),
            "questions_missing_dossier": text("""
                select count(*)
                from unresolved_questions uq
                left join coverage_maps cm on cm.id = uq.coverage_map_id
                where uq.coverage_map_id is not null and cm.id is null
                """),
            "holding_positions_nonpositive_quantity": text("""
                select count(*)
                from positions
                where list_type = 'holding' and quantity <= 0
                """),
            "holding_positions_negative_market_value": text("""
                select count(*)
                from positions
                where list_type = 'holding' and market_value < 0
                """),
            "shadow_fills_missing_order": text("""
                select count(*)
                from shadow_fills sf
                left join shadow_orders so on so.id = sf.order_id
                where so.id is null
                """),
            "filled_shadow_orders_missing_fill": text("""
                select count(*)
                from shadow_orders so
                left join shadow_fills sf on sf.order_id = so.id
                where so.status = 'filled' and sf.id is null
                """),
            "nonfilled_shadow_orders_with_fill": text("""
                select count(*)
                from shadow_orders so
                join shadow_fills sf on sf.order_id = so.id
                where so.status <> 'filled'
                """),
            "shadow_order_negative_values": text("""
                select count(*)
                from shadow_orders
                where requested_quantity < 0
                   or filled_quantity < 0
                   or reference_price < 0
                   or reserved_notional < 0
                """),
            "shadow_account_events_missing_experiment_or_transaction": text("""
                select count(*)
                from shadow_account_events sae
                left join shadow_experiments se on se.id = sae.experiment_id
                left join transactions t on t.id = sae.source_transaction_id
                where se.id is null or t.id is null
                """),
            "shadow_account_event_invalid_values": text("""
                select count(*)
                from shadow_account_events
                where quantity_before < 0
                   or quantity_after < 0
                   or cash_before < 0
                   or cash_after < 0
                   or amount < 0
                   or status not in ('applied', 'not_applicable', 'rejected', 'needs_reconciliation', 'superseded')
                """),
            "completed_shadow_results_missing_lesson_observation": text("""
                select count(*)
                from experiment_results er
                join shadow_experiments se on se.id = er.experiment_id
                left join lesson_observations lo on lo.experiment_result_id = er.id
                where se.run_status = 'completed' and lo.id is null
                """),
            "shadow_lesson_observations_missing_result": text("""
                select count(*)
                from lesson_observations lo
                left join experiment_results er on er.id = lo.experiment_result_id
                where er.id is null
                """),
        }
        abnormal = {}
        for key, stmt in abnormal_queries.items():
            result = await session.execute(stmt)
            rows = result.fetchall()
            abnormal[key] = rows

    print("Prophet state audit")
    print("====================")
    print(f"runtime_settings_path: {settings.RUNTIME_SETTINGS_PATH}")
    print(f"runtime_llm_provider: {runtime.llm.provider}")
    print(f"runtime_llm_key_set: {bool(runtime.llm.api_key)}")
    print(
        "runtime_research_provider_order: " + ",".join(runtime.research.provider_order)
    )
    print(
        "runtime_research_configured_providers: "
        + ",".join(configured_research_providers(runtime.research))
    )
    print(f"runtime_research_key_set: {bool(runtime.research.api_key)}")
    print(f"runtime_gmail_enabled: {runtime.gmail.enabled}")
    print(
        f"runtime_gmail_ready: {bool(runtime.gmail.password) and bool(runtime.gmail.username)}"
    )
    print(f"runtime_plaid_enabled: {runtime.plaid.enabled}")
    print(f"runtime_plaid_connected: {bool(runtime.plaid.access_token)}")
    print(f"runtime_paper_trading_enabled: {runtime.paper_trading.enabled}")
    print(f"runtime_paper_trading_provider: {runtime.paper_trading.provider}")
    for key, value in counts.items():
        print(f"{key}: {value}")
    print(f"duplicate coverage subjects: {len(coverage_dupes)}")
    print(f"duplicate conclusion subjects: {len(conclusion_dupes)}")
    if coverage_dupes:
        print("coverage duplicates:")
        for subject_type, subject_id, count in coverage_dupes[:10]:
            print(f"  - {subject_type}:{subject_id} ({count})")
    if conclusion_dupes:
        print("conclusion duplicates:")
        for subject_type, subject_id, count in conclusion_dupes[:10]:
            print(f"  - {subject_type}:{subject_id} ({count})")
    print("abnormal checks:")
    for key, rows in abnormal.items():
        if len(rows) == 1 and len(rows[0]) == 1:
            print(f"  - {key}: {rows[0][0]}")
        else:
            print(f"  - {key}: {rows[:5]}")


async def _count(session, model, *filters) -> int:
    stmt = select(func.count()).select_from(model)
    for item in filters:
        stmt = stmt.where(item)
    return int((await session.execute(stmt)).scalar_one() or 0)


if __name__ == "__main__":
    asyncio.run(main())
