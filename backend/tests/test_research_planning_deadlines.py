import asyncio
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from investos.config import settings
from investos.core.provider_health import ProviderHealthStore, guarded_provider_call
from investos.services.agent import AgentService
from investos.services.chat_turn_state import ChatTurnState


@pytest.fixture(autouse=True)
def isolated_provider_health(tmp_path, monkeypatch):
    monkeypatch.setattr(
        ProviderHealthStore, "_path", staticmethod(lambda: tmp_path / "health.json")
    )


def portfolio_plan():
    return {
        "research_objective": "Prioritize current portfolio developments.",
        "retrieval_query": "current portfolio holdings risks and material developments",
        "information_needs": ["Current holdings and dated material developments"],
        "external_research_required": True,
        "research_mode": "focused_external",
        "portfolio_context_role": "primary_evidence",
        "coverage_scope": "portfolio_wide",
        "use_historical_analogies": False,
        "reason": "The question asks for current priorities across the book.",
    }


async def plan(service):
    return await service._plan_question_research(
        message="What deserves my attention now, and why?",
        subject_name="Synthetic portfolio",
        subject_type="portfolio",
        freshness_requirement={"requires_fresh_research": True},
    )


async def test_slow_plan_retains_portfolio_acquisition_scope(monkeypatch):
    monkeypatch.setattr(settings, "LLM_TIMEOUT_SECONDS", 10)

    async def slow_model(**kwargs):
        await asyncio.sleep(6.05)
        return portfolio_plan()

    monkeypatch.setattr("investos.services.agent.call_llm_json", slow_model)
    result = await plan(AgentService(None))
    assert result["planner_fallback"] is False
    assert result["coverage_scope"] == "portfolio_wide"
    assert result["retrieval_query"] == portfolio_plan()["retrieval_query"]


@pytest.mark.parametrize("failure", [TimeoutError, httpx.ReadTimeout])
async def test_durable_planning_failure_does_not_checkpoint_literal_fallback(
    monkeypatch, failure
):
    service = AgentService(None)
    saved = ChatTurnState.initial()
    persist = Mock()
    service._turn_state = ChatTurnState(saved, persist)
    model = AsyncMock(side_effect=[failure("synthetic capacity"), portfolio_plan()])
    monkeypatch.setattr("investos.services.agent.call_llm_json", model)
    with pytest.raises(failure):
        await service._turn_state.run("research_plan", lambda: plan(service))
    assert "research_plan" not in saved["stages"]
    assert persist.call_count == 0

    result = await service._turn_state.run("research_plan", lambda: plan(service))
    assert result["coverage_scope"] == "portfolio_wide"
    assert result["planner_fallback"] is False
    assert saved["stages"]["research_plan"]["value"] == result
    assert model.await_count == 2
    assert (
        await service._turn_state.run("research_plan", lambda: plan(service)) == result
    )
    assert model.await_count == 2


async def test_configured_planning_deadline_and_direct_scoped_fallback(monkeypatch):
    monkeypatch.setattr(settings, "LLM_TIMEOUT_SECONDS", 0.02)
    cancelled = asyncio.Event()

    async def unavailable(**kwargs):
        try:
            await asyncio.Future()
        finally:
            cancelled.set()

    model = AsyncMock(side_effect=unavailable)
    monkeypatch.setattr(
        "investos.services.agent.call_llm_json", guarded_provider_call(model)
    )
    service = AgentService(None)
    result = await asyncio.wait_for(
        service._plan_question_research(
            message="Why does the dispatch halt matter?",
            subject_name="Portfolio",
            subject_type="portfolio",
            freshness_requirement={
                "requires_fresh_research": True,
                "subject_hint": {
                    "subject_kind": "theme",
                    "search_text": "Dispatch network halt",
                },
            },
        ),
        timeout=0.3,
    )
    assert result["planner_fallback"] is True
    assert result["coverage_scope"] == "question_specific"
    assert result["retrieval_query"] == "Dispatch network halt"
    assert cancelled.is_set()
    assert model.await_args.kwargs["timeout_seconds"] == 0.02


async def test_planning_cancellation_cannot_save_a_fallback(monkeypatch):
    service = AgentService(None)
    saved = ChatTurnState.initial()
    service._turn_state = ChatTurnState(saved, lambda: None)
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def unavailable(**kwargs):
        entered.set()
        try:
            await asyncio.Future()
        finally:
            cancelled.set()

    monkeypatch.setattr("investos.services.agent.call_llm_json", unavailable)
    task = asyncio.create_task(
        service._turn_state.run("research_plan", lambda: plan(service))
    )
    await asyncio.wait_for(entered.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set()
    assert "research_plan" not in saved["stages"]
