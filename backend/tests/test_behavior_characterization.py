"""Synthetic characterization tests for high-risk user-visible behavior."""

from types import SimpleNamespace
from uuid import UUID

import pytest

from investos.core.prompting import compact_packet_context
from investos.services.agent import AgentService
from investos.services.dashboard import DashboardService
from investos.services.mailbox import GmailMailboxService
from investos.services.portfolio import PortfolioService
from investos.services.portfolio_lookahead import PortfolioLookaheadService
from investos.services.portfolio_peers import PortfolioPeerContextService
from investos.services.reasoning import ReasoningService
from investos.services.research import ResearchService
from investos.services.source import SourceService
from investos.services.source_learning import SourceLearningService


def _assert_contains(text: str, *needles: str) -> None:
    for needle in needles:
        assert needle in text


def _assert_not_contains(text: str, *needles: str) -> None:
    for needle in needles:
        assert needle not in text


def _lookahead_answer(portfolio_lookahead: dict) -> str:
    service = AgentService.__new__(AgentService)
    result = service._deterministic_operating_answer(
        {
            "query_type": "portfolio_lookahead",
            "portfolio_lookahead": portfolio_lookahead,
        }
    )
    assert result is not None
    return str(result.get("assistant_message") or "")


def test_broker_confirmation_parses_as_transaction() -> None:
    service = GmailMailboxService.__new__(GmailMailboxService)

    parsed = service._parse_robinhood_deterministic(
        "Your order to buy 4 shares of EXMPL executed at an average price of $25.50.",
        subject="Your order has been executed",
    )

    assert parsed is not None
    assert parsed["document_type"] == "order_confirmation"
    assert parsed["action"] == "buy"
    assert parsed["ticker"] == "EXMPL"
    assert parsed["quantity"] == 4.0
    assert parsed["price"] == 25.5


@pytest.mark.parametrize(
    ("message", "expected_route"),
    [
        ("hi", "conversation"),
        ("did it catch my latest transactions?", "operate"),
        ("how does MEMA look in the long run? can it hit 20% CAGR?", "analyze"),
    ],
    ids=("smalltalk", "transaction-status", "investment-analysis"),
)
def test_fallback_turn_intent_routes_user_requests(
    message: str, expected_route: str
) -> None:
    service = AgentService.__new__(AgentService)

    result = service._fallback_turn_intent(message)

    assert result["route"] == expected_route


def test_transaction_source_summary_keeps_email_receipt() -> None:
    transaction = SimpleNamespace(
        notes="Deterministic parse: BUY 2 EXMPL @ $40.00",
        provenance_json={
            "source": "gmail",
            "source_type": "email_order_confirmation",
            "source_label": "Broker confirmation email",
            "raw_evidence_id": "55555555-5555-5555-5555-555555555555",
            "confidence": 1.0,
        },
    )

    result = DashboardService._transaction_source_summary(transaction)

    assert result["source_type"] == "email_order_confirmation"
    assert result["source_label"] == "Broker confirmation email"
    assert str(result["source_evidence_id"]) == ("55555555-5555-5555-5555-555555555555")
    assert result["source_confidence"] == 1.0


def test_corrected_or_canceled_transactions_do_not_replay() -> None:
    observed = {
        status: PortfolioService.transaction_is_active(SimpleNamespace(status=status))
        for status in ("settled", "corrected", "canceled")
    }

    assert observed == {"settled": True, "corrected": False, "canceled": False}


def test_provider_failure_fallback_is_honest_and_not_canned() -> None:
    packet = {
        "query_text": "how does MEMA look in the long run? can it hit 20% CAGR?",
        "subject_name": "MEMA - Memory Alpha Corp.",
        "direct_evidence": [],
        "connected_evidence": [],
        "historical_evidence": [],
        "contradiction_evidence": [],
        "gap_flags": ["no_coverage_map"],
        "portfolio_context": {"top_holdings": [{"ticker": "MEMA"}, {"ticker": "MEMB"}]},
    }

    result = ReasoningService(None)._timeout_fallback_reasoning(packet)
    reasoning = str(result.get("reasoning") or "")

    _assert_contains(
        reasoning,
        "Current read",
        "Evidence state",
        "MEMA, MEMB",
        "Weak points in this read",
        "Best next check",
    )
    _assert_not_contains(
        reasoning,
        "could not complete the live structured analyst pass",
        "Local packet",
        "Provider analysis did not return",
        "Ask again in a moment",
        "starting valuation",
        "normalized cycle earnings",
        "dot-com analogies",
    )


def test_loss_fallback_uses_measured_attribution_without_inventing_a_cause() -> None:
    packet = {
        "query_text": "why have i been losing money lately?",
        "subject_name": "Portfolio",
        "direct_evidence": [],
        "connected_evidence": [],
        "historical_evidence": [],
        "contradiction_evidence": [],
        "portfolio_context": {
            "top_holdings": [{"ticker": "MEMA"}, {"ticker": "MEMB"}],
            "performance_attribution": {
                "period_start": "2026-06-23T00:00:00Z",
                "as_of": "2026-07-14T00:00:00Z",
                "gain": -500.0,
                "return_pct": -5.0,
                "benchmark_ticker": "SPY",
                "benchmark_return_pct": 1.5,
                "active_return_pct": -6.5,
                "coverage_pct": 100,
                "items": [
                    {"ticker": "MEML", "gain": -180.0},
                    {"ticker": "MEMA", "gain": -120.0},
                ],
            },
        },
    }

    result = ReasoningService(None)._timeout_fallback_reasoning(packet)
    reasoning = str(result.get("reasoning") or "")

    _assert_contains(
        reasoning,
        "lost $500.00",
        "MEML",
        "SPY returned +1.50%",
        "what moved the book, not why",
    )
    _assert_not_contains(
        reasoning,
        "Missing attribution inputs",
        "risk-off",
        "cyclical correction",
    )


def test_peer_context_links_synthetic_memory_companies() -> None:
    descriptors = [
        {
            "entity_id": "11111111-1111-1111-1111-111111111111",
            "ticker": "MEMA",
            "name": "Memory Alpha Corp.",
            "weight_pct": 22.0,
            "sector": "Technology",
            "industry": "Memory semiconductors",
            "name_terms": ["mema", "memory alpha"],
            "knowledge_terms": [
                "memory alpha",
                "nand",
                "flash",
                "memory",
                "ssd",
                "hbm",
                "ai-storage",
            ],
        },
        {
            "entity_id": "22222222-2222-2222-2222-222222222222",
            "ticker": "MEMB",
            "name": "Memory Beta Inc.",
            "weight_pct": 14.0,
            "sector": "Technology",
            "industry": "Memory semiconductors",
            "name_terms": ["memb", "memory beta"],
            "knowledge_terms": [
                "memory beta",
                "dram",
                "nand",
                "memory",
                "hbm",
                "ai",
                "pricing",
            ],
        },
        {
            "entity_id": "33333333-3333-3333-3333-333333333333",
            "ticker": "AUTX",
            "name": "Auto Example Co.",
            "weight_pct": 12.0,
            "sector": "Consumer Discretionary",
            "industry": "Automobiles",
            "name_terms": ["autx", "auto example"],
            "knowledge_terms": ["robotaxi", "fsd", "automotive", "ev"],
        },
    ]

    exposures = PortfolioPeerContextService.score_descriptors(descriptors)
    match = next(
        exposure
        for exposure in exposures
        if exposure.get("source_ticker") == "MEMA"
        and exposure.get("target_ticker") == "MEMB"
    )

    assert float(match["confidence"]) >= 0.45
    assert {"nand", "memory", "hbm"}.issubset(set(match["shared_terms"]))


def test_next_week_question_routes_to_portfolio_lookahead() -> None:
    assert PortfolioLookaheadService.looks_like_lookahead_request(
        "anything i should look forward/pay attention to the next week?"
    )


def test_lookahead_surfaces_dated_memory_earnings_and_mechanism() -> None:
    message = _lookahead_answer(
        {
            "as_of": "2026-06-22T19:00:00+00:00",
            "horizon_end": "2026-06-29T19:00:00+00:00",
            "attention_items": [
                {
                    "source": "active_watch",
                    "ticker": "MEMB",
                    "title": "Memory Beta earnings report",
                    "event_type": "earnings_release",
                    "due_at": "2026-06-24T20:00:00+00:00",
                    "countdown_seconds": 176400,
                    "portfolio_weight_pct": 18.0,
                    "why_it_matters": (
                        "MEMB is a large memory holding; earnings guidance updates "
                        "HBM/NAND pricing, which also affects MEMA memory exposure."
                    ),
                }
            ],
        }
    )

    _assert_contains(
        message,
        "MEMB",
        "Memory Beta earnings report",
        "2026-06-24",
        "HBM/NAND pricing",
        "MEMA memory exposure",
    )


def test_lookahead_exposes_expectation_delta_and_portfolio_readthrough() -> None:
    message = _lookahead_answer(
        {
            "as_of": "2026-06-22T19:00:00+00:00",
            "horizon_end": "2026-06-29T19:00:00+00:00",
            "attention_items": [
                {
                    "source": "active_watch",
                    "ticker": "INFR",
                    "title": "Infra Systems earnings report",
                    "event_type": "earnings_release",
                    "due_at": "2026-06-24T20:00:00+00:00",
                    "countdown_seconds": 176400,
                    "portfolio_weight_pct": 0,
                    "why_it_matters": (
                        "A large AI infrastructure earnings print can reset "
                        "expectations for semis, networking, and related portfolio "
                        "exposures."
                    ),
                    "investment_lens": {
                        "expectation_delta": (
                            "Compare the pre-event investor hurdle against reported "
                            "AI revenue and guidance."
                        ),
                        "market_reaction": (
                            "Check price reaction and analyst estimate revisions."
                        ),
                        "portfolio_transmission": (
                            "Map the result through AI infrastructure peers and any "
                            "held semiconductor exposure."
                        ),
                        "best_next_check": (
                            "Use the official release/transcript and market reaction "
                            "before changing watch priority."
                        ),
                    },
                }
            ],
        }
    )

    _assert_contains(
        message,
        "INFR",
        "Infra Systems earnings report",
        "Investment read",
        "pre-event investor hurdle",
        "price reaction",
        "AI infrastructure peers",
        "Best next check",
    )


def test_lookahead_surfaces_undated_earnings_watch_and_date_gap() -> None:
    message = _lookahead_answer(
        {
            "as_of": "2026-06-22T19:00:00+00:00",
            "horizon_end": "2026-06-29T19:00:00+00:00",
            "attention_items": [
                {
                    "source": "active_watch_date_missing",
                    "ticker": "MEMB",
                    "title": "HBM demand confirmation and capex execution",
                    "event_type": "earnings_release",
                    "due_at": None,
                    "countdown_seconds": None,
                    "portfolio_weight_pct": 18.0,
                    "why_it_matters": (
                        "MEMB is 18% of the synthetic portfolio; this earnings "
                        "release watch is important, but it has no stored event date, "
                        "so the calendar needs to be resolved before next-week risk "
                        "can be trusted. If it fires: Weak guidance would pressure "
                        "MEMB and correlated MEMA memory exposure."
                    ),
                }
            ],
        }
    )

    _assert_contains(
        message,
        "MEMB",
        "HBM demand confirmation",
        "no stored event date",
        "MEMA memory exposure",
    )


def test_source_feedback_becomes_a_retrieval_lesson() -> None:
    evidence = SimpleNamespace(
        id=UUID("44444444-4444-4444-4444-444444444444"),
        title="HBM demand revision",
        url="https://example.com/hbm",
    )
    source = SimpleNamespace(
        name="Primary NAND tracker",
        source_type="analyst",
    )

    payload = SourceService._feedback_lesson_payload(
        evidence=evidence,
        source=source,
        feedback={
            "rating": "not_useful",
            "note": "Repeats old memory-price chatter without direct channel evidence.",
            "context": "source_workspace",
            "flagged_at": "2026-06-20T12:00:00+00:00",
        },
    )

    assert payload["title"] == "Source feedback: Primary NAND tracker was not useful"
    assert payload["applicable_regimes"] == [
        "source_feedback",
        "not_useful",
        "analyst",
    ]
    _assert_contains(
        str(payload["summary"]),
        "source=Primary NAND tracker",
        "rating=not useful",
        "Down-rank similar evidence",
        "Repeats old memory-price chatter",
    )


def test_source_feedback_is_visible_to_reasoning() -> None:
    packet = compact_packet_context(
        {
            "query_text": "does this source help the thesis?",
            "subject_name": "MEMA - Memory Alpha Corp.",
            "direct_evidence": [],
            "connected_evidence": [],
            "historical_evidence": [],
            "contradiction_evidence": [],
            "portfolio_context": {
                "source_feedback": {
                    "counts": {"useful": 1, "not_useful": 2},
                    "recent": [
                        {
                            "rating": "not_useful",
                            "source_name": "Generic Market Blog",
                            "source_type": "web",
                            "title": "Memory prices again",
                            "note": "No direct NAND/HBM mechanism.",
                            "context": "source_workspace",
                            "flagged_at": "2026-06-20T12:00:00+00:00",
                        }
                    ],
                }
            },
        }
    )

    result = ReasoningService(None)._fallback_reasoning(packet)
    influence = result["source_feedback_influence"]

    assert influence["counts"] == {"useful": 1, "not_useful": 2}
    _assert_contains(
        str(influence["summary"]),
        "Source feedback available to this turn",
        "Generic Market Blog",
        "No direct NAND/HBM mechanism",
    )


def test_source_feedback_adjusts_measured_source_quality() -> None:
    result = SourceLearningService._apply_feedback_adjustment(
        {
            "source_type": "web_research",
            "specialization_domains": [],
            "known_weaknesses": [],
            "factual_reliability": "medium",
            "calibration": "calibrated",
            "correction_quality": "slow_corrects",
            "noise_ratio": "moderate",
            "trust_trajectory": "stable",
            "idea_generation_value": "medium",
            "timing_value": "medium",
            "portfolio_relevance_value": "medium",
            "specificity": "moderate",
            "originality": "occasional_original",
            "quality_score": 0.6,
            "originality_score": 0.5,
            "timing_usefulness": 0.5,
            "should_promote_to_trusted": True,
            "trust_reasoning": "Initial source evaluation.",
        },
        {"useful": 0, "not_useful": 3},
    )

    assert result["quality_score"] < 0.6
    assert result["should_promote_to_trusted"] is False
    assert result["noise_ratio"] == "noisy"
    assert result["trust_trajectory"] == "degrading"
    assert "0 useful, 3 not useful" in result["trust_reasoning"]


def test_recursive_research_title_collapses_to_real_subject() -> None:
    result = ResearchService._clean_research_title(
        title="Auto research: Research on Research on Unclassified Research",
        query="Can MEMA hit 20% CAGR over the long run?",
        metadata_json={"subject_name": "MEMA - Memory Alpha Corp."},
    )

    assert result == (
        "Research on MEMA - Memory Alpha Corp.: "
        "Can MEMA hit 20% CAGR over the long run?"
    )


def test_knowledge_status_reports_counts_evidence_and_search_time() -> None:
    service = AgentService.__new__(AgentService)
    result = service._deterministic_operating_answer(
        {
            "query_type": "knowledge_status",
            "knowledge_status": {
                "subject_name": "MEMA - Memory Alpha Corp.",
                "query_terms": ["mema", "bandwidth flash"],
                "direct_active_count": 2,
                "direct_deprecated_count": 1,
                "direct_term_matches": [
                    {
                        "type": "fact",
                        "text": (
                            "MEMA has no direct bandwidth-flash node yet; bandwidth "
                            "demand is a research gap."
                        ),
                    }
                ],
                "matching_active_nodes": [],
                "matching_deprecated_nodes": [],
                "searched_at": "2026-06-20T12:00:00+00:00",
            },
        }
    )

    assert result is not None
    message = str(result.get("assistant_message") or "")
    _assert_contains(
        message,
        "I checked Knowledge for MEMA",
        "2 active directly linked knowledge nodes",
        "bandwidth demand is a research gap",
        "2026-06-20",
    )
