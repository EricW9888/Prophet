# Core system policies that dictate rigor and discipline.

EVIDENCE_PROMOTION_POLICY = {
    "tier3_market_chatter": {
        "can_upgrade_conclusion_alone": False,
        "requires_verification": True,
        "max_confidence_yield": "low",
    },
    "tier2_verified_media": {
        "can_upgrade_conclusion_alone": False,
        "requires_verification": False,
        "max_confidence_yield": "medium",
    },
    "tier1_direct_filing": {
        "can_upgrade_conclusion_alone": False,
        "requires_verification": False,
        "max_confidence_yield": "medium",
    },
}

RETRIEVAL_BUDGET = {
    "exploration": {
        "max_depth": 3,
        "max_tokens": 10000,
        "allowed_layers": ["L1_context", "L3_facts", "L4_baseline"],
    },
    "answer": {
        "max_depth": 5,
        "max_tokens": 30000,
        "allowed_layers": [
            "L1_context",
            "L2_gaps",
            "L3_facts",
            "L4_baseline",
            "L5_conflicting",
        ],
    },
    "verification": {
        "max_depth": 10,
        "max_tokens": 100000,
        "allowed_layers": [
            "L1_context",
            "L2_gaps",
            "L3_facts",
            "L4_baseline",
            "L5_conflicting",
            "L6_historical",
            "L7_thematic",
            "L8_research",
        ],
    },
}


def analyze_evidence_rigor(
    evidence_tiers: list[str],
    independent_source_keys: list[str] | None = None,
) -> bool:
    """Return whether a stack can upgrade a conclusion, not merely store a fact."""
    if not independent_source_keys or len(independent_source_keys) != len(
        evidence_tiers
    ):
        return False
    independent = {
        key.strip().casefold()
        for key in independent_source_keys
        if isinstance(key, str) and key.strip()
    }
    return len(independent) >= 2 and any(
        tier == "tier1_direct_filing" for tier in evidence_tiers
    )
