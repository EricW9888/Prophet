from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from investos.core.prompting import compact_packet_context
from investos.schemas.agent import AgentTurnRequest
from investos.services.agent import AgentService
from investos.services.answer_coverage import (
    assess_answer_coverage,
    render_answer_coverage,
    requested_answer_needs,
)
from investos.services.corroboration import CorroborationService
from investos.services.reasoning import ReasoningService
from tests.test_corroboration import _node, _packet, _result
from tests.test_quantity_evidence import binding, sample
from tests.test_review_evidence_context import review_payload


def fixture():
    node = _node(
        publisher="network.example", content_hash="notice", authority="primary"
    )
    node["text"] = (
        "The operator paused the shared dispatch network; connected depots could not dispatch deliveries."
    )
    node["source"].update(
        evidence_title="Operator notice", evidence_url="https://network.example/notice"
    )
    plan = {
        "original_question": "Why does the dispatch pause matter?",
        "research_objective": "Explain the pause's significance",
        "information_needs": ["Explain the immediate operational mechanism"],
        "optional_information_needs": ["Long-run history of similar pauses"],
    }
    packet = {**_packet(node), "research_plan": plan}
    assertion = _result([node["id"]])["material_assertions"][0]
    assertion.update(statement=node["text"], subject_scope="Dispatch network")
    payload = review_payload(assertions=[assertion])
    payload.update(
        question_answerability="inadequate",
        answerability_reason="Historical studies are missing.",
        missing_information=["Long-run history of similar pauses"],
        information_need_reviews=[
            {
                "need_index": 0,
                "status": "answered",
                "basis": "source_report",
                "answer": "The operator reports that connected depots could not dispatch deliveries because they depend on this shared network.",
                "assertion_indices": [0],
                "assumption_index": None,
                "limitation": "The notice does not quantify downstream losses.",
            }
        ],
    )
    return plan, packet, payload


async def reviewed(monkeypatch, packet, payload):
    service = ReasoningService(SimpleNamespace(add=lambda value: None))
    monkeypatch.setattr(service, "_active_provider_unconfigured_label", lambda: None)
    monkeypatch.setattr(
        "investos.services.reasoning.call_llm_json", AsyncMock(return_value=payload)
    )
    return await service._create_critique(
        SimpleNamespace(id=uuid4()), packet, {"stance": "neutral"}
    )


async def test_supported_core_explanation_survives_missing_optional_history(
    monkeypatch,
):
    plan, packet, payload = fixture()
    review = await reviewed(monkeypatch, packet, payload)
    candidate = {
        "stance": "neutral",
        "confidence_band": "medium",
        "thesis_summary": "Candidate",
        "reasoning": "Candidate analysis",
    }
    CorroborationService().apply_independent_review(candidate, review)
    AgentService._apply_independent_answerability_gate(
        candidate, review, question_research_plan=plan, packet_context=packet
    )
    answer = AgentService(None)._fast_path_orchestration(
        AgentTurnRequest(message=plan["original_question"]), candidate
    )
    assert "could not dispatch deliveries" in answer["assistant_message"]
    assert "https://network.example/notice" in answer["assistant_message"]
    assert "Still unresolved" not in answer["assistant_message"]
    assert not candidate["corroboration"]["can_promote"]
    assert candidate["answerability"]["status"] == "partial"


@pytest.mark.parametrize(
    "defect",
    [
        "wrong_source",
        "mixed_source_ids",
        "mixed_scope",
        "missing_index",
        "duplicate_index",
        "invalid_assertion",
        "no_falsifier",
    ],
)
async def test_unbound_or_ambiguous_coverage_cannot_turn_into_an_answer(
    monkeypatch, defect
):
    plan, packet, payload = fixture()
    row = payload["information_need_reviews"][0]
    if defect == "wrong_source":
        payload["material_assertions"][0]["supporting_evidence_ids"] = [str(uuid4())]
    elif defect == "mixed_source_ids":
        payload["material_assertions"][0]["supporting_evidence_ids"].append(
            str(uuid4())
        )
    elif defect == "mixed_scope":
        payload["material_assertions"][0]["scope_consistency"] = "mixed"
    elif defect == "missing_index":
        row["need_index"] = 9
    elif defect == "duplicate_index":
        payload["information_need_reviews"].append(deepcopy(row))
    elif defect == "invalid_assertion":
        row["assertion_indices"] = [0, 99]
    else:
        row.update(basis="conditional", assumption_index=0)
        payload["assumptions"] = [
            {
                "statement": "Demand may be delayed",
                "is_material": True,
                "evidence_ids": [],
                "falsifier": "",
            }
        ]
    review = await reviewed(monkeypatch, packet, payload)
    coverage = assess_answer_coverage(plan, review, packet)
    assert coverage["status"] == "inadequate"
    assert render_answer_coverage(coverage) is None
    candidate = {
        "stance": "bullish",
        "confidence_band": "high",
        "thesis_summary": "Unsupported result",
        "reasoning": "Unsupported result",
    }
    AgentService._apply_independent_answerability_gate(
        candidate, review, question_research_plan=plan, packet_context=packet
    )
    assert candidate["stance"] == "no_view"
    assert not candidate["corroboration"]["can_promote"]
    AgentService._apply_independent_answerability_gate(
        candidate, review, question_research_plan=plan, packet_context=packet
    )
    assert candidate["superseded_candidate"]["thesis_summary"] == "Unsupported result"


async def test_conditional_explanation_keeps_its_limit_and_falsifier(monkeypatch):
    plan, packet, payload = fixture()
    payload["information_need_reviews"][0].update(
        basis="conditional",
        assertion_indices=[],
        assumption_index=0,
        answer="If the network remains unavailable, dependent depots may accumulate a dispatch backlog.",
        limitation="This is a contingent mechanism, not a measured backlog or forecast.",
    )
    payload["assumptions"] = [
        {
            "statement": "Dependent depots lack a working alternative",
            "is_material": True,
            "evidence_ids": [],
            "falsifier": "Independent backup dispatch remains operational.",
        }
    ]
    review = await reviewed(monkeypatch, packet, payload)
    coverage = assess_answer_coverage(plan, review, packet)
    assert coverage["status"] == "partial"
    answer = render_answer_coverage(coverage)
    assert "Conditional interpretation" in answer
    assert "not a measured backlog or forecast" in answer
    assert "Independent backup dispatch" in answer


@pytest.mark.parametrize("url", ["https://[broken", "javascript:alert(1)"])
async def test_unusable_source_links_do_not_break_supported_answers(monkeypatch, url):
    plan, packet, payload = fixture()
    packet["direct_evidence"][0]["source"]["evidence_url"] = url
    review = await reviewed(monkeypatch, packet, payload)
    answer = render_answer_coverage(assess_answer_coverage(plan, review, packet))
    assert "could not dispatch deliveries" in answer
    assert url not in answer


def test_compaction_keeps_all_core_requirements_and_complete_question():
    needs = [
        f"Core need {index}: " + "Required qualification. " * 20 for index in range(8)
    ]
    question = (
        "Requested information. " * 30 + "Also preserve the final requested comparison."
    )
    plan = {
        "original_question": question,
        "information_needs": needs,
        "optional_information_needs": ["Optional context"],
    }
    compact = compact_packet_context({"research_plan": plan})["research_plan"]
    assert compact["original_question"] == question
    assert compact["information_needs"] == needs
    assert compact["optional_information_needs"] == ["Optional context"]


def test_historical_review_without_a_contract_does_not_invent_coverage():
    plan, packet, payload = fixture()
    del payload["information_need_reviews"]
    assert assess_answer_coverage(plan, payload, packet) is None


def test_answer_contract_uses_user_words_instead_of_planner_expansion():
    question = (
        "Explain why the pause matters and what its confirmed financial impact is."
    )
    requested = ["why the pause matters", "what its confirmed financial impact is"]
    assert (
        requested_answer_needs(question, {"requested_information": requested})
        == requested
    )
    assert requested_answer_needs(
        question,
        {"requested_information": ["a comprehensive history of network governance"]},
    ) == [question]
    assert requested_answer_needs(question) == [question]


@pytest.mark.parametrize("subject_type", ["portfolio", "entity"])
def test_no_account_context_keeps_subject_research_and_full_validation_packet(
    subject_type,
):
    packet = {
        "subject_type": subject_type,
        "research_plan": {"portfolio_context_role": "not_needed"},
        "portfolio_context": {
            "holdings": [
                {"ticker": "EXMPL", "private_ledger": "account detail " * 1000}
            ],
            "performance_attribution": {"private": "account performance " * 1000},
            "subject_fundamental_metrics": [
                {"metric": "Issuer operating margin", "source": "issuer.example"}
            ],
            "subject_market_setup_signals": [{"signal": "Issuer guidance"}],
            "quantity_measurements": [{"measurement_ref": "account-only"}],
        },
    }
    frozen = deepcopy(packet)
    prompt = compact_packet_context(packet)
    assert "holdings" not in prompt["portfolio_context"]
    assert "performance_attribution" not in prompt["portfolio_context"]
    assert prompt["quantity_measurements"] == []
    assert bool(prompt["portfolio_context"].get("subject_fundamental_metrics")) == (
        subject_type == "entity"
    )
    assert packet == frozen
    packet["research_plan"]["portfolio_context_role"] = "primary_evidence"
    assert (
        compact_packet_context(packet)["portfolio_context"]["holdings"]
        == packet["portfolio_context"]["holdings"]
    )


@pytest.mark.parametrize("wrong_value", [False, True])
async def test_measured_answer_uses_the_admitted_quantity_through_delivery(
    monkeypatch, wrong_value
):
    plan, packet, payload = fixture()
    measured = sample()
    packet["quantity_measurements"] = [measured]
    plan["information_needs"] = ["What is the measured holdings value?"]
    assertion = payload["material_assertions"][0]
    assertion.update(
        statement="The recorded holdings value is $10,000.",
        evidence_basis="portfolio_ledger",
        supporting_evidence_ids=[],
        quantity_bindings=[binding(measured)],
    )
    payload["information_need_reviews"][0].update(
        basis="measurement",
        answer=(
            "The recorded holdings value is $10 million."
            if wrong_value
            else "The recorded holdings value is $10,000."
        ),
    )
    review = await reviewed(monkeypatch, packet, payload)
    candidate = {"stance": "uncertain", "confidence_band": "low"}
    AgentService._apply_independent_answerability_gate(
        candidate, review, question_research_plan=plan, packet_context=packet
    )
    coverage = candidate["answerability"]["information_need_coverage"]
    if wrong_value:
        assert render_answer_coverage(coverage) is None
        assert candidate["answerability"]["status"] == "inadequate"
    else:
        rendered = AgentService(None)._fast_path_orchestration(
            AgentTurnRequest(message="What is the value?"), candidate
        )
        AgentService._audit_delivered_quantities(rendered, candidate)
        assert "Measured" in rendered["assistant_message"]
        assert "$10,000" in rendered["assistant_message"]
        assert candidate.get("quantity_grounding", {}).get("verified") is not False
