from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from investos.schemas.agent import AgentTurnRequest
from investos.services.agent import AGENT_TOOLS, AgentService
from investos.services.portfolio_lookahead import PortfolioLookaheadService
from investos.services.research import ResearchSearchResult, ResearchService


@pytest.mark.parametrize(
    "message",
    [
        "Pressure-test this thesis: a major supplier replaced its CEO today. What are the portfolio implications and which catalysts could disprove it?",
        "Analyze today's earnings report and its impact on my holdings.",
        "What does today's executive departure mean for memory demand?",
        "Evaluate this investment thesis without changing my portfolio or loss budget.",
    ],
)
async def test_substantive_analysis_is_not_a_calendar_shortcut(monkeypatch, message):
    assert not PortfolioLookaheadService.looks_like_lookahead_request(message)
    service = AgentService(None)
    assert service._fallback_turn_intent(message)["route"] == "analyze"
    classify = AsyncMock(
        return_value={"route": "analyze", "requires_fresh_research": True}
    )
    monkeypatch.setattr("investos.services.agent.call_llm_json", classify)
    assert (await service._classify_turn_intent(message))["route"] == "analyze"
    classify.assert_awaited_once()


@pytest.mark.parametrize(
    "message",
    [
        "What should I watch next week?",
        "anything i should look forward/pay attention to the next week?",
        "Show my upcoming earnings calendar.",
        "Which portfolio events are coming up?",
    ],
)
def test_explicit_calendar_questions_keep_the_fast_path(message):
    assert PortfolioLookaheadService.looks_like_lookahead_request(message)


def test_fallback_router_preserves_explicit_follow_up_correction():
    intent = AgentService(None)._fallback_turn_intent(
        "I mean available for trading in the US"
    )

    assert intent["route"] == "continue"


@pytest.mark.asyncio
async def test_market_check_keeps_dated_unheld_company_news_and_rejects_stale_matches(
    monkeypatch,
):
    now = datetime.now(UTC)

    async def discover(_self, **kwargs):
        assert kwargs["news_window_hours"] == 24
        rows = (
            [
                {
                    "title": "A major supplier replaces its CEO",
                    "url": "https://example.test/new",
                    "published_date": (now - timedelta(minutes=5)).isoformat(),
                },
                {
                    "title": "Nasdaq index wrap",
                    "url": "https://example.test/old",
                    "published_date": (now - timedelta(days=90)).isoformat(),
                },
                {"title": "Nasdaq index wrap", "url": "https://example.test/unknown"},
            ]
            if kwargs["title"] == "Portfolio update: Market"
            else []
        )
        return ResearchSearchResult(True, "ok", kwargs["query"], rows)

    monkeypatch.setattr(ResearchService, "search", discover)
    result = await AgentService(None)._portfolio_fresh_checks(
        {}, "Current developments"
    )
    assert [i["url"] for i in result["results"]] == ["https://example.test/new"]
    assert result["coverage"] == "bounded_search_not_exhaustive"
    assert result["checks"][0]["window_hours"] == 24


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
@pytest.mark.parametrize(
    ("message", "scope"),
    [
        ("what happened recently", None),
        (
            "Give me a daily portfolio review: what changed recently, which holdings have current source-backed evidence, what material risks are unsupported, and what is the next useful check?",
            None,
        ),
        (
            "Walk through the book and prioritize what deserves investigation.",
            "portfolio_wide",
        ),
    ],
)
async def test_vague_portfolio_update_checks_market_industry_and_loss_driver(
    monkeypatch,
    message,
    scope,
):
    async def discover(_self, *, query, **kwargs):
        assert kwargs["news_window_hours"] == 24
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
            True,
            "ok",
            query,
            [
                generic,
                {
                    **matches[kwargs["title"]],
                    "published_date": (
                        datetime.now(UTC) - timedelta(minutes=5)
                    ).isoformat(),
                },
            ],
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
        message=message,
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
            **({"coverage_scope": scope} if scope else {}),
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
async def test_explicit_question_scope_keeps_macro_research_focused(monkeypatch):
    search = AsyncMock(
        return_value=ResearchSearchResult(True, "ok", "credit spreads", [])
    )
    monkeypatch.setattr(ResearchService, "search", search)
    service = AgentService(None)
    portfolio_checks = AsyncMock(
        side_effect=AssertionError(
            "A focused question must not become a whole-book recap"
        )
    )
    monkeypatch.setattr(service, "_portfolio_fresh_checks", portfolio_checks)
    await service._maybe_fresh_research_context(
        message="What changed recently in credit spreads, and how does that affect my portfolio?",
        subject_name="Portfolio",
        subject_type="portfolio",
        freshness_requirement={"requires_fresh_research": True},
        question_research_plan={
            "coverage_scope": "question_specific",
            "external_research_required": True,
            "portfolio_context_role": "secondary_read_through",
            "retrieval_query": "US investment grade high yield credit spreads recent change transmission",
        },
    )
    search.assert_awaited_once()
    assert search.await_args.kwargs["query"].startswith("US investment grade")
    portfolio_checks.assert_not_awaited()


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
