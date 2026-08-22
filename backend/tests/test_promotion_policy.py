from investos.core.policy import EVIDENCE_PROMOTION_POLICY, analyze_evidence_rigor


def test_five_rumors_do_not_equal_one_fact():
    # The core anti-hallucination invariant: a pile of weak evidence cannot
    # upgrade a conclusion no matter how many items there are.
    weak_stack = ["tier3_market_chatter"] * 5
    assert analyze_evidence_rigor(weak_stack) is False


def test_mixed_weak_and_medium_still_insufficient():
    assert (
        analyze_evidence_rigor(
            ["tier3_market_chatter", "tier2_verified_media", "tier2_verified_media"]
        )
        is False
    )


def test_single_tier1_filing_cannot_upgrade_an_investment_conclusion():
    assert (
        analyze_evidence_rigor(["tier1_direct_filing"], ["publisher:sec.gov"]) is False
    )


def test_tier1_plus_independent_confirmation_can_upgrade():
    assert (
        analyze_evidence_rigor(
            ["tier1_direct_filing", "tier2_verified_media"],
            ["publisher:sec.gov", "publisher:reuters.com"],
        )
        is True
    )


def test_same_publisher_does_not_count_twice():
    assert (
        analyze_evidence_rigor(
            ["tier1_direct_filing", "tier2_verified_media"],
            ["publisher:example.com", "publisher:example.com"],
        )
        is False
    )


def test_empty_stack_is_insufficient():
    assert analyze_evidence_rigor([]) is False


def test_policy_table_keeps_weak_tiers_non_promoting():
    # Guard against someone flipping a weak tier to self-promote.
    assert (
        EVIDENCE_PROMOTION_POLICY["tier3_market_chatter"][
            "can_upgrade_conclusion_alone"
        ]
        is False
    )
    assert (
        EVIDENCE_PROMOTION_POLICY["tier2_verified_media"][
            "can_upgrade_conclusion_alone"
        ]
        is False
    )
    assert (
        EVIDENCE_PROMOTION_POLICY["tier1_direct_filing"]["can_upgrade_conclusion_alone"]
        is False
    )
