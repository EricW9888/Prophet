from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from investos.schemas.agent import AgentTurnRequest
from investos.services.agent import AGENT_TOOLS, AgentService
from investos.services.research import ResearchSearchResult, ResearchService


def test_fallback_router_preserves_explicit_follow_up_correction():
    intent = AgentService(None)._fallback_turn_intent(
        "I mean available for trading in the US"
    )

    assert intent["route"] == "continue"


def test_listing_access_question_requires_fresh_research():
    requirement = AgentService._fallback_fresh_context_requirement(
        "Is this available for trading on a US exchange?"
    )

    assert requirement["required"] is True


def test_fallback_intent_carries_freshness_as_a_safety_signal():
    intent = AgentService(None)._fallback_turn_intent(
        "I mean available for trading in the US"
    )

    assert intent["route"] == "continue"
    assert intent["requires_fresh_research"] is True
    assert intent["freshness_reason"]


def test_turn_intent_schema_assigns_freshness_to_the_llm_classifier():
    from investos.services.agent import TURN_INTENT_SCHEMA

    assert "requires_fresh_research" in TURN_INTENT_SCHEMA["properties"]
    assert "freshness_reason" in TURN_INTENT_SCHEMA["properties"]
    assert "requires_fresh_research" in TURN_INTENT_SCHEMA["required"]


@pytest.mark.asyncio
async def test_classifier_freshness_decision_overrides_fallback_phrase_gate():
    service = AgentService(None)

    result = await service._maybe_fresh_research_context(
        message="latest operating margin",
        subject_name="Example Co.",
        subject_type="entity",
        freshness_requirement={
            "requires_fresh_research": False,
            "freshness_reason": "",
        },
    )

    assert result is None


@pytest.mark.asyncio
async def test_vague_portfolio_update_checks_market_industry_and_loss_driver(
    monkeypatch,
):
    async def discover(_self, *, query, **kwargs):
        generic = {"title": "World news today", "url": "https://example.test/generic"}
        matches = {
            "Portfolio update: Market": {
                "title": "Nasdaq fell",
                "url": "https://example.test/market",
            },
            "Portfolio update: Industries": {
                "title": "Memory Chips demand",
                "url": "https://example.test/industry",
            },
            "Portfolio update: Holdings": {
                "title": "SMALL guidance cut",
                "url": "https://example.test/holding",
            },
        }
        return ResearchSearchResult(
            True, "ok", query, [generic, matches[kwargs["title"]]]
        )

    monkeypatch.setattr(ResearchService, "search", discover)
    holdings = [
        {
            "ticker": f"BIG{i}",
            "name": f"Big {i}",
            "industry": "Memory Chips",
            "market_value": 200 - i,
        }
        for i in range(5)
    ]
    holdings.append(
        {
            "ticker": "SMALL",
            "name": "Smaller issuer",
            "industry": "Memory Chips",
            "market_value": 20,
        }
    )
    result = await AgentService(None)._maybe_fresh_research_context(
        message="what happened recently",
        subject_name="Portfolio",
        subject_type="portfolio",
        packet_context={
            "portfolio_context": {
                "holdings": holdings,
                "performance_attribution": {
                    "items": [
                        {
                            "ticker": "SMALL",
                            "contribution_pct": -4,
                            "data_status": "complete",
                        }
                    ]
                },
            }
        },
        question_research_plan={
            "external_research_required": True,
            "retrieval_query": "what happened recently",
        },
    )
    assert [item["lane"] for item in result["checks"]] == [
        "Market",
        "Industries",
        "Holdings",
    ]
    assert "SMALL" in result["checks"][2]["query"]
    assert all(item["result_count"] == 1 for item in result["checks"])
    assert [item["url"] for item in result["results"]] == [
        f"https://example.test/{path}" for path in ("market", "industry", "holding")
    ]


@pytest.mark.asyncio
async def test_attachment_turn_uses_read_only_model_path_without_saving_input_file(
    monkeypatch,
):
    model = AsyncMock(
        return_value={
            "answer": "The report says twelve.",
            "rationale": "Read the attached count.",
        }
    )
    monkeypatch.setattr("investos.services.agent.call_llm_json", model)
    service = AgentService(None)
    persist = AsyncMock()
    monkeypatch.setattr(service, "_persist_conversation_exchange", persist)
    response = await service.handle_turn(
        AgentTurnRequest(
            message="What is the count?",
            attachments=[
                {"name": "report.csv", "text": "count,12\nattachment-only secret"}
            ],
        )
    )
    assert response.process_mode == "attached_text"
    assert response.actions == []
    assert "attachment-only secret" in model.await_args.kwargs["user_prompt"]
    assert persist.await_args.kwargs["user_message"].endswith("Files: report.csv")
    assert "attachment-only secret" not in str(persist.await_args.kwargs)
    with pytest.raises(ValidationError, match="24 KB"):
        AgentTurnRequest(
            message="Too large",
            attachments=[{"name": "report.txt", "text": "é" * 12_001}],
        )


@pytest.mark.parametrize(
    ("auto_execute", "has_fresh_research", "expected"),
    [
        (True, False, True),
        (True, True, False),
        (False, False, False),
        (False, True, False),
    ],
)
def test_state_updates_require_explicit_execution_and_durable_evidence(
    auto_execute, has_fresh_research, expected
):
    assert (
        AgentService._state_updates_allowed(
            auto_execute=auto_execute,
            has_fresh_research=has_fresh_research,
        )
        is expected
    )


def test_preview_mode_cannot_register_model_proposed_watchers():
    reasoning_result = {"active_watchers": [{"ticker": "MEMB"}]}

    assert AgentService._watchers_allowed(reasoning_result, auto_execute=False) == []
    assert AgentService._watchers_allowed(reasoning_result, auto_execute=True) == [
        {"ticker": "MEMB"}
    ]


def test_uncorroborated_result_cannot_create_a_decision_but_keeps_other_tests():
    orchestration = {
        "decision": {"should_create": True, "rationale": "Act now."},
        "shadow": {"should_create": True},
        "verification": {"should_create": True},
    }

    result = AgentService._enforce_action_evidence_boundary(
        orchestration,
        {"corroboration": {"can_promote": False}},
    )

    assert result["decision"]["should_create"] is False
    assert "independently corroborated" in result["decision"]["rationale"]
    assert result["shadow"]["should_create"] is True
    assert result["verification"]["should_create"] is True


def test_agent_exposes_open_window_performance_attribution_tool():
    tools = {
        item["function"]["name"]: item["function"]
        for item in AGENT_TOOLS
        if item.get("type") == "function"
    }

    attribution = tools["get_performance_attribution"]
    assert attribution["parameters"]["properties"]["days"]["type"] == "number"
    assert "dated prices" in attribution["description"]


def test_agent_exposes_audited_knowledge_relevance_correction_tool():
    tools = {
        item["function"]["name"]: item["function"]
        for item in AGENT_TOOLS
        if item.get("type") == "function"
    }

    reassessment = tools["reassess_knowledge_relevance"]
    assert set(reassessment["parameters"]["required"]) == {"node_type", "node_id"}
    assert "deprecate unsupported" in reassessment["description"]
    assert "audit trail" in reassessment["description"]


def test_deterministic_attribution_answer_reports_measured_drags_and_boundary():
    result = AgentService(None)._deterministic_operating_answer(
        {
            "query_type": "performance_attribution",
            "performance_attribution": {
                "period_start": "2026-06-23T00:00:00Z",
                "as_of": "2026-07-14T00:00:00Z",
                "gain": -753.63,
                "return_pct": -6.67,
                "benchmark_ticker": "SPY",
                "benchmark_return_pct": 2.54,
                "active_return_pct": -9.21,
                "coverage_pct": 100,
                "covered_positions": 2,
                "total_positions": 2,
                "unavailable_tickers": [],
                "items": [
                    {
                        "ticker": "MEMX",
                        "gain": -243.25,
                        "contribution_pct": -2.15,
                        "capital_return_pct": -20.58,
                    },
                    {
                        "ticker": "AUTO",
                        "gain": 123.90,
                        "contribution_pct": 1.10,
                        "capital_return_pct": 5.50,
                    },
                ],
            },
        }
    )

    assert result is not None
    message = result["assistant_message"]
    assert "lost $753.63" in message
    assert "MEMX" in message
    assert "AUTO" in message
    assert "not a claim about why" in message
