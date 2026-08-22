from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from investos.schemas.source import (
    MarketSetupOutcomeAssessmentResponse,
    MarketSetupSignalBackfillResponse,
    MarketSetupSignalCreate,
)
from investos.services.market_setup import MarketSetupSignalService
from investos.services.reasoning import ReasoningService


def test_market_setup_draft_preserves_open_ended_expectation_hurdle():
    evidence_id = uuid4()
    evidence = SimpleNamespace(
        id=evidence_id,
        title="Chip Systems earnings setup",
        event_time=datetime(2026, 6, 1, tzinfo=timezone.utc),
        public_time=datetime(2026, 6, 6, tzinfo=timezone.utc),
        created_at=datetime(2026, 6, 6, tzinfo=timezone.utc),
        metadata_json={
            "ticker": "chpx",
            "signal_name": "Chip Systems expected-overperformance hurdle",
            "signal_family": "expected_overperformance_hurdle",
            "setup_context": "Investors expected not just a beat, but a larger AI revenue acceleration.",
            "actual_context": "The company beat consensus, but the AI upside was below the whisper setup.",
            "price_reaction": "Shares fell after the print despite headline overperformance.",
            "investment_relevance": (
                "This matters because the market bar, not the absolute beat, controls the earnings reaction."
            ),
            "next_test": "Compare consensus, whisper, guidance, and post-print estimate revisions.",
            "confidence": "82%",
        },
    )
    source = SimpleNamespace(source_type="analyst")

    draft = MarketSetupSignalService.draft_from_evidence(evidence, source)

    assert draft is not None
    assert draft.ticker == "CHPX"
    assert draft.raw_evidence_id == evidence_id
    assert draft.signal_family == "expected_overperformance_hurdle"
    assert "larger AI revenue acceleration" in draft.setup_context
    assert "below the whisper setup" in draft.actual_context
    assert draft.confidence == 0.82


def test_market_setup_assessment_requires_due_or_observed_outcome():
    now = datetime(2026, 7, 13, 18, tzinfo=timezone.utc)
    base = {
        "actual_context": None,
        "price_reaction": None,
        "metadata_json": {},
    }
    due = SimpleNamespace(
        **base,
        event_time=now - timedelta(hours=8),
    )
    future = SimpleNamespace(
        **base,
        event_time=now + timedelta(hours=1),
    )
    observed = SimpleNamespace(
        **{**base, "actual_context": "Reported revenue missed the whisper hurdle."},
        event_time=None,
    )

    assert MarketSetupSignalService._assessment_due(due, now=now, grace_hours=6) is True
    assert (
        MarketSetupSignalService._assessment_due(future, now=now, grace_hours=6)
        is False
    )
    assert (
        MarketSetupSignalService._assessment_due(observed, now=now, grace_hours=6)
        is True
    )


def test_market_setup_assessment_filters_evidence_and_confidence():
    allowed_id = uuid4()
    rogue_id = uuid4()
    proposal = MarketSetupSignalService._sanitize_assessment_proposal(
        {
            "assessment": "validated",
            "confidence": 0.91,
            "rationale": "Later evidence directly confirmed the expected mechanism.",
            "limitations": "Magnitude remains approximate.",
            "assessment_evidence_ids": [str(allowed_id), str(rogue_id), "invalid"],
        },
        allowed_evidence_ids={allowed_id},
        min_confidence=0.8,
    )

    assert proposal["assessment"] == "validated"
    assert proposal["assessment_evidence"] == [allowed_id]
    assert proposal["should_apply"] is True

    low_confidence = MarketSetupSignalService._sanitize_assessment_proposal(
        {
            "assessment": "invalidated",
            "confidence": 0.4,
            "rationale": "The expected channel did not materialize.",
            "limitations": "",
            "assessment_evidence_ids": [str(allowed_id)],
        },
        allowed_evidence_ids={allowed_id},
        min_confidence=0.8,
    )
    assert low_confidence["should_apply"] is False


def test_market_setup_assessment_retry_gate_and_query_are_signal_specific():
    now = datetime(2026, 7, 13, 18, tzinfo=timezone.utc)
    signal = SimpleNamespace(
        ticker="CHPX",
        signal_name="Expected-overperformance hurdle",
        setup_context="Investors expected AI revenue to exceed the published consensus by a wide margin.",
        value_text=None,
        event_time=now - timedelta(days=2),
        public_time=None,
        as_of=None,
        created_at=now - timedelta(days=3),
        metadata_json={
            "outcome_assessment_attempt": {
                "next_retry_at": (now + timedelta(hours=2)).isoformat(),
            }
        },
    )

    assert MarketSetupSignalService._assessment_retry_ready(signal, now=now) is False
    signal.metadata_json["outcome_assessment_attempt"]["next_retry_at"] = (
        now - timedelta(minutes=1)
    ).isoformat()
    assert MarketSetupSignalService._assessment_retry_ready(signal, now=now) is True

    query = MarketSetupSignalService._outcome_followup_query(signal)
    assert "CHPX" in query
    assert "AI revenue" in query
    assert "mechanism, timing, magnitude" in query


@pytest.mark.asyncio
async def test_market_setup_assessment_attempt_persists_backoff_and_research_trace():
    class FakeSession:
        committed = False

        async def commit(self):
            self.committed = True

    class Signal:
        metadata_json = {
            "outcome_assessment_attempt": {"attempt_count": 2},
        }
        updated = False

        def mark_updated(self):
            self.updated = True

    session = FakeSession()
    signal = Signal()
    await MarketSetupSignalService(session)._record_assessment_attempt(
        signal=signal,
        result={
            "assessment": "indeterminate",
            "confidence": 0.35,
            "rationale": "No direct outcome evidence.",
            "limitations": "Only adjacent commentary was available.",
            "recommended_research_query": "Find the direct outcome.",
            "research_followup": {
                "started": True,
                "evidence_id": uuid4(),
            },
        },
        retry_hours=24,
    )

    attempt = signal.metadata_json["outcome_assessment_attempt"]
    assert attempt["attempt_count"] == 3
    assert datetime.fromisoformat(attempt["next_retry_at"]) > datetime.fromisoformat(
        attempt["attempted_at"]
    )
    assert isinstance(attempt["research_followup"]["evidence_id"], str)
    assert signal.updated is True
    assert session.committed is True


def test_market_setup_api_contract_keeps_currency_and_quality_gate_name():
    create = MarketSetupSignalCreate(
        signal_name="Debt refinancing hurdle", currency="USD"
    )
    response = MarketSetupSignalBackfillResponse.model_validate(
        {
            "dry_run": True,
            "scanned": 10,
            "candidates": 2,
            "created": 0,
            "skipped_existing": 1,
            "skipped_no_signal": 5,
            "skipped_quality_gate": 2,
            "skipped_unsafe_origin": 0,
            "examples": [],
        }
    )

    assert create.currency == "USD"
    assert response.skipped_quality_gate == 2

    assessment = MarketSetupOutcomeAssessmentResponse.model_validate(
        {
            "scanned": 50,
            "due": 5,
            "eligible": 12,
            "deferred": 8,
            "proposed": 5,
            "applied": 0,
            "research_attempted": 1,
            "research_started": 1,
            "results": [],
        }
    )
    assert assessment.deferred == 8
    assert assessment.research_started == 1


def test_market_setup_text_backfill_uses_dynamic_subject_catalog():
    entity_id = uuid4()
    security_id = uuid4()
    source_item_id = uuid4()
    evidence = SimpleNamespace(
        id=uuid4(),
        title="ZZZ earnings setup",
        source_item_type="web_research",
        event_time=datetime(2026, 7, 1, tzinfo=timezone.utc),
        public_time=datetime(2026, 7, 2, tzinfo=timezone.utc),
        created_at=datetime(2026, 7, 2, tzinfo=timezone.utc),
        eligible_action_time=datetime(2026, 7, 2, tzinfo=timezone.utc),
        metadata_json={},
    )
    source_item = SimpleNamespace(
        id=source_item_id,
        summary=(
            "$ZZZ beat consensus EPS, but investors had expected a larger AI revenue acceleration "
            "and shares fell after the report."
        ),
        extracted_text=None,
        processing_status="processed",
    )
    source = SimpleNamespace(
        id=uuid4(), name="Example Research", source_type="analyst", is_trusted=True
    )
    known_subjects = [
        {
            "entity_id": entity_id,
            "security_id": security_id,
            "ticker": "ZZZ",
            "name": "Zeta Example Corp",
            "aliases": ["Zeta"],
            "portfolio_relevant": True,
        }
    ]

    draft = MarketSetupSignalService.draft_from_evidence_or_text(
        evidence=evidence,
        source=source,
        source_item=source_item,
        known_subjects=known_subjects,
    )

    assert draft is not None
    assert draft.ticker == "ZZZ"
    assert draft.entity_id == entity_id
    assert draft.security_id == security_id
    assert draft.source_item_id == source_item_id
    assert draft.signal_family == "expectation_delta"
    assert "larger AI revenue acceleration" in draft.setup_context
    assert "market reaction depends on the gap" in draft.investment_relevance
    assert draft.metadata["backfill_extractor"] == "text_market_setup_seed"
    assert draft.metadata["matched_subject"]["ticker"] == "ZZZ"


def test_market_setup_text_backfill_keeps_fundamental_metric_context_open_ended():
    evidence = SimpleNamespace(
        id=uuid4(),
        title="ABC leverage and forward P/E setup",
        source_item_type="manual_note",
        event_time=None,
        public_time=datetime(2026, 7, 3, tzinfo=timezone.utc),
        created_at=datetime(2026, 7, 3, tzinfo=timezone.utc),
        metadata_json={},
    )
    source_item = SimpleNamespace(
        id=uuid4(),
        summary="Alpha Balance Corp trades at 22x forward P/E while debt and interest coverage are becoming central to the downside case.",
        extracted_text=None,
        processing_status="processed",
    )
    known_subjects = [
        {
            "entity_id": uuid4(),
            "security_id": uuid4(),
            "ticker": "ABC",
            "name": "Alpha Balance Corp",
            "aliases": [],
            "portfolio_relevant": False,
        }
    ]

    draft = MarketSetupSignalService.draft_from_evidence_or_text(
        evidence=evidence,
        source=SimpleNamespace(
            id=uuid4(), name="Manual", source_type="manual", is_trusted=False
        ),
        source_item=source_item,
        known_subjects=known_subjects,
    )

    assert draft is not None
    assert draft.ticker == "ABC"
    assert draft.signal_family == "fundamental_metric_setup"
    assert "22x forward P/E" in draft.setup_context
    assert "balance-sheet metrics" in draft.investment_relevance


def test_market_setup_text_backfill_avoids_ambiguous_symbol_false_positive():
    evidence = SimpleNamespace(
        id=uuid4(),
        title="Auto Dynamics app monetization setup",
        source_item_type="web_research",
        event_time=None,
        public_time=datetime(2026, 7, 3, tzinfo=timezone.utc),
        created_at=datetime(2026, 7, 3, tzinfo=timezone.utc),
        metadata_json={},
    )
    source_item = SimpleNamespace(
        id=uuid4(),
        summary="Portfolio setup: Auto Dynamics app revenue and cash flow expectations improved after the report.",
        extracted_text=None,
        processing_status="processed",
    )
    known_subjects = [
        {
            "entity_id": uuid4(),
            "security_id": uuid4(),
            "ticker": "APP",
            "name": "AppLovin",
            "aliases": [],
            "portfolio_relevant": True,
        },
        {
            "entity_id": uuid4(),
            "security_id": uuid4(),
            "ticker": "CASH",
            "name": "CASH",
            "aliases": [],
            "portfolio_relevant": True,
        },
    ]

    draft = MarketSetupSignalService.draft_from_evidence_or_text(
        evidence=evidence,
        source=SimpleNamespace(
            id=uuid4(), name="Web", source_type="web_research", is_trusted=False
        ),
        source_item=source_item,
        known_subjects=known_subjects,
    )

    assert draft is not None
    assert draft.ticker is None
    assert draft.subject_type == "portfolio"
    assert "matched_subject" not in draft.metadata


def test_market_setup_backfill_skips_background_chat_unless_explicit_signal():
    background_turn = SimpleNamespace(
        source_item_type="conversation_turn",
        metadata_json={"origin": "agent_reflection"},
    )
    explicit_signal = SimpleNamespace(
        id=uuid4(),
        title="Explicit setup",
        source_item_type="conversation_turn",
        event_time=None,
        public_time=None,
        created_at=datetime(2026, 7, 4, tzinfo=timezone.utc),
        metadata_json={
            "signal_name": "Explicit market setup",
            "setup_context": "User explicitly saved this as setup context.",
        },
    )

    assert (
        MarketSetupSignalService._should_skip_backfill(
            background_turn,
            include_conversation_turns=False,
        )
        is True
    )
    assert (
        MarketSetupSignalService._should_skip_backfill(
            explicit_signal,
            include_conversation_turns=False,
        )
        is False
    )


def test_market_setup_context_makes_packet_non_thin_and_fallback_concrete():
    service = ReasoningService(session=None)
    packet = {
        "query_text": "why did Chip Systems miss if it beat?",
        "subject_type": "entity",
        "subject_name": "Chip Systems",
        "direct_evidence": [],
        "connected_evidence": [],
        "historical_evidence": [],
        "contradiction_evidence": [],
        "lessons": [],
        "portfolio_context": {
            "subject_market_setup_signals": [
                {
                    "id": str(uuid4()),
                    "signal_name": "Chip Systems expected-overperformance hurdle",
                    "setup_context": "Investors expected a larger AI revenue acceleration.",
                    "actual_context": "The report beat consensus but missed the higher whisper bar.",
                    "investment_relevance": "The relevant mechanism is expectation delta, not the absolute beat.",
                }
            ]
        },
    }

    assert service._is_thin_packet(packet) is False
    fallback = service._fallback_market_setup_context(packet)

    assert fallback is not None
    assert "Chip Systems expected-overperformance hurdle" in fallback
    assert "expectation delta" in fallback
