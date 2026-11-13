import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import select

from investos.models.conclusion import ConclusionState
from investos.models.reasoning import EvidencePacket
from investos.services.agent import AgentService
from investos.services.operating_state import OperatingStateService
from investos.services.portfolio_risk import PortfolioRiskService
from investos.services.quantity_evidence import portfolio_measurements
from investos.services.reasoning import ReasoningService
from investos.services.research import ResearchService
from investos.services.retrieval import RetrievalService
from tests.test_corroboration import _result
from tests.test_portfolio_risk import frozen_packet
from tests.test_quantity_evidence import binding, sample
from tests.test_question_lifecycle import question_session


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "stale",
        "changed_book",
        "future_book",
        "foreign_currency",
        "short_history",
        "invalid_source",
    ],
)
async def test_quantity_adapter_reuses_risk_eligibility_without_confusing_stress_sample_size(
    monkeypatch, fault
):
    packet = frozen_packet()
    cutoff = datetime.fromisoformat(packet["cutoff"])

    class FrozenClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cutoff + (timedelta(days=10) if fault == "stale" else timedelta())

    monkeypatch.setattr("investos.services.quantity_evidence.datetime", FrozenClock)
    holdings = deepcopy(packet["positions"])
    for item in holdings:
        item["quantity"] = str(item["quantity"])
    if fault == "changed_book":
        holdings[0]["quantity"] = "11"
    if fault == "future_book":
        packet["portfolio_captured_at"] = (cutoff + timedelta(hours=1)).isoformat()
    if fault == "foreign_currency":
        next(iter(packet["snapshots"].values()))["currency"] = "KRW"
    if fault == "short_history":
        for snapshot in packet["snapshots"].values():
            snapshot["daily_observations"] = snapshot["daily_observations"][-10:]
    if fault == "invalid_source":
        packet = {}
    packet = json.loads(json.dumps(packet, default=lambda value: value.isoformat()))
    monkeypatch.setattr(
        PortfolioRiskService,
        "latest",
        AsyncMock(
            return_value=SimpleNamespace(id=uuid4(), shock_parameters_json=packet)
        ),
    )
    measurements = await portfolio_measurements(None, holdings, None)
    mark = next(
        item
        for item in measurements
        if item["basis"] == "captured_close_holdings_value_excluding_cash"
    )
    assert (mark["status"] == "complete") == (fault in {None, "short_history"})
    assert all(
        item["unit"] == "shares"
        for item in measurements
        if item["basis"] == "recorded_ledger_quantity"
    )


@pytest.mark.parametrize("bad", [True, False])
async def test_reasoning_checks_quantities_before_persisting_accepted_state(
    question_session, monkeypatch, bad
):
    from investos.core.provider_health import ProviderHealthStore

    session = question_session
    subject = uuid4()
    measured = sample()
    packet = EvidencePacket(
        query_text="Review the recorded portfolio value",
        subject_type="portfolio",
        subject_id=subject,
        portfolio_context_json={"quantity_measurements": [measured]},
        direct_evidence_ids=[],
        connected_evidence_ids=[],
        historical_evidence_ids=[],
        contradiction_evidence_ids=[],
        lesson_ids=[],
    )
    session.add(packet)
    await session.flush()
    text = (
        "The recorded holdings value is $10 million."
        if bad
        else "The recorded holdings value is $10,000."
    )
    result = _result([])
    result.update(reasoning=text, thesis_summary=text)
    result["material_assertions"][0].update(
        statement=text,
        evidence_basis="portfolio_ledger",
        supporting_context_paths=["portfolio_context.total_market_value"],
        quantity_bindings=[
            binding(
                measured,
                "$10 million" if bad else "$10,000",
                value=10000000 if bad else 10000,
            )
        ],
    )
    context = {
        "query_text": packet.query_text,
        "subject_type": "portfolio",
        "subject_id": str(subject),
        "subject_name": "Synthetic portfolio",
        "portfolio_context": {"total_market_value": 10000},
        "quantity_measurements": [measured],
        "direct_evidence": [],
        "connected_evidence": [],
        "historical_evidence": [],
        "contradiction_evidence": [],
    }
    monkeypatch.setattr(
        RetrievalService, "hydrate_packet", AsyncMock(return_value=context)
    )
    monkeypatch.setattr(
        ReasoningService, "_active_provider_unconfigured_label", lambda self: None
    )
    monkeypatch.setattr(
        ReasoningService, "_find_cached_run", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(ProviderHealthStore, "require_available", lambda **kwargs: None)
    monkeypatch.setattr(
        ReasoningService, "_reason_with_llm", AsyncMock(return_value=result)
    )
    run, assessed = await ReasoningService(session).run_analysis(
        packet.id, include_critique=False
    )
    await session.flush()
    assert assessed["corroboration"]["can_promote"] is (not bad)
    assert run.structured_output_json["corroboration"]["can_promote"] is (not bad)
    assert run.output_text == text
    state = await session.scalar(
        select(ConclusionState).where(ConclusionState.subject_id == subject)
    )
    assert state is not None
    assert state.current_thesis_summary == ("" if bad else text)
    assert state.current_stance == ("no_view" if bad else "bullish")


@pytest.mark.parametrize("bad", [True, False])
async def test_strategist_does_not_seed_research_with_invalid_account_quantities(
    question_session, monkeypatch, bad
):
    import investos.services.agent as module
    from investos.services.agent_action_log import AgentActionLogService

    measured = sample()
    monkeypatch.setattr(
        OperatingStateService,
        "portfolio_state_payload",
        AsyncMock(
            return_value={
                "top_holdings": [{"ticker": "EXMPL"}],
                "quantity_measurements": [measured],
            }
        ),
    )
    monkeypatch.setattr(
        OperatingStateService,
        "portfolio_monitor_payload",
        AsyncMock(return_value={"priority_review_items": [{"id": "synthetic-signal"}]}),
    )
    monkeypatch.setattr(
        OperatingStateService,
        "review_queue_payload",
        AsyncMock(return_value={"top_items": [{"id": "synthetic-review"}]}),
    )
    monkeypatch.setattr(
        OperatingStateService,
        "lessons_payload",
        AsyncMock(return_value={"recent_lessons": []}),
    )
    monkeypatch.setattr(
        AgentActionLogService, "has_recent_fingerprint", lambda *args, **kwargs: False
    )
    goal = {
        "goal_name": "Review concentration",
        "target_ticker": "EXMPL",
        "action": "research",
        "portfolio_connection": "direct_holding",
        "why_now": "A new filing needs review",
        "rationale": (
            "Holdings are worth $10 million."
            if bad
            else "Recorded holdings are worth $10,000."
        ),
        "quantity_bindings": [
            binding(
                measured,
                "$10 million" if bad else "$10,000",
                value=10000000 if bad else 10000,
            )
        ],
    }
    monkeypatch.setattr(
        module, "call_llm_json", AsyncMock(return_value={"strategic_goals": [goal]})
    )
    research = AsyncMock(return_value=SimpleNamespace(started=True, loop_detail=None))
    monkeypatch.setattr(ResearchService, "run_ad_hoc_request", research)
    result = await AgentService(question_session).run_strategic_planning_cycle()
    assert result["actions_taken"] == (0 if bad else 1)
    assert result["goals"][0]["quantity_verification"]["verified"] is (not bad)
    assert research.await_count == (0 if bad else 1)


async def test_thin_and_recovery_prompts_retain_measurement_catalog(monkeypatch):
    import investos.services.reasoning as module

    measured = sample()
    context = {
        "quantity_measurements": [measured],
        "portfolio_context": {"quantity_measurements": [measured]},
    }
    model = AsyncMock(return_value={"material_assertions": []})
    monkeypatch.setattr(module, "call_llm_json", model)
    service = ReasoningService(None)
    await service._thin_packet_reasoning(context)
    assert json.loads(model.call_args.kwargs["user_prompt"])[
        "quantity_measurements"
    ] == [measured]
    assert service._recovery_packet_context(context)["quantity_measurements"] == [
        measured
    ]


async def test_independent_reviewer_does_not_launder_unbound_amounts(
    question_session, monkeypatch
):
    import investos.services.reasoning as module

    monkeypatch.setattr(
        ReasoningService, "_active_provider_unconfigured_label", lambda self: None
    )
    monkeypatch.setattr(module.settings, "REASONING_INDEPENDENT_REVIEW_ENABLED", True)
    raw = {
        "candidate_stance": "bullish",
        "confidence_band": "high",
        "independent_summary": "The account is worth $10 million.",
        "question_answerability": "adequate",
        "material_assertions": [],
        "assumptions": [],
        "issues_found": [],
        "severity": "none",
    }
    monkeypatch.setattr(module, "call_llm_json", AsyncMock(return_value=raw))
    review = await ReasoningService(question_session)._create_critique(
        SimpleNamespace(id=uuid4()), {"quantity_measurements": [sample()]}, {}
    )
    assert review["summary"] == raw["independent_summary"]
    assert review["question_answerability"] == "inadequate"
    assert not review["quantity_grounding"]["verified"]
