from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from investos.services.market_setup import MarketSetupSignalService
from investos.services.pattern_discovery import (
    PATTERN_DISCOVERY_SCHEMA,
    PatternDiscoveryService,
)


def _result(*, evidence_refs=None, affected_tickers=None, confidence=0.88):
    return {
        "actionable": True,
        "reasoning_summary": "Two independent sources point to a shared mechanism.",
        "hypothesis": {
            "label": "Memory supply discipline may lead margin revisions",
            "pattern_type": "cross-company cycle transmission",
            "observation": "Memory suppliers are pairing tighter supply with stronger pricing commentary.",
            "proposed_mechanism": "Supply discipline lifts pricing before reported margins fully reflect it.",
            "affected_tickers": affected_tickers or ["MEMB", "MEMA"],
            "direction": "potentially supportive",
            "evidence_refs": evidence_refs or ["setup:one", "setup:two"],
            "falsifier": "Independent capacity data shows renewed oversupply while contract prices weaken.",
            "next_test": "Compare supplier capacity plans, contract pricing, and estimate revisions.",
            "why_now": "The holdings share a memory-cycle exposure before the next reported quarter.",
            "historical_episode_id": "episode-one",
            "confidence": confidence,
        },
    }


def _registry(*, same_lineage=False):
    return {
        "setup:one": {"lineage_key": "publisher:one.example"},
        "setup:two": {
            "lineage_key": (
                "publisher:one.example" if same_lineage else "publisher:two.example"
            )
        },
    }


def test_pattern_schema_keeps_pattern_type_open_ended():
    pattern_type = PATTERN_DISCOVERY_SCHEMA["properties"]["hypothesis"]["properties"][
        "pattern_type"
    ]

    assert pattern_type == {"type": "string"}


def test_pattern_validation_requires_independent_source_lineages():
    hypothesis, rejection = PatternDiscoveryService._validate_hypothesis(
        result=_result(),
        evidence_registry=_registry(same_lineage=True),
        tracked_tickers={"MEMB", "MEMA"},
        historical_episode_ids={"episode-one"},
        minimum_confidence=0.78,
        minimum_independent_sources=2,
    )

    assert hypothesis is None
    assert rejection == "pattern_lacks_independent_corroboration"


def test_pattern_validation_preserves_testable_portfolio_hypothesis():
    hypothesis, rejection = PatternDiscoveryService._validate_hypothesis(
        result=_result(affected_tickers=["memb", "MEMA", "OUTSIDE"]),
        evidence_registry=_registry(),
        tracked_tickers={"MEMB", "MEMA"},
        historical_episode_ids={"episode-one"},
        minimum_confidence=0.78,
        minimum_independent_sources=2,
    )

    assert rejection is None
    assert hypothesis is not None
    assert hypothesis["affected_tickers"] == ["MEMB", "MEMA"]
    assert hypothesis["lineage_keys"] == [
        "publisher:one.example",
        "publisher:two.example",
    ]
    assert hypothesis["historical_episode_id"] == "episode-one"
    assert "oversupply" in hypothesis["falsifier"]
    assert "estimate revisions" in hypothesis["next_test"]


def test_pattern_validation_rejects_unknown_evidence_and_low_confidence():
    unknown, unknown_reason = PatternDiscoveryService._validate_hypothesis(
        result=_result(evidence_refs=["setup:one", "setup:missing"]),
        evidence_registry=_registry(),
        tracked_tickers={"MEMB", "MEMA"},
        historical_episode_ids=set(),
        minimum_confidence=0.78,
        minimum_independent_sources=2,
    )
    weak, weak_reason = PatternDiscoveryService._validate_hypothesis(
        result=_result(confidence=0.4),
        evidence_registry=_registry(),
        tracked_tickers={"MEMB", "MEMA"},
        historical_episode_ids=set(),
        minimum_confidence=0.78,
        minimum_independent_sources=2,
    )

    assert unknown is None
    assert unknown_reason == "pattern_referenced_unknown_evidence"
    assert weak is None
    assert weak_reason == "pattern_confidence_below_threshold"


def test_pattern_fingerprint_is_stable_across_ticker_order():
    first, _ = PatternDiscoveryService._validate_hypothesis(
        result=_result(affected_tickers=["MEMB", "MEMA"]),
        evidence_registry=_registry(),
        tracked_tickers={"MEMB", "MEMA"},
        historical_episode_ids={"episode-one"},
        minimum_confidence=0.78,
        minimum_independent_sources=2,
    )
    second, _ = PatternDiscoveryService._validate_hypothesis(
        result=_result(affected_tickers=["MEMA", "MEMB"]),
        evidence_registry=_registry(),
        tracked_tickers={"MEMB", "MEMA"},
        historical_episode_ids={"episode-one"},
        minimum_confidence=0.78,
        minimum_independent_sources=2,
    )

    assert first is not None and second is not None
    assert PatternDiscoveryService._pattern_fingerprint(
        first
    ) == PatternDiscoveryService._pattern_fingerprint(second)


def test_pattern_dedupes_paraphrases_with_shared_exposure_and_evidence():
    incoming = {
        "label": "HBM capacity pressure supports memory pricing",
        "pattern_type": "structural supply constraint",
        "proposed_mechanism": "HBM wafer allocation constrains conventional DRAM and NAND supply.",
        "affected_tickers": ["MEMB", "MEMA", "SKHYV", "DRAM"],
        "evidence_refs": ["setup:1", "setup:2", "setup:3", "setup:4"],
        "pattern_fingerprint": "incoming",
    }
    existing = {
        "label": "Memory supply contraction transmits pricing power",
        "pattern_type": "supply substitution effect",
        "proposed_mechanism": "HBM consumes wafer starts and tightens conventional DRAM and NAND supply.",
        "affected_tickers": ["MEMB", "MEMA", "MEMX", "DRAM", "SKHYV"],
        "evidence_refs": ["setup:2", "setup:3", "setup:8", "setup:9"],
        "pattern_fingerprint": "existing",
    }

    assert PatternDiscoveryService._is_duplicate_candidate(incoming, existing) is True


def test_pattern_dedup_keeps_different_mechanisms_without_shared_evidence():
    incoming = {
        "label": "Pricing rises as memory capacity tightens",
        "pattern_type": "supply cycle",
        "proposed_mechanism": "Supplier capacity cuts reduce available NAND output.",
        "affected_tickers": ["MEMB", "MEMA"],
        "evidence_refs": ["setup:1", "setup:2"],
        "pattern_fingerprint": "incoming",
    }
    existing = {
        "label": "Customer concentration increases downside risk",
        "pattern_type": "demand concentration",
        "proposed_mechanism": "One hyperscaler delays orders after an inventory build.",
        "affected_tickers": ["MEMB", "MEMA"],
        "evidence_refs": ["setup:8", "setup:9"],
        "pattern_fingerprint": "existing",
    }

    assert PatternDiscoveryService._is_duplicate_candidate(incoming, existing) is False


@pytest.mark.asyncio
async def test_pattern_evidence_window_excludes_future_dated_signals():
    class EmptyResult:
        def scalars(self):
            return self

        def all(self):
            return []

    class RecordingSession:
        def __init__(self):
            self.statements = []

        async def execute(self, statement):
            self.statements.append(statement)
            return EmptyResult()

    session = RecordingSession()
    now = datetime(2026, 7, 17, 8, 0, tzinfo=UTC)

    await PatternDiscoveryService(session)._recent_evidence_registry(
        tracked_tickers={"MEMB", "MEMA"},
        now=now,
    )

    first_query = session.statements[0]
    compiled = first_query.compile()
    comparison_values = list(compiled.params.values())
    assert now in comparison_values
    assert any(
        value < now for value in comparison_values if isinstance(value, datetime)
    )


@pytest.mark.asyncio
async def test_market_setup_signal_can_join_a_larger_transaction():
    class RecordingSession:
        def __init__(self):
            self.commits = 0
            self.refreshes = 0

        def add(self, _value):
            return None

        async def flush(self):
            return None

        async def commit(self):
            self.commits += 1

        async def refresh(self, _value):
            self.refreshes += 1

    session = RecordingSession()
    service = MarketSetupSignalService(session)
    service._resolve_subject = AsyncMock(
        return_value={
            "subject_type": "portfolio",
            "subject_id": None,
            "entity_id": None,
            "security_id": None,
            "ticker": None,
        }
    )
    service._attach_graph_edges = AsyncMock(return_value=0)

    await service.create_signal(
        signal_name="Provisional cross-holding pattern",
        commit_transaction=False,
    )

    assert session.commits == 0
    assert session.refreshes == 0
    service._attach_graph_edges.assert_awaited_once()
