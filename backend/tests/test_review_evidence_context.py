from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import select

from investos.core.prompting import compact_packet_context
from investos.models.knowledge import Fact
from investos.models.reasoning import ReasoningRun
from investos.models.source import Source
from investos.schemas.agent import AgentTurnRequest
from investos.services.agent import AgentService
from investos.services.corroboration import near_duplicate_signature
from investos.services.quantity_evidence import qualify_numeric_message
from investos.services.reasoning import ReasoningService
from tests.test_corroboration import _node, _packet, _result
from tests.test_investor_workflows import source_records, workflow_session
from tests.test_question_scope import isolated_scope_runtime


def review_payload(assertions=None, summary="The issuer announced collaboration."):
    return {
        "candidate_stance": "neutral",
        "confidence_band": "medium",
        "independent_summary": summary,
        "question_answerability": "partial",
        "answerability_reason": "The announcement is evidenced; fulfillment remains unknown.",
        "missing_information": ["later fulfillment evidence"],
        "material_assertions": assertions or [],
        "assumptions": [],
        "alternative_hypotheses": [],
        "issues_found": [],
        "severity": "minor",
    }


@pytest.mark.parametrize("copy_kind", ["exact", "near"])
@pytest.mark.parametrize("planner_timeout", [False, True])
async def test_chat_and_analysis_review_validate_full_evidence_and_duplicate_lineage(
    workflow_session, monkeypatch, copy_kind, planner_timeout
):
    session = workflow_session
    topic = f"Zythera{uuid4().hex[:8]} supplier collaboration"
    facts = []
    signature, token_count = near_duplicate_signature(
        "Synthetic wire report on a supplier collaboration and its announced qualification program. "
        * 12
    )
    for index, publisher in enumerate(
        ("issuer.example", "wire.example", "copy.example")
    ):
        raw, item = await source_records(session)
        source = await session.get(Source, raw.source_id)
        source.url = raw.url = f"https://{publisher}/announcement"
        source.source_type = "official" if index == 0 else "news"
        raw.source_item_type = "url"
        raw.public_time = datetime(2026, 1, 1, tzinfo=UTC) if index < 2 else None
        raw.content_hash = (
            "issuer"
            if index == 0
            else "wire" if copy_kind == "exact" else f"wire-{index}"
        )
        if index and copy_kind == "near":
            raw.metadata_json = {
                "near_duplicate_signature": signature,
                "signature_token_count": token_count,
            }
        fact = Fact(
            source_item_id=item.id,
            statement=f"{topic}: the issuer announced collaboration. "
            + "Synthetic source context retained for validation. " * 25,
            fact_type="partnership",
            confidence=0.8,
            tier="hard_fact",
            importance="high",
            directness="primary" if index == 0 else "secondary",
            novelty="confirming",
            contradiction_role="neutral",
        )
        session.add(fact)
        facts.append(fact)
    await session.flush()
    primary = _result([str(fact.id) for fact in facts])
    primary.update(
        stance="neutral",
        thesis_summary="The issuer announced collaboration.",
        reasoning="Fulfillment remains unknown.",
    )
    primary["material_assertions"][0].update(
        statement="The issuer announced collaboration.", subject_scope=topic
    )
    reply = review_payload(primary["material_assertions"])
    payloads = []

    async def model(**kwargs):
        import json

        payloads.append(json.loads(kwargs["user_prompt"])["packet_context"])
        return deepcopy(reply)

    monkeypatch.setattr("investos.services.reasoning.call_llm_json", model)
    monkeypatch.setattr(
        ReasoningService,
        "_reason_with_llm",
        AsyncMock(side_effect=lambda *args, **kwargs: deepcopy(primary)),
    )
    monkeypatch.setattr(
        ReasoningService, "_active_provider_unconfigured_label", lambda self: None
    )
    original_review = ReasoningService._create_critique

    async def complete_snapshot(self, run, context, result, **kwargs):
        # A compact model excerpt is deliberately shorter than the evidence
        # that the independent validator must receive from either caller.
        assert all(len(row["text"]) > 500 for row in context["direct_evidence"])
        review = await original_review(self, run, context, result, **kwargs)
        assessment = review["corroboration"]
        assert assessment["independent_supporting_source_count"] == 2
        assert assessment["duplicate_copy_count"] == 1
        assert assessment["assertions"][0]["primary_source_count"] == 1
        assert review["question_answerability"] == "partial"
        return review

    monkeypatch.setattr(ReasoningService, "_create_critique", complete_snapshot)
    monkeypatch.setattr(
        AgentService,
        "_classify_turn_intent",
        AsyncMock(
            return_value={
                "route": "analyze",
                "subject_hint": {"subject_kind": "theme", "search_text": topic},
            }
        ),
    )
    monkeypatch.setattr(
        "investos.services.agent.call_llm_json", AsyncMock(side_effect=TimeoutError())
    )
    monkeypatch.setattr(
        AgentService, "_dispatch_dynamic_subagents", AsyncMock(return_value={})
    )
    monkeypatch.setattr(
        AgentService, "_maybe_auto_research_after_gap", AsyncMock(return_value=None)
    )

    async def unchanged(self, payload, resolved, context, result, lenses):
        return result

    async def answer_plan(self, **kwargs):
        return self._fast_path_orchestration(
            kwargs["payload"], kwargs["reasoning_result"]
        )

    monkeypatch.setattr(AgentService, "_synthesize_and_refine", unchanged)
    if not planner_timeout:
        monkeypatch.setattr(AgentService, "_plan_agent_actions", answer_plan)
    response = await AgentService(session).handle_turn(
        AgentTurnRequest(message=topic, auto_execute=False)
    )
    assert "announced collaboration" in response.assistant_message
    assert "Fulfillment remains unknown." in response.assistant_message
    assert "later fulfillment evidence" in response.assistant_message
    assert not response.actions
    if planner_timeout:
        assert "action planner timed out" in response.rationale_summary
    assert "defensible answer" not in response.assistant_message
    run = await session.get(ReasoningRun, response.reasoning_run_id)
    await ReasoningService(session).run_analysis(
        run.evidence_packet_id, include_critique=True, allow_state_update=False
    )
    assert len(payloads) == 2
    for packet in payloads:
        nodes = packet["direct_evidence"]
        assert all(len(row["text"]) <= 320 for row in nodes)
        assert {row["source"]["publisher_host"] for row in nodes} == {
            "issuer.example",
            "wire.example",
            "copy.example",
        }
        assert all(row["source"]["lineage_key"] for row in nodes)
        assert all(row["source"]["is_trusted"] is False for row in nodes)
        assert all(row["source"]["acquisition_kind"] == "url" for row in nodes)
        sources = {row["source"]["publisher_host"]: row["source"] for row in nodes}
        assert sources["issuer.example"]["public_time"]
        assert sources["wire.example"]["public_time"]
        assert sources["copy.example"].get("public_time") is None


@pytest.mark.parametrize("answerability", ["partial", "adequate"])
@pytest.mark.parametrize("stance", ["neutral", "bearish"])
async def test_invalid_review_quantities_preserve_coverage_and_block_actions(
    monkeypatch, answerability, stance
):
    service = ReasoningService(SimpleNamespace(add=lambda value: None))
    monkeypatch.setattr(service, "_active_provider_unconfigured_label", lambda: None)
    payload = review_payload(summary="Unverified account value is $900 million.")
    payload.update(question_answerability=answerability, candidate_stance=stance)
    monkeypatch.setattr(
        "investos.services.reasoning.call_llm_json",
        AsyncMock(return_value=payload),
    )
    review = await service._create_critique(
        SimpleNamespace(id=uuid4()), {}, {"stance": "neutral"}
    )
    assert review["question_answerability"] == answerability
    assert review["quantity_grounding"]["verified"] is False
    first, second = _node(publisher="issuer.example", content_hash="a"), _node(
        publisher="wire.example", content_hash="b"
    )
    result = _result([first["id"], second["id"]])
    from investos.services.corroboration import CorroborationService

    corroboration = CorroborationService()
    corroboration.assess_result(result, _packet(first, second))
    assert result["corroboration"]["can_promote"]
    corroboration.apply_independent_review(result, review)
    AgentService._apply_independent_answerability_gate(
        result, review, question_research_plan={}
    )
    assert result["answerability"]["status"] == answerability
    assert not result["corroboration"]["can_promote"]
    assert result["corroboration"]["confidence_cap"] == "very_low"
    assert result["confidence_band"] == "very_low"
    assert "superseded_candidate" not in result
    assert (
        AgentService._watchers_allowed(
            {**result, "active_watchers": [{"threshold": 999}]}, auto_execute=True
        )
        == []
    )
    proposal = {
        "decision": {"should_create": True},
        "shadow_experiment": {"should_create": True},
        "pruning": {"should_run": True},
        "memory_note": "Unverified account value",
    }
    AgentService._enforce_action_evidence_boundary(proposal, result)
    assert not proposal["shadow_experiment"]["should_create"]
    assert not proposal["pruning"]["should_run"]
    assert not proposal["memory_note"]


def test_numeric_warning_only_describes_financial_quantities_in_the_delivered_answer():
    state = {"quantity_grounding": {"verified": False}}
    assert (
        qualify_numeric_message("The requested current evidence is missing.", state)
        == "The requested current evidence is missing."
    )
    assert qualify_numeric_message("The account is $900 million.", state).startswith(
        "**Numerical claims need verification."
    )
    assert state["quantity_grounding"]["verified"] is False
