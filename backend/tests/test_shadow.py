from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from investos.schemas.lesson import LessonResponse
from investos.schemas.shadow import ShadowExperimentCreate
from investos.services.operating_loop import OperatingLoopService
from investos.services.shadow import ShadowService


def test_shadow_default_window_is_time_evolving():
    start = datetime(2026, 7, 17, 12, tzinfo=UTC)

    end = ShadowService._default_end_point(start_point=start, horizon_label="adaptive")

    assert end > start
    assert (end - start).days >= 1


def test_shadow_checkpoint_waits_for_due_time_but_evidence_can_wake_it():
    now = datetime(2026, 7, 17, 12, tzinfo=UTC)
    progress = {"next_checkpoint_at": (now + timedelta(hours=6)).isoformat()}

    assert not ShadowService._checkpoint_is_due(
        progress=progress,
        pending_evidence_events=[],
        now=now,
    )
    assert ShadowService._checkpoint_is_due(
        progress=progress,
        pending_evidence_events=[{"event_id": "new-evidence"}],
        now=now,
    )


def test_shadow_checkpoint_schedule_spans_the_observation_window():
    start = datetime(2026, 7, 17, 12, tzinfo=UTC)
    experiment = SimpleNamespace(
        start_point=start,
        end_point=start + timedelta(days=9),
    )

    checkpoint = ShadowService._next_checkpoint_at(
        experiment=experiment,
        target_steps=4,
        now=start,
    )

    assert checkpoint == start + timedelta(days=3)


def test_zero_duration_shadow_run_is_not_eligible_for_learning():
    instant = datetime(2026, 7, 17, 12, tzinfo=UTC)

    assert not ShadowService._experiment_has_valid_observation_window(
        SimpleNamespace(start_point=instant, end_point=instant)
    )
    assert ShadowService._experiment_has_valid_observation_window(
        SimpleNamespace(start_point=instant, end_point=instant + timedelta(days=1))
    )


def test_shadow_dividend_terms_use_point_in_time_settled_share_count():
    timestamp = datetime(2026, 7, 16, 18, 0, tzinfo=UTC)
    history = [
        SimpleNamespace(id=uuid4(), action="buy", quantity=10, executed_at=timestamp),
        SimpleNamespace(id=uuid4(), action="sell", quantity=2, executed_at=timestamp),
        SimpleNamespace(id=uuid4(), action="split", quantity=4, executed_at=timestamp),
    ]
    dividend = SimpleNamespace(
        id=uuid4(),
        action="dividend",
        quantity=0,
        price=16,
        executed_at=timestamp,
        provenance_json={},
    )
    history.append(dividend)

    per_share, derivation = ShadowService._dividend_terms(
        transaction=dividend,
        history=history,
    )

    assert per_share == Decimal("0.5")
    assert derivation == "derived_from_settled_transaction_ledger"


def test_shadow_dividend_terms_prefer_nested_explicit_per_share_provenance():
    dividend = SimpleNamespace(
        id=uuid4(),
        action="dividend",
        quantity=0,
        price=999,
        executed_at=datetime(2026, 7, 16, 18, 0, tzinfo=UTC),
        provenance_json={"broker_payload": {"dividend_per_share": "1.25"}},
    )

    per_share, derivation = ShadowService._dividend_terms(
        transaction=dividend,
        history=[dividend],
    )

    assert per_share == Decimal("1.25")
    assert derivation == "explicit_per_share_provenance"


def test_lesson_response_keeps_legacy_null_classifications_readable():
    lesson = SimpleNamespace(
        id=uuid4(),
        title="Observed shadow outcome",
        summary="One completed run is retained as provisional evidence.",
        lesson_type="shadow_experiment",
        applicable_sectors=None,
        applicable_regimes=None,
        originating_decision_review_id=None,
        originating_experiment_result_id=uuid4(),
        experiment_family_id=uuid4(),
        maturity_status="provisional",
        confidence_score=0.25,
        supporting_observations=1,
        contradicting_observations=0,
        neutral_observations=0,
        last_validated_at=None,
        stale_after=None,
        metadata_json=None,
        usage_count=0,
        created_at=datetime.now(UTC),
    )

    response = LessonResponse.model_validate(lesson)

    assert response.applicable_sectors == []
    assert response.applicable_regimes == []
    assert response.metadata_json == {}


def test_shadow_experiment_lock_is_stable_per_experiment():
    first_id = uuid4()
    second_id = uuid4()

    assert ShadowService._experiment_run_lock(
        first_id
    ) is ShadowService._experiment_run_lock(first_id)
    assert ShadowService._experiment_run_lock(
        first_id
    ) is not ShadowService._experiment_run_lock(second_id)
    assert ShadowService.experiment_run_is_active(first_id) is False


def test_shadow_subject_matching_uses_structured_ids_not_display_text():
    subject_id = uuid4()
    security_id = uuid4()
    experiment = SimpleNamespace(
        initial_portfolio_state_json={
            "experiment_context": {
                "subject_refs": [
                    {
                        "subject_type": "entity",
                        "subject_id": str(subject_id),
                        "security_id": str(security_id),
                    }
                ]
            }
        }
    )

    assert ShadowService._experiment_matches_subject(
        experiment,
        subject_type="entity",
        subject_id=subject_id,
        security_id=security_id,
    )
    assert not ShadowService._experiment_matches_subject(
        experiment,
        subject_type="entity",
        subject_id=uuid4(),
        security_id=uuid4(),
    )


def test_shadow_discovery_subject_refs_follow_cited_candidate_evidence():
    cited_entity = uuid4()
    cited_security = uuid4()
    refs = ShadowService._discovery_subject_refs(
        discovery_profile={"evidence_snapshot": [{"ticker": "AAA"}]},
        candidates=[
            {
                "ticker": "AAA",
                "entity_id": str(cited_entity),
                "security_id": str(cited_security),
            },
            {
                "ticker": "BBB",
                "entity_id": str(uuid4()),
                "security_id": str(uuid4()),
            },
        ],
    )

    assert refs == [
        {
            "subject_type": "entity",
            "subject_id": str(cited_entity),
            "security_id": str(cited_security),
        }
    ]


@pytest.mark.asyncio
async def test_operating_loop_wakes_active_subject_experiment(monkeypatch):
    subject_id = uuid4()
    security_id = uuid4()
    evidence_id = uuid4()
    active = SimpleNamespace(id=uuid4(), run_status="running")
    service = OperatingLoopService(session=None)
    monkeypatch.setattr(
        service,
        "_active_position_context",
        AsyncMock(
            return_value=(
                SimpleNamespace(id=uuid4()),
                SimpleNamespace(id=security_id, ticker="TEST"),
            )
        ),
    )
    find = AsyncMock(return_value=active)
    queue = AsyncMock(
        return_value={
            "event_id": str(uuid4()),
            "experiment_id": str(active.id),
            "queued": True,
            "deduplicated": False,
        }
    )
    monkeypatch.setattr(ShadowService, "find_subject_experiment", find)
    monkeypatch.setattr(ShadowService, "queue_subject_evidence_event", queue)

    result = await service._maybe_trigger_shadow(
        subject_id=subject_id,
        subject_type="entity",
        subject_name="Test Company",
        trigger_reason="new evidence ingested",
        previous_state=SimpleNamespace(current_stance="neutral", confidence_band="low"),
        current_state=SimpleNamespace(current_stance="bullish", confidence_band="high"),
        coverage=SimpleNamespace(contradiction_count=0, overall_coverage_score=75),
        raw_evidence_id=evidence_id,
    )

    assert result["reason"] == "active_shadow_woken_by_evidence"
    assert result["experiment_id"] == str(active.id)
    queue.assert_awaited_once()
    assert queue.await_args.kwargs["raw_evidence_id"] == evidence_id


def test_shadow_discovery_priority_and_labels_are_advisory_when_contract_is_complete():
    profile = ShadowService._normalize_discovery_profile(
        {
            "should_launch": True,
            "name": "Custom memory spread paper trade",
            "opportunity_type": "custom_memory_spread",
            "priority_score": 0.2,
            "signal_stage": "early_confirmation",
            "why_now": "Official memory demand evidence changed before the next earnings print.",
            "priced_in_assessment": "uncertain",
            "investable_thesis": "HBM/NAND evidence could justify testing a staged MEMB/MEMA spread without changing the real book.",
            "portfolio_transmission": "The portfolio has direct memory-cycle exposure through MEMB and MEMA.",
            "expected_edge": "The shadow can compare staged confirmation against static real-book concentration.",
            "leading_indicators": ["Official memory demand evidence improved."],
            "lagging_confirmations": [
                "Reported margins have not yet confirmed the demand change."
            ],
            "evidence_refs": ["fundamental:memory-demand"],
            "evidence_to_check": ["Official HBM/NAND guidance and estimate revisions."],
            "falsification_tests": ["Guidance weakens memory pricing or margins."],
            "risk_controls": [
                "Keep simulated exposure capped and reverse on official contradiction."
            ],
            "uncertainties": ["The evidence may already be reflected in valuation."],
            "policy": "Paper-trade a custom memory spread only when official evidence confirms the setup.",
            "operator_prompt": "Review official guidance, price reaction, and estimate revisions at every checkpoint.",
            "horizon": "event_driven_until_next_memory_print",
            "no_launch_reason": "",
        }
    )

    actionable, reason = ShadowService._discovery_profile_is_actionable(profile)

    assert actionable is True
    assert reason is None
    assert profile["opportunity_type"] == "custom_memory_spread"
    assert profile["horizon"] == "event_driven_until_next_memory_print"


def test_shadow_discovery_requires_falsifiable_investment_contract():
    profile = ShadowService._normalize_discovery_profile(
        {
            "should_launch": True,
            "name": "Memory-cycle inflection paper trade",
            "opportunity_type": "catalyst_timing",
            "priority_score": 0.8,
            "signal_stage": "early",
            "why_now": "A new demand signal arrived before guidance.",
            "priced_in_assessment": "uncertain",
            "investable_thesis": "HBM pricing strength may spill into memory-equipment and NAND sentiment.",
            "portfolio_transmission": "Portfolio has direct MEMB/MEMA memory exposure and adjacent AI infrastructure names.",
            "expected_edge": "The shadow can test earlier sizing changes before the accepted thesis reaches high confidence.",
            "leading_indicators": ["Demand signal improved."],
            "lagging_confirmations": ["Earnings and margins have not confirmed."],
            "evidence_refs": ["setup:memory-demand"],
            "evidence_to_check": [],
            "falsification_tests": ["MEMB guidance cuts HBM/NAND demand expectations."],
            "risk_controls": [
                "Cap total memory exposure and require official source confirmation."
            ],
            "uncertainties": ["Competitive supply response is unknown."],
            "policy": "Paper-trade a staged memory-cycle confirmation policy.",
            "operator_prompt": "Increase only after official evidence; trim if guidance contradicts the setup.",
            "horizon": "medium_term",
            "no_launch_reason": "",
        }
    )

    actionable, reason = ShadowService._discovery_profile_is_actionable(profile)

    assert actionable is False
    assert reason == "missing_required_shadow_discovery_lists:evidence_to_check"


def test_shadow_discovery_accepts_specific_paper_trade_opportunity():
    profile = ShadowService._normalize_discovery_profile(
        {
            "should_launch": True,
            "name": "Memory-cycle confirmation paper trade",
            "opportunity_type": "catalyst_timing",
            "priority_score": 0.82,
            "signal_stage": "confirming",
            "why_now": "Demand evidence is improving before the next reported margin update.",
            "priced_in_assessment": "partially_priced",
            "investable_thesis": "Official HBM/NAND guidance could confirm whether memory-cycle strength is broad enough to justify more exposure.",
            "portfolio_transmission": "MEMB and MEMA carry direct memory-cycle exposure, while AI infrastructure holdings provide read-through.",
            "expected_edge": "A shadow can test adding only after confirmation versus holding the current concentrated real book unchanged.",
            "leading_indicators": [
                "Official demand commentary improved before reported revenue and margins."
            ],
            "lagging_confirmations": [
                "Reported margins and estimate revisions have not fully confirmed the signal."
            ],
            "evidence_refs": ["fundamental:memory-guidance"],
            "evidence_to_check": [
                "Official earnings release or transcript guidance.",
                "Price reaction and estimate revisions after the event.",
            ],
            "falsification_tests": [
                "Guidance weakens NAND/HBM demand or margins.",
                "Positive revenue is offset by pricing or inventory deterioration.",
            ],
            "risk_controls": [
                "Cap simulated memory exposure.",
                "Use a watcher deadline and trim on contradiction.",
            ],
            "uncertainties": [
                "The market may already discount the expected memory recovery."
            ],
            "policy": "Paper-trade a confirmation-gated memory exposure policy against the real portfolio baseline.",
            "operator_prompt": "At each checkpoint, compare official guidance, price reaction, and estimate revisions before changing memory exposure.",
            "horizon": "medium_term",
            "no_launch_reason": "",
        }
    )

    actionable, reason = ShadowService._discovery_profile_is_actionable(profile)

    assert actionable is True
    assert reason is None
    assert profile["trigger_reason"].startswith("Autonomous shadow opportunity:")
    assert "priority=0.82" in profile["trigger_reason"]
    assert "Official HBM/NAND guidance" in profile["investable_thesis"]


def test_shadow_discovery_rejects_unknown_or_market_only_evidence_refs():
    base = ShadowService._normalize_discovery_profile(
        {
            "should_launch": True,
            "name": "Evidence-linked candidate",
            "opportunity_type": "open_ended_inflection",
            "priority_score": 0.7,
            "signal_stage": "early",
            "why_now": "A leading signal changed before reported results.",
            "priced_in_assessment": "uncertain",
            "investable_thesis": "Test whether the leading change becomes a durable earnings revision.",
            "portfolio_transmission": "The tracked position is directly exposed.",
            "expected_edge": "Act only if the source-backed signal precedes consensus revisions.",
            "leading_indicators": ["A point-in-time signal changed."],
            "lagging_confirmations": ["Reported earnings have not confirmed."],
            "evidence_refs": ["market:XYZ:2026-07-10"],
            "evidence_to_check": ["Verify the source-backed mechanism."],
            "falsification_tests": ["The leading signal reverses."],
            "risk_controls": ["Use a bounded paper position."],
            "uncertainties": ["The change may already be priced in."],
            "policy": "Paper-trade only while the point-in-time evidence remains valid.",
            "operator_prompt": "Recheck evidence, pricing, and invalidation at every checkpoint.",
            "horizon": "adaptive",
            "no_launch_reason": "",
        }
    )

    actionable, reason = ShadowService._discovery_profile_is_actionable(
        base,
        available_evidence_refs={"market:XYZ:2026-07-10"},
    )
    assert actionable is False
    assert (
        reason == "shadow_discovery_requires_source_backed_evidence_beyond_market_tape"
    )

    base["evidence_refs"] = ["fundamental:unknown"]
    actionable, reason = ShadowService._discovery_profile_is_actionable(
        base,
        available_evidence_refs={"fundamental:known"},
    )
    assert actionable is False
    assert reason == "unknown_shadow_discovery_evidence_refs:fundamental:unknown"


def test_shadow_discovery_rejects_invented_notional_above_snapshot():
    profile = ShadowService._normalize_discovery_profile(
        {
            "should_launch": True,
            "name": "Bounded candidate",
            "opportunity_type": "risk_control",
            "priority_score": 0.8,
            "signal_stage": "confirming",
            "why_now": "A source-backed risk signal changed.",
            "priced_in_assessment": "uncertain",
            "investable_thesis": "Test a reduction without changing the real book.",
            "portfolio_transmission": "Convert $1.5M to cash.",
            "expected_edge": "Reduce drawdown.",
            "leading_indicators": ["Risk increased."],
            "lagging_confirmations": ["Reported results have not confirmed."],
            "evidence_refs": ["fundamental:risk"],
            "evidence_to_check": ["Recheck the source."],
            "falsification_tests": ["Risk reverses."],
            "risk_controls": ["Keep the paper trade bounded."],
            "uncertainties": ["Timing is uncertain."],
            "policy": "Paper-trade a partial reduction.",
            "operator_prompt": "Review at the next checkpoint.",
            "horizon": "adaptive",
            "no_launch_reason": "",
        }
    )

    actionable, reason = ShadowService._discovery_profile_is_actionable(
        profile,
        available_evidence_refs={"fundamental:risk"},
        portfolio_value=11_000.0,
    )

    assert actionable is False
    assert reason == "shadow_discovery_notional_exceeds_point_in_time_portfolio"


def test_shadow_report_preserves_discovery_profile_without_name_error():
    discovery_profile = {
        "investable_thesis": "Test whether concentration controls improve the real-book outcome.",
        "expected_edge": "Reduce avoidable drawdown without losing the core thesis.",
    }
    experiment = SimpleNamespace(
        policy_description="Cap concentrated positions when source-backed risk rises.",
        initial_portfolio_state_json={
            "experiment_context": {
                "trigger_type": "autonomous_discovery",
                "discovery_profile": discovery_profile,
            },
            "snapshot_summary": {},
        },
        final_portfolio_state_json={"run_details": {}},
    )
    result = SimpleNamespace(
        shadow_return=0.01,
        actual_return=0.0,
        alpha=0.01,
        max_drawdown=0.0,
        reasoning="Checkpoint result.",
    )

    report = ShadowService(None)._build_experiment_report(
        experiment=experiment,
        result=result,
        run_log=[],
    )

    assert report["opportunity_summary"] == discovery_profile


def test_shadow_report_calls_display_zero_alpha_a_match():
    experiment = SimpleNamespace(
        policy_description="Test a bounded policy against the real portfolio.",
        initial_portfolio_state_json={
            "experiment_context": {"trigger_type": "manual"},
            "snapshot_summary": {},
        },
        final_portfolio_state_json={"run_details": {}},
    )
    result = SimpleNamespace(
        shadow_return=0.0000001,
        actual_return=0.0,
        alpha=0.0000001,
        max_drawdown=0.0,
        reasoning="No displayed difference.",
    )

    report = ShadowService(None)._build_experiment_report(
        experiment=experiment,
        result=result,
        run_log=[],
    )

    assert report["policy_assessment"].startswith("The experiment matched")
    assert report["actual_outcome"]["outperformed_baseline"] is None
    assert report["learning_summary"]["lesson_direction"] == "observed_match"
    assert "does not yet support promoting or demoting" in report["key_lesson"]


def test_shadow_learning_keeps_one_run_provisional_and_validates_repetition():
    provisional = ShadowService._shadow_lesson_state(
        alphas=[0.04],
        material_alpha=0.01,
        minimum_runs=3,
        validation_consistency=0.67,
    )
    validated = ShadowService._shadow_lesson_state(
        alphas=[0.04, 0.03, 0.02],
        material_alpha=0.01,
        minimum_runs=3,
        validation_consistency=0.67,
    )

    assert provisional["maturity"] == "provisional"
    assert provisional["supporting"] == 1
    assert provisional["confidence"] < 1.0
    assert validated["maturity"] == "validated"
    assert validated["supporting"] == 3
    assert validated["confidence"] == 1.0


def test_shadow_learning_preserves_counterexamples_instead_of_forcing_a_rule():
    state = ShadowService._shadow_lesson_state(
        alphas=[0.04, -0.03, 0.02],
        material_alpha=0.01,
        minimum_runs=3,
        validation_consistency=0.8,
    )

    assert state["maturity"] == "mixed"
    assert state["supporting"] == 2
    assert state["contradicting"] == 1
    assert state["direction"] == "outperformance"


def test_open_ended_family_key_groups_repeated_mechanism_not_one_off_headline():
    service = ShadowService(None)
    first = service._family_name(
        name="Memory opportunity after one earnings call",
        policy_description="Stage exposure when supply discipline improves.",
        trigger_reason="A first catalyst changed.",
        discovery_profile={
            "family_key": "memory supply discipline",
            "evidence_snapshot": [{"ticker": "MEMB"}, {"ticker": "MEMA"}],
        },
    )
    repeated = service._family_name(
        name="Different headline months later",
        policy_description="A differently worded policy.",
        trigger_reason="A later catalyst changed.",
        discovery_profile={
            "family_key": "memory supply discipline",
            "evidence_snapshot": [{"ticker": "MEMA"}, {"ticker": "MEMB"}],
        },
    )

    assert first == repeated


def test_shadow_model_timeout_is_configurable(monkeypatch):
    monkeypatch.setattr(
        "investos.services.shadow.settings.SHADOW_LLM_TIMEOUT_SECONDS",
        37,
    )
    assert ShadowService._structured_llm_timeout_seconds() == 37


@pytest.mark.asyncio
async def test_shadow_discovery_propagates_provider_failure(monkeypatch):
    service = ShadowService(session=None)

    async def discovery_context(*, captured_at):
        return {
            "captured_at": captured_at.isoformat(),
            "portfolio": {
                "total_market_value": 10_000.0,
                "remaining_buying_power": 1_000.0,
            },
            "candidates": [{"ticker": "XYZ"}],
            "evidence_registry": [],
        }

    async def fail_llm(**kwargs):
        raise TimeoutError("provider unavailable")

    monkeypatch.setattr(service, "_build_discovery_context", discovery_context)
    monkeypatch.setattr("investos.services.shadow.call_llm_json", fail_llm)

    with pytest.raises(TimeoutError, match="provider unavailable"):
        await service.discover_and_queue_experiments()


@pytest.mark.asyncio
async def test_shadow_checkpoint_failure_never_invents_fallback_trades(monkeypatch):
    service = ShadowService(session=None)
    position = SimpleNamespace(
        security_id=uuid4(),
        quantity=10,
        market_value=1_000,
        current_price=100,
    )
    experiment = SimpleNamespace(
        name="Fallback safety",
        policy_description="Test concentration risk.",
        initial_portfolio_state_json={
            "experiment_context": {"trigger_reason": "verification"},
            "snapshot_summary": {
                "total_market_value": 1_000,
                "remaining_buying_power": 0,
            },
        },
    )

    async def ticker(_security_id):
        return "TEST"

    async def name(_security_id):
        return "Test Company"

    async def fail_checkpoint(**kwargs):
        raise TimeoutError("provider unavailable")

    monkeypatch.setattr(service, "_security_ticker", ticker)
    monkeypatch.setattr(service, "_security_name", name)
    monkeypatch.setattr("investos.services.shadow.call_llm_json", fail_checkpoint)

    plan = await service._build_checkpoint_plan(
        experiment=experiment,
        positions=[(position, None)],
        guidance={"guidance_mode": "concentration_control", "guidance_summary": "trim"},
        checkpoint_log=[],
        decision_history=[],
        step_number=1,
    )

    assert plan["decisions"][0]["action"] == "hold"
    assert plan["decisions"][0]["target_weight_pct"] == 100.0
    assert plan["_checkpoint_source"] == "fallback"


@pytest.mark.asyncio
async def test_shadow_checkpoint_receives_pending_evidence_events(monkeypatch):
    service = ShadowService(session=None)
    position = SimpleNamespace(
        security_id=uuid4(),
        quantity=10,
        market_value=1_000,
        current_price=100,
    )
    experiment = SimpleNamespace(
        name="Evidence wake-up",
        policy_description="Reassess when material evidence arrives.",
        initial_portfolio_state_json={
            "experiment_context": {"trigger_reason": "new evidence"},
            "snapshot_summary": {
                "total_market_value": 1_000,
                "remaining_buying_power": 0,
            },
        },
        final_portfolio_state_json={
            "run_details": {
                "pending_evidence_events": [
                    {
                        "event_id": str(uuid4()),
                        "trigger_type": "processed_evidence",
                        "trigger_metadata": {
                            "summary": "Supplier guidance changed materially."
                        },
                    }
                ]
            }
        },
    )
    captured = {}

    async def ticker(_security_id):
        return "TEST"

    async def name(_security_id):
        return "Test Company"

    async def checkpoint(**kwargs):
        captured.update(kwargs)
        return {
            "checkpoint_objective": "Reassess the changed evidence.",
            "portfolio_view": "The evidence changed the monitored setup.",
            "planned_posture": "Hold until the evidence is corroborated.",
            "why_now": "A material subject-linked event arrived.",
            "research_goal": None,
            "monitoring_focus": ["supplier guidance"],
            "what_would_change_mind": ["official corroboration"],
            "catalyst_tracker": "Track the next official filing.",
            "contingency_plan": "Keep exposure unchanged until validated.",
            "decisions": [],
        }

    monkeypatch.setattr(service, "_security_ticker", ticker)
    monkeypatch.setattr(service, "_security_name", name)
    monkeypatch.setattr("investos.services.shadow.call_llm_json", checkpoint)

    plan = await service._build_checkpoint_plan(
        experiment=experiment,
        positions=[(position, None)],
        guidance={"guidance_mode": "evidence_review"},
        checkpoint_log=[],
        decision_history=[],
        step_number=2,
    )

    assert "Supplier guidance changed materially" in captured["user_prompt"]
    assert plan["_checkpoint_source"] == "provider"


def test_target_weight_can_create_bounded_intent_for_tracked_zero_position():
    quantity = ShadowService._desired_shadow_trade_quantity(
        current_quantity=0.0,
        live_quantity=0.0,
        action="buy",
        multiplier=1.0,
        target_weight_pct=5.0,
        account_equity=10_000.0,
        reference_price=100.0,
    )

    assert quantity == 5.0


def test_shadow_portfolio_total_values_paper_only_positions_from_broker_state():
    paper_security_id = uuid4()
    total = ShadowService(None)._shadow_portfolio_total(
        {
            "cash": 100.0,
            "positions": [
                {
                    "security_id": str(paper_security_id),
                    "quantity": 2.0,
                    "current_price": 125.0,
                }
            ],
        },
        [],
    )

    assert total == 350.0


def test_historical_paper_state_falls_back_to_captured_snapshot_marks():
    security_id = uuid4()
    experiment = SimpleNamespace(
        initial_portfolio_state_json={
            "captured_at": "2026-07-01T12:00:00+00:00",
            "positions": [
                {
                    "security_id": str(security_id),
                    "current_price": 125.0,
                }
            ],
            "shadow_state": {
                "cash": 50.0,
                "positions": [
                    {
                        "security_id": str(security_id),
                        "quantity": 2.0,
                        "avg_cost_basis": 100.0,
                    }
                ],
            },
        },
        final_portfolio_state_json={
            "shadow_state": {
                "cash": 50.0,
                "positions": [
                    {
                        "security_id": str(security_id),
                        "quantity": 2.0,
                        "avg_cost_basis": 100.0,
                        "current_price": 0.0,
                    }
                ],
            }
        },
    )

    state = ShadowService(None)._paper_state_for_serialization(experiment)

    assert state["positions"][0]["current_price"] == 125.0
    assert state["positions"][0]["marked_at"] == "2026-07-01T12:00:00+00:00"


@pytest.mark.asyncio
async def test_manual_paper_account_is_created_without_autonomous_queue(monkeypatch):
    session = SimpleNamespace(
        add=MagicMock(),
        commit=AsyncMock(),
        refresh=AsyncMock(),
    )
    service = ShadowService(session)
    monkeypatch.setattr(
        service,
        "_portfolio_snapshot",
        AsyncMock(
            return_value={
                "experiment_context": {},
                "snapshot_summary": {},
                "shadow_state": {"cash": 1000.0, "cash_reserved": 0.0, "positions": []},
            }
        ),
    )
    monkeypatch.setattr(
        service, "_get_or_create_family_state", AsyncMock(return_value=None)
    )

    experiment = await service.create_experiment(
        ShadowExperimentCreate(
            name="Manual account",
            policy_description="User-directed paper account.",
            auto_run=False,
        )
    )

    assert experiment.run_status == "manual"
    assert (
        experiment.initial_portfolio_state_json["experiment_context"]["execution_mode"]
        == "manual"
    )
    session.add.assert_called_once_with(experiment)
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_cash_only_paper_account_uses_explicit_user_funding(monkeypatch):
    session = SimpleNamespace(
        add=MagicMock(),
        commit=AsyncMock(),
        refresh=AsyncMock(),
    )
    service = ShadowService(session)
    monkeypatch.setattr(
        service,
        "_portfolio_snapshot",
        AsyncMock(
            return_value={
                "experiment_context": {},
                "snapshot_summary": {"total_market_value": 50_000.0},
                "shadow_state": {
                    "cash": 0.0,
                    "cash_reserved": 0.0,
                    "positions": [{"security_id": str(uuid4()), "quantity": 5.0}],
                },
            }
        ),
    )
    monkeypatch.setattr(
        service, "_get_or_create_family_state", AsyncMock(return_value=None)
    )

    experiment = await service.create_experiment(
        ShadowExperimentCreate(
            name="Cash-only account",
            policy_description="User-funded paper account.",
            auto_run=False,
            account_basis="cash_only",
            starting_cash=25_000.0,
        )
    )

    state = experiment.initial_portfolio_state_json
    assert state["experiment_context"]["account_basis"] == "cash_only"
    assert state["shadow_state"] == {
        "cash": 25_000.0,
        "cash_reserved": 0.0,
        "positions": [],
    }
    assert state["run_details"]["paper_account"]["equity"] == 25_000.0
    assert state["snapshot_summary"]["paper_starting_cash"] == 25_000.0


@pytest.mark.asyncio
async def test_cash_only_paper_account_requires_positive_explicit_funding():
    service = ShadowService(session=None)

    with pytest.raises(ValueError, match="positive starting_cash"):
        await service.create_experiment(
            ShadowExperimentCreate(
                name="Invalid account",
                policy_description="Missing funding.",
                auto_run=False,
                account_basis="cash_only",
            )
        )


@pytest.mark.asyncio
async def test_queueing_manual_account_explicitly_converts_it_to_autonomous_mode(
    monkeypatch,
):
    experiment = SimpleNamespace(
        run_status="manual",
        initial_portfolio_state_json={
            "experiment_context": {"execution_mode": "manual"}
        },
        skip_reason=None,
        completed_at=None,
    )
    session = SimpleNamespace(commit=AsyncMock(), refresh=AsyncMock())
    service = ShadowService(session)
    monkeypatch.setattr(service, "get_experiment", AsyncMock(return_value=experiment))

    queued = await service.queue_experiment_run(uuid4())

    assert queued.run_status == "queued"
    assert (
        queued.initial_portfolio_state_json["experiment_context"]["execution_mode"]
        == "autonomous"
    )


@pytest.mark.asyncio
async def test_new_manual_account_serializes_initial_paper_balance():
    class EmptyResult:
        def scalars(self):
            return self

        def all(self):
            return []

        def scalar_one_or_none(self):
            return None

    experiment = SimpleNamespace(
        id=uuid4(),
        name="New manual account",
        policy_description="Manual paper account.",
        start_point=None,
        end_point=None,
        initial_portfolio_state_json={
            "experiment_context": {
                "execution_mode": "manual",
                "account_basis": "cash_only",
            },
            "snapshot_summary": {"paper_starting_cash": 100_000.0},
            "shadow_state": {"cash": 100_000.0, "positions": []},
            "run_details": {"paper_account": {"cash": 100_000.0, "equity": 100_000.0}},
        },
        final_portfolio_state_json=None,
        run_status="manual",
        skip_reason=None,
        created_at=None,
        completed_at=None,
    )
    session = SimpleNamespace(execute=AsyncMock(return_value=EmptyResult()))

    serialized = await ShadowService(session).serialize_experiment(experiment)

    assert serialized["run_details"]["paper_account"]["cash"] == 100_000.0
    assert serialized["paper_positions"] == []


@pytest.mark.asyncio
async def test_experiment_summary_omits_heavy_run_history():
    class EmptyResult:
        def scalar_one_or_none(self):
            return None

    experiment = SimpleNamespace(
        id=uuid4(),
        name="Long-running paper account",
        policy_description="Test a source-backed policy.",
        start_point=None,
        end_point=None,
        initial_portfolio_state_json={
            "experiment_context": {},
            "snapshot_summary": {"holding_count": 3},
        },
        final_portfolio_state_json={
            "run_details": {
                "progress": {"step_count": 2, "target_steps": 4},
                "guidance": {"guidance_mode": "wait_for_confirmation"},
                "run_log": [{"ticker": "MEMB"}] * 100,
                "decision_history": [{"step_index": 1}] * 100,
                "paper_account": {"cash": 50_000.0},
            },
            "report": {
                "policy_assessment": "The policy remains provisional.",
                "key_lesson": "Repeat before trusting the result.",
                "thesis_context": [{"ticker": "MEMB"}] * 100,
            },
        },
        run_status="completed",
        skip_reason=None,
        created_at=None,
        completed_at=None,
    )
    session = SimpleNamespace(execute=AsyncMock(return_value=EmptyResult()))

    serialized = await ShadowService(session).serialize_experiment(
        experiment,
        include_details=False,
    )

    assert serialized["run_details"] == {
        "progress": {"step_count": 2, "target_steps": 4},
        "guidance": {"guidance_mode": "wait_for_confirmation"},
    }
    assert serialized["report"] == {
        "policy_assessment": "The policy remains provisional.",
        "key_lesson": "Repeat before trusting the result.",
    }
    assert serialized["actions"] == []
    assert serialized["orders"] == []
    assert serialized["fills"] == []
    assert serialized["paper_positions"] == []
