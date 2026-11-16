from uuid import uuid4

import pytest

from investos.services.corroboration import (
    CorroborationService,
    near_duplicate_distance,
    near_duplicate_signature,
    normalized_publisher_host,
    source_authority,
    source_lineage_key,
)


def _node(*, publisher: str, content_hash: str, authority: str = "secondary") -> dict:
    return {
        "id": str(uuid4()),
        "type": "fact",
        "tier": "hard_fact",
        "source": {
            "lineage_key": f"publisher:{publisher}",
            "content_hash": content_hash,
            "authority": authority,
        },
    }


def _node_with_signature(*, publisher: str, text: str) -> dict:
    node = _node(publisher=publisher, content_hash=str(uuid4()))
    signature, token_count = near_duplicate_signature(text)
    node["source"]["near_duplicate_signature"] = signature
    node["source"]["signature_token_count"] = token_count
    return node


def _result(support_ids: list[str], contradiction_ids: list[str] | None = None) -> dict:
    return {
        "stance": "bullish",
        "confidence_band": "very_high",
        "thesis_summary": "Demand is improving.",
        "material_assertions": [
            {
                "statement": "Demand is improving.",
                "subject_scope": "Example Company",
                "time_scope": "current quarter",
                "scope_consistency": "matched",
                "scope_notes": "Same issuer and reporting period.",
                "evidence_basis": "retrieved_source",
                "supporting_context_paths": [],
                "supporting_evidence_ids": support_ids,
                "contradicting_evidence_ids": contradiction_ids or [],
            }
        ],
        "assumptions": [],
    }


def _packet(*nodes: dict) -> dict:
    return {
        "direct_evidence": list(nodes),
        "connected_evidence": [],
        "historical_evidence": [],
        "contradiction_evidence": [],
    }


def test_independent_publishers_corroborate_a_material_assertion():
    first = _node(publisher="issuer.example", content_hash="a", authority="primary")
    second = _node(publisher="wire.example", content_hash="b")
    result = _result([first["id"], second["id"]])

    assessment = CorroborationService().assess_result(result, _packet(first, second))

    assert assessment["status"] == "corroborated"
    assert assessment["can_promote"] is True
    assert result["confidence_band"] == "very_high"


@pytest.mark.parametrize("late_defect", ["assertion", "assumption"])
def test_reasoning_preparation_cannot_discard_a_late_promotion_blocker(late_defect):
    from copy import deepcopy

    from investos.services.reasoning import ReasoningService

    first = _node(publisher="issuer.example", content_hash="a", authority="primary")
    second = _node(publisher="wire.example", content_hash="b")
    result = _result([first["id"], second["id"]])
    supported = result["material_assertions"][0]
    result["material_assertions"] = [deepcopy(supported) for _ in range(6)]
    if late_defect == "assertion":
        result["material_assertions"][-1].update(
            statement="An additional regulatory approval has occurred.",
            supporting_evidence_ids=[],
        )
    else:
        result["assumptions"] = [
            {
                "statement": "Background context",
                "is_material": False,
                "evidence_ids": [],
                "falsifier": "Context changes",
            }
            for _ in range(5)
        ] + [
            {
                "statement": "A necessary permit will be granted",
                "is_material": True,
                "evidence_ids": [],
                "falsifier": "Permit is denied",
            }
        ]
    prepared = ReasoningService(None)._prepare_reasoning_result(result)
    assessment = CorroborationService().assess_result(prepared, _packet(first, second))
    assert not assessment["can_promote"]
    assert len(prepared["material_assertions"]) == 6
    if late_defect == "assumption":
        assert len(prepared["assumptions"]) == 6


@pytest.mark.parametrize("path", ["normal", "thin", "reduced", "alternate"])
async def test_model_paths_retain_every_generated_verification_input(monkeypatch, path):
    from copy import deepcopy
    from unittest.mock import AsyncMock

    import fastjsonschema

    import investos.services.reasoning as module
    from investos.services.verification import VERIFICATION_SCHEMA

    result = _result([])
    arrays = (
        "supporting_evidence_ids",
        "contradicting_evidence_ids",
        "what_would_falsify",
        "what_would_strengthen",
        "active_contradictions",
        "material_assertions",
        "assumptions",
        "alternative_hypotheses",
        "active_watchers",
    )
    for field in arrays:
        result[field] = [f"{field}:{index}" for index in range(8)]
    result["material_assertions"] = [
        {
            **_result([])["material_assertions"][0],
            "statement": f"A source reports development {index}.",
            "quantity_bindings": [],
        }
        for index in range(8)
    ]
    result["assumptions"] = [
        {
            "statement": f"Necessary condition {index}",
            "is_material": True,
            "evidence_ids": [],
            "falsifier": "The condition is contradicted",
        }
        for index in range(8)
    ]
    result["alternative_hypotheses"] = [
        {
            "hypothesis": f"Competing explanation {index}",
            "supporting_evidence_ids": [],
            "disconfirming_evidence_ids": [],
            "decisive_test": "Obtain discriminating evidence",
        }
        for index in range(8)
    ]
    result["active_watchers"] = [
        {
            "ticker": "EXMPL",
            "condition_type": f"observable_{index}",
            "threshold": None,
            "objective": "Check a defined observable",
            "adjustment_plan": "Review the resulting evidence",
            "deadline_hours": 24,
        }
        for index in range(8)
    ]
    for schema in (
        module.REASONING_SCHEMA,
        module.CRITIQUE_SCHEMA,
        VERIFICATION_SCHEMA,
    ):
        for field in ("material_assertions", "assumptions", "alternative_hypotheses"):
            if field in schema["properties"]:
                fastjsonschema.compile(schema["properties"][field])(result[field])
    original = deepcopy(result)
    monkeypatch.setattr(module, "call_llm_json", AsyncMock(return_value=result))
    service = module.ReasoningService(None)
    monkeypatch.setattr(service, "_is_thin_packet", lambda context: False)
    monkeypatch.setattr(service, "_active_provider_unconfigured_label", lambda: None)
    monkeypatch.setattr(
        service, "_recovery_packet_context", lambda context: {"reduced": True}
    )
    packet = {"query_text": "Synthetic multi-part question"}
    if path == "normal":
        output = await service._reason_with_llm(packet, system_prompt="Synthetic")
    elif path == "thin":
        output = await service._thin_packet_reasoning(packet)
    elif path == "reduced":
        output = await service._reduced_context_recovery(
            packet, system_prompt="Synthetic"
        )
    else:
        monkeypatch.setattr(
            module,
            "available_llm_json_recovery_providers",
            AsyncMock(return_value=["nvidia_nim"]),
        )
        output = await service._alternate_provider_recovery(
            packet, system_prompt="Synthetic"
        )
    assert output is not None
    for field in arrays:
        assert output[field] == original[field], (path, field)


def test_watch_proposals_are_retained_before_existing_action_admission():
    from investos.services.agent import AgentService
    from investos.services.reasoning import ReasoningService

    proposals = [{"condition_type": f"observable_{index}"} for index in range(4)]
    result = ReasoningService(None)._prepare_reasoning_result(
        {"active_watchers": proposals}
    )
    assert result["active_watchers"] == proposals
    assert AgentService._watchers_allowed(result, auto_execute=False) == []
    assert AgentService._watchers_allowed(result, auto_execute=True) == proposals[:2]
    assert result["active_watchers"] == proposals


def test_same_publisher_does_not_become_two_sources():
    first = _node(publisher="wire.example", content_hash="a")
    second = _node(publisher="wire.example", content_hash="b")
    result = _result([first["id"], second["id"]])

    assessment = CorroborationService().assess_result(result, _packet(first, second))

    assert assessment["status"] == "single_source"
    assert assessment["can_promote"] is False
    assert result["confidence_band"] == "low"


def test_syndicated_exact_copy_does_not_count_as_independent_confirmation():
    first = _node(publisher="wire.example", content_hash="same")
    second = _node(publisher="republisher.example", content_hash="same")
    result = _result([first["id"], second["id"]])

    assessment = CorroborationService().assess_result(result, _packet(first, second))

    assert assessment["independent_supporting_source_count"] == 1
    assert assessment["duplicate_copy_count"] == 1
    assert assessment["can_promote"] is False


def test_lightly_edited_syndicated_copy_does_not_count_as_independent():
    base = " ".join(
        f"memory demand pricing supply margin cycle evidence token {index}"
        for index in range(30)
    )
    edited = base.replace("token 12", "updated 12")
    first = _node_with_signature(publisher="wire.example", text=base)
    second = _node_with_signature(publisher="republisher.example", text=edited)
    result = _result([first["id"], second["id"]])

    assessment = CorroborationService().assess_result(result, _packet(first, second))

    assert (
        near_duplicate_distance(
            first["source"]["near_duplicate_signature"],
            second["source"]["near_duplicate_signature"],
        )
        <= 3
    )
    assert assessment["independent_supporting_source_count"] == 1
    assert assessment["duplicate_copy_count"] == 1
    assert assessment["can_promote"] is False


def test_short_similar_notes_are_not_near_duplicate_evidence():
    signature, token_count = near_duplicate_signature("same short note")

    assert signature is None
    assert token_count == 3


def test_malformed_signature_and_token_metadata_fail_closed_without_crashing():
    first = _node(publisher="issuer.example", content_hash="a")
    second = _node(publisher="wire.example", content_hash="b")
    first["source"].update(
        near_duplicate_signature="not-a-signature",
        signature_token_count="unknown",
    )
    second["source"].update(
        near_duplicate_signature="f" * 128,
        signature_token_count=-5,
    )
    result = _result([first["id"], second["id"]])

    assessment = CorroborationService().assess_result(result, _packet(first, second))

    assert near_duplicate_distance("not-a-signature", "f" * 128) is None
    assert assessment["independent_supporting_source_count"] == 2


def test_material_contradiction_blocks_promotion():
    first = _node(publisher="issuer.example", content_hash="a")
    second = _node(publisher="wire.example", content_hash="b")
    contradiction = _node(publisher="regulator.example", content_hash="c")
    result = _result(
        [first["id"], second["id"]],
        [contradiction["id"]],
    )

    assessment = CorroborationService().assess_result(
        result, _packet(first, second, contradiction)
    )

    assert assessment["status"] == "disputed"
    assert assessment["can_promote"] is False
    assert result["confidence_band"] == "low"


def test_security_or_period_scope_mismatch_blocks_promotion():
    first = _node(publisher="issuer.example", content_hash="a")
    second = _node(publisher="wire.example", content_hash="b")
    result = _result([first["id"], second["id"]])
    result["material_assertions"][0]["scope_consistency"] = "mixed"
    result["material_assertions"][0]["scope_notes"] = "Different securities or periods."

    assessment = CorroborationService().assess_result(result, _packet(first, second))

    assert assessment["assertions"][0]["status"] == "scope_mismatch"
    assert assessment["can_promote"] is False


def test_unresolved_material_assumption_blocks_an_otherwise_supported_view():
    first = _node(publisher="issuer.example", content_hash="a")
    second = _node(publisher="wire.example", content_hash="b")
    result = _result([first["id"], second["id"]])
    result["assumptions"] = [
        {
            "statement": "The price move was caused by the reported event.",
            "is_material": True,
            "evidence_ids": [],
            "falsifier": "Factor attribution explains the move instead.",
        }
    ]

    assessment = CorroborationService().assess_result(result, _packet(first, second))

    assert assessment["unresolved_material_assumption_count"] == 1
    assert assessment["can_promote"] is False
    assert result["confidence_band"] == "very_low"


def test_blind_analyst_disagreement_prevents_promotion():
    first = _node(publisher="issuer.example", content_hash="a")
    second = _node(publisher="wire.example", content_hash="b")
    result = _result([first["id"], second["id"]])
    service = CorroborationService()
    service.assess_result(result, _packet(first, second))

    review = {"candidate_stance": "bearish", "confidence_band": "medium"}
    service.apply_independent_review(result, review)

    assert review["stance_disagrees"] is True
    assert result["corroboration"]["status"] == "analyst_disagreement"
    assert result["corroboration"]["can_promote"] is False
    assert result["confidence_band"] == "low"


def test_portfolio_ledger_assertion_uses_structured_context_not_publication():
    from investos.services.quantity_evidence import measurement

    measured = measurement(
        value=24,
        unit="percent",
        basis="position_mark_divided_by_holdings_mark",
        subject_ref="security:synthetic",
        snapshot_ref="synthetic-snapshot",
        as_of="2026-01-02T21:00:00Z",
    )
    bound = {
        key: measured[key]
        for key in (
            "measurement_ref",
            "value",
            "unit",
            "basis",
            "subject_ref",
            "snapshot_ref",
            "as_of",
            "period_start",
            "period_end",
        )
    }
    bound.update(quote="24%", value=24, occurrence=0)
    result = _result([])
    result["material_assertions"][0].update(
        statement="The position is 24% of the account.",
        evidence_basis="portfolio_ledger",
        supporting_context_paths=["portfolio_context.top_holdings.0.weight_pct"],
        quantity_bindings=[bound],
    )
    packet = {
        **_packet(),
        "portfolio_context": {"top_holdings": [{"ticker": "MEMA", "weight_pct": 24}]},
        "quantity_measurements": [measured],
    }

    assessment = CorroborationService().assess_result(result, packet)

    assertion = assessment["assertions"][0]
    assert assertion["status"] == "account_evidence"
    assert assertion["valid_supporting_context_paths"] == [
        "portfolio_context.top_holdings.0.weight_pct"
    ]
    assert assessment["can_promote"] is True
    assert assessment["independent_supporting_source_count"] == 0


def test_existing_context_paths_do_not_verify_invented_account_values():
    result = _result([])
    result["material_assertions"][0].update(
        statement="The account is worth $10 million and the holding is 60%.",
        evidence_basis="portfolio_ledger",
        supporting_context_paths=[
            "portfolio_context.total_market_value",
            "portfolio_context.top_holdings.0.weight_pct",
        ],
    )
    packet = {
        **_packet(),
        "portfolio_context": {
            "total_market_value": 10000,
            "top_holdings": [
                {"ticker": "EXMPL", "market_value": 2500, "weight_pct": 25}
            ],
        },
    }
    assessment = CorroborationService().assess_result(result, packet)
    assert assessment["can_promote"] is False
    assert assessment["assertions"][0]["status"] != "account_evidence"


def test_structured_context_assertion_fails_closed_for_missing_path():
    result = _result([])
    result["material_assertions"][0].update(
        evidence_basis="market_data_calculation",
        supporting_context_paths=[
            "portfolio_context.performance_attribution.total_return"
        ],
    )

    assessment = CorroborationService().assess_result(result, _packet())

    assert assessment["assertions"][0]["status"] == "unsupported"
    assert assessment["can_promote"] is False


def test_inadequate_question_scope_overrides_stance_agreement():
    first = _node(publisher="issuer.example", content_hash="a")
    second = _node(publisher="wire.example", content_hash="b")
    result = _result([first["id"], second["id"]])
    service = CorroborationService()
    service.assess_result(result, _packet(first, second))

    review = {
        "candidate_stance": "bullish",
        "confidence_band": "medium",
        "question_answerability": "inadequate",
        "answerability_reason": "The evidence covers a held company, not the requested opportunity universe.",
    }
    service.apply_independent_review(result, review)

    assert result["corroboration"]["status"] == "question_scope_inadequate"
    assert result["corroboration"]["can_promote"] is False
    assert result["confidence_band"] == "very_low"


def test_missing_source_provenance_never_counts_as_corroboration():
    node = {"id": str(uuid4()), "type": "event", "source": None}
    result = _result([node["id"]])

    assessment = CorroborationService().assess_result(result, _packet(node))

    assert assessment["status"] == "insufficient_support"
    assert assessment["can_promote"] is False


def test_source_identity_helpers_are_conservative_and_explicit():
    assert normalized_publisher_host("https://www.example.com/a") == "example.com"
    assert source_authority("news", {}) == "secondary"
    assert source_authority("news", {"is_primary_source": True}) == "primary"
    assert (
        source_lineage_key(
            source_id=uuid4(),
            source_url="https://copy.example/a",
            evidence_url=None,
            metadata={"canonical_source_id": "ORIGINAL"},
        )
        == "lineage:original"
    )
