import pytest

from investos.services.agent import AGENT_TOOLS, AgentService


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
