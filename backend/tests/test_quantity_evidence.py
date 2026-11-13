from copy import deepcopy

import pytest

from investos.services.corroboration import CorroborationService
from investos.services.quantity_evidence import (
    audit_numeric_narrative,
    measurement,
    parse_quantity,
    validate_quantities,
)


def sample(value="10000", unit="USD", **kwargs):
    return measurement(
        value=value,
        unit=unit,
        basis="captured_close_holdings_value_excluding_cash",
        subject_ref="portfolio",
        snapshot_ref="synthetic-snapshot-a",
        as_of="2026-01-02T21:00:00Z",
        **kwargs,
    )


def binding(item, quote="$10,000", **changes):
    return {
        **{
            key: item[key]
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
        },
        "value": float(item["value"]) if item["value"] is not None else None,
        "quote": quote,
        "occurrence": 0,
        **changes,
    }


def test_valid_dimensioned_quantity_and_negative_and_zero_values():
    for value, quote in [("10000", "$10,000"), ("-500", "-$500"), ("0", "$0")]:
        item = sample(value)
        check = validate_quantities(
            {
                "statement": f"The recorded value is {quote}.",
                "quantity_bindings": [binding(item, quote)],
            },
            {"quantity_measurements": [item]},
        )
        assert check["verified"], check


@pytest.mark.parametrize(
    "changes,issue",
    [
        ({"value": 10000000}, "value_mismatch"),
        ({"unit": "KRW"}, "unit_mismatch"),
        ({"basis": "broker_total_equity"}, "basis_mismatch"),
        ({"subject_ref": "security:another-instrument"}, "subject_ref_mismatch"),
        ({"snapshot_ref": "older-snapshot"}, "snapshot_ref_mismatch"),
        ({"as_of": "2025-01-02T21:00:00Z"}, "as_of_mismatch"),
        ({"period_start": "2025-01-01"}, "period_start_mismatch"),
        ({"period_end": "2025-12-31"}, "period_end_mismatch"),
    ],
)
def test_value_and_each_measurement_dimension_are_checked(changes, issue):
    item = sample()
    check = validate_quantities(
        {
            "statement": "Recorded holdings value is $10,000.",
            "quantity_bindings": [binding(item, **changes)],
        },
        {"quantity_measurements": [item]},
    )
    assert not check["verified"] and issue in check["issues"]


def test_a_correct_numeric_field_cannot_launder_a_wrong_magnitude_in_the_quote():
    item = sample()
    check = validate_quantities(
        {
            "statement": "The account is worth $10 million.",
            "quantity_bindings": [binding(item, "$10 million")],
        },
        {"quantity_measurements": [item]},
    )
    assert not check["verified"] and "value_mismatch" in check["issues"]
    check = validate_quantities(
        {
            "statement": "The account is worth $10 million.",
            "quantity_bindings": [binding(item, "10", value=10)],
        },
        {"quantity_measurements": [item]},
    )
    assert not check["verified"]


@pytest.mark.parametrize(
    "status", ["missing", "partial", "book_changed", "partial_or_stale"]
)
def test_missing_and_incomplete_measurements_are_not_zero_or_verified(status):
    item = sample("0", status=status)
    check = validate_quantities(
        {"statement": "Value is $0.", "quantity_bindings": [binding(item, "$0")]},
        {"quantity_measurements": [item]},
    )
    assert not check["verified"]


def test_each_repeated_quantity_requires_a_binding_to_its_own_occurrence():
    item = sample()
    assertion = {
        "statement": "Holdings are $10,000 and another account is $10,000.",
        "quantity_bindings": [binding(item)],
    }
    assert not validate_quantities(assertion, {"quantity_measurements": [item]})[
        "verified"
    ]


def test_narrative_cannot_change_the_numbers_in_a_valid_assertion():
    item = sample()
    check = validate_quantities(
        {
            "statement": "Recorded value is $10,000.",
            "quantity_bindings": [binding(item)],
        },
        {"quantity_measurements": [item]},
    )
    assertions = [{"quantity_verification": check}]
    assert audit_numeric_narrative(["Recorded value is $10,000."], assertions)[
        "verified"
    ]
    assert not audit_numeric_narrative(
        ["The account is worth $10 million."], assertions
    )["verified"]


def test_context_exists_but_mismatched_assertion_never_promotes_even_with_forged_binding():
    item = sample()
    result = {
        "confidence_band": "high",
        "reasoning": "The account is worth $10 million.",
        "material_assertions": [
            {
                "statement": "The account is worth $10 million.",
                "scope_consistency": "matched",
                "evidence_basis": "portfolio_ledger",
                "supporting_context_paths": ["portfolio_context.total_market_value"],
                "quantity_bindings": [binding(item, "$10 million", value=10000000)],
            }
        ],
    }
    original = deepcopy(result)
    CorroborationService().assess_result(
        result,
        {
            "quantity_measurements": [item],
            "portfolio_context": {"total_market_value": 10000},
        },
    )
    assert not result["corroboration"]["can_promote"]
    assert result["confidence_band"] == "very_low"
    assert result["reasoning"] == original["reasoning"]


def test_parser_rejects_nonfinite_and_handles_decimal_scales_without_binary_rounding():
    assert parse_quantity("$1.25 million")[0] == 1250000
    assert parse_quantity("0.1%")[0].as_tuple().exponent == -1
    assert parse_quantity("$1e999999999")[0] is None


def test_wrong_weight_cannot_borrow_a_correct_amount_or_basis():
    item = sample("25", unit="percent")
    check = validate_quantities(
        {
            "statement": "The position is 60% of holdings.",
            "quantity_bindings": [binding(item, "60%", value=60)],
        },
        {"quantity_measurements": [item]},
    )
    assert not check["verified"]
    assert "value_mismatch" in check["issues"]


def test_issuer_figures_keep_the_source_evidence_path_without_an_account_binding():
    from tests.test_corroboration import _node, _packet, _result

    first = _node(publisher="issuer.example", content_hash="a", authority="primary")
    second = _node(publisher="wire.example", content_hash="b")
    text = "The issuer reported $10 million revenue and 25% growth."
    result = _result([first["id"], second["id"]])
    result.update(thesis_summary=text, reasoning=text)
    result["material_assertions"][0]["statement"] = text
    CorroborationService().assess_result(result, _packet(first, second))
    assert result["corroboration"]["can_promote"]
    assert result["quantity_grounding"]["verified"]


def test_rewriter_cannot_turn_a_verified_assertion_into_actionable_bad_math():
    from investos.services.agent import AgentService

    item = sample()
    result = {
        "material_assertions": [
            {
                "statement": "Holdings are $10,000.",
                "scope_consistency": "matched",
                "evidence_basis": "portfolio_ledger",
                "quantity_bindings": [binding(item)],
            }
        ]
    }
    CorroborationService().assess_result(result, {"quantity_measurements": [item]})
    assert result["corroboration"]["can_promote"]
    proposal = {
        "assistant_message": "The account is $10 million; adjust the paper position.",
        "decision": {"should_create": True},
        "shadow_experiment": {"should_create": True},
        "pruning": {"should_run": True},
        "memory_note": "Account is $10 million",
    }
    AgentService._audit_delivered_quantities(proposal, result)
    AgentService._enforce_action_evidence_boundary(proposal, result)
    assert not result["corroboration"]["can_promote"]
    assert not proposal["decision"]["should_create"]
    assert not proposal["shadow_experiment"]["should_create"]
    assert not proposal["pruning"]["should_run"]
    assert not proposal["memory_note"]
    assert result["blocked_action_proposal"]["memory_note"] == "Account is $10 million"
    assert (
        AgentService._watchers_allowed(
            {**result, "active_watchers": [{"threshold": 999}]}, auto_execute=True
        )
        == []
    )
