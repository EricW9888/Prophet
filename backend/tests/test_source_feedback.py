from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from investos.schemas.source import (
    SourceClaimBatchAssessmentCreate,
    SourceClaimBatchAssessmentResponse,
    SourceCreate,
)
from investos.services.review import ReviewService
from investos.services.source import (
    NOTE_SOURCE_ITEM_TYPES,
    SOURCE_FEEDBACK_LESSON_TAG,
    SOURCE_FEEDBACK_LESSON_TYPE,
    SourceService,
)
from investos.services.source_claim_policy import source_claim_priority
from investos.services.source_learning import SourceLearningService


def test_source_feedback_lesson_payload_preserves_user_signal():
    evidence_id = uuid4()
    source = SimpleNamespace(name="Primary NAND tracker", source_type="analyst")
    evidence = SimpleNamespace(
        id=evidence_id,
        title="HBM demand revision",
        url="https://example.com/hbm",
    )
    payload = SourceService._feedback_lesson_payload(
        evidence=evidence,
        source=source,
        feedback={
            "rating": "not_useful",
            "note": "Repeats old memory-price chatter without direct channel evidence.",
            "context": "source_workspace",
            "flagged_at": "2026-06-17T12:00:00+00:00",
        },
    )

    assert payload["lesson_type"] == SOURCE_FEEDBACK_LESSON_TYPE
    assert payload["title"] == "Source feedback: Primary NAND tracker was not useful"
    assert f"{SOURCE_FEEDBACK_LESSON_TAG}={evidence_id}" in payload["summary"]
    assert "Repeats old memory-price chatter" in payload["summary"]
    assert "Down-rank similar evidence" in payload["summary"]
    assert payload["applicable_regimes"] == [
        "source_feedback",
        "not_useful",
        "analyst",
    ]


def test_feedback_summary_returns_linked_lesson_metadata():
    evidence = SimpleNamespace(
        id=uuid4(),
        title="Useful HBM primary data",
        url=None,
        source_item_type="research_note",
        created_at=datetime(2026, 6, 17, tzinfo=timezone.utc),
    )
    source = SimpleNamespace(
        id=uuid4(), name="Manual Research Inbox", source_type="manual"
    )
    lesson_id = uuid4()

    summary = SourceService._feedback_summary(
        evidence,
        source,
        {
            "rating": "useful",
            "note": "This had the missing direct mechanism.",
            "context": "source_workspace",
            "flagged_at": "2026-06-17T12:00:00+00:00",
            "lesson_id": str(lesson_id),
            "lesson_title": "Source feedback: Manual Research Inbox was useful",
        },
    )

    assert summary["lesson_id"] == str(lesson_id)
    assert (
        summary["lesson_title"] == "Source feedback: Manual Research Inbox was useful"
    )
    assert summary["flagged_at"] == datetime(2026, 6, 17, 12, tzinfo=timezone.utc)


def test_youtube_manual_transcript_keeps_specific_origin_label():
    source = SimpleNamespace(source_type="youtube")
    evidence = SimpleNamespace(
        metadata_json={
            "origin": "source_workspace",
            "media_source_type": "youtube",
            "video_url": "https://www.youtube.com/watch?v=abc123",
        },
        source_item_type="manual_transcript",
        url=None,
    )

    origin = SourceService._evidence_origin_summary(evidence, source)

    assert "manual_transcript" in NOTE_SOURCE_ITEM_TYPES
    assert "video_notes" in NOTE_SOURCE_ITEM_TYPES
    assert origin["origin_kind"] == "manual"
    assert origin["origin_label"] == "Manual YouTube transcript"
    assert origin["origin_detail"] == "https://www.youtube.com/watch?v=abc123"


def test_video_notes_keep_specific_origin_label():
    source = SimpleNamespace(source_type="manual")
    evidence = SimpleNamespace(
        metadata_json={
            "origin": "source_workspace",
            "media_source_type": "youtube",
        },
        source_item_type="video_notes",
        url="https://www.youtube.com/watch?v=def456",
    )

    origin = SourceService._evidence_origin_summary(evidence, source)

    assert origin["origin_kind"] == "manual"
    assert origin["origin_label"] == "YouTube video notes"
    assert origin["origin_detail"] == "https://www.youtube.com/watch?v=def456"


def test_local_audio_transcript_keeps_extraction_origin_label():
    source = SimpleNamespace(source_type="youtube")
    evidence = SimpleNamespace(
        metadata_json={
            "trigger": "manual_youtube_ingest",
            "ingest_mode": "local_audio_transcription",
            "video_id": "dQw4w9WgXcQ",
        },
        source_item_type="video_audio_transcript",
        url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    )

    origin = SourceService._evidence_origin_summary(evidence, source)

    assert origin["origin_kind"] == "manual"
    assert origin["origin_label"] == "Local YouTube audio transcript"
    assert origin["origin_detail"] == "dQw4w9WgXcQ"


@pytest.mark.asyncio
async def test_create_source_returns_hydrated_response_without_lazy_relationships():
    class FakeSession:
        def add(self, source):
            if source.id is None:
                source.id = uuid4()

        async def commit(self):
            pass

        async def refresh(self, _source):
            pass

    class Service(SourceService):
        async def _find_duplicate_source(self, **_kwargs):
            return None

        async def _source_response_by_id(self, source_id):
            return {
                "id": source_id,
                "name": "ExampleFinance",
                "source_type": "youtube",
                "url": "https://www.youtube.com/@ExampleFinance",
                "description": "Transcript source",
                "is_trusted": True,
                "origin": {
                    "origin_kind": "manual",
                    "origin_label": "YouTube transcript source",
                    "origin_detail": "Transcript based.",
                },
                "evidence_count": 0,
                "trust_profile": None,
                "value_profile": None,
                "quality_segments": [],
                "performance_history": [],
                "recent_items": [],
                "created_at": datetime(2026, 7, 6, tzinfo=timezone.utc),
                "updated_at": datetime(2026, 7, 6, tzinfo=timezone.utc),
            }

    result = await Service(FakeSession()).create_source(
        SourceCreate(
            name="ExampleFinance",
            source_type="youtube",
            url="https://www.youtube.com/@ExampleFinance",
            description="Transcript source",
            is_trusted=True,
        )
    )

    assert isinstance(result, dict)
    assert result["source_type"] == "youtube"
    assert result["quality_segments"] == []
    assert result["origin"]["origin_label"] == "YouTube transcript source"


@pytest.mark.asyncio
async def test_create_source_duplicate_can_adopt_clearer_manual_name():
    existing = SimpleNamespace(
        id=uuid4(),
        name="Example",
        source_type="youtube",
        url="https://www.youtube.com/@ExampleFinance",
        description=None,
        is_trusted=False,
    )

    class FakeSession:
        async def commit(self):
            pass

        async def refresh(self, _source):
            pass

    class Service(SourceService):
        async def _find_duplicate_source(self, **_kwargs):
            return existing

        async def _source_response_by_id(self, source_id):
            return {
                "id": source_id,
                "name": existing.name,
                "source_type": existing.source_type,
                "url": existing.url,
                "description": existing.description,
                "is_trusted": existing.is_trusted,
                "origin": {
                    "origin_kind": "manual",
                    "origin_label": "YouTube transcript source",
                    "origin_detail": "Transcript based.",
                },
                "evidence_count": 0,
                "trust_profile": None,
                "value_profile": None,
                "quality_segments": [],
                "performance_history": [],
                "recent_items": [],
                "created_at": datetime(2026, 7, 6, tzinfo=timezone.utc),
                "updated_at": datetime(2026, 7, 6, tzinfo=timezone.utc),
            }

    result = await Service(FakeSession()).create_source(
        SourceCreate(
            name="ExampleFinance",
            source_type="youtube",
            url="https://www.youtube.com/@ExampleFinance",
            description="Transcript source",
            is_trusted=True,
        )
    )

    assert result["name"] == "ExampleFinance"
    assert existing.description == "Transcript source"
    assert existing.is_trusted is True


def test_source_learning_adjusts_quality_from_user_feedback():
    evaluation = {
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
    }

    adjusted = SourceLearningService._apply_feedback_adjustment(
        evaluation,
        {"useful": 0, "not_useful": 3},
    )

    assert adjusted["quality_score"] < evaluation["quality_score"]
    assert adjusted["originality_score"] < evaluation["originality_score"]
    assert adjusted["timing_usefulness"] < evaluation["timing_usefulness"]
    assert adjusted["should_promote_to_trusted"] is False
    assert adjusted["noise_ratio"] == "noisy"
    assert adjusted["trust_trajectory"] == "degrading"
    assert "0 useful, 3 not useful" in adjusted["trust_reasoning"]


def test_source_performance_history_payload_scores_assessed_claims():
    claim_time = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assessment_time = datetime(2026, 6, 1, tzinfo=timezone.utc)
    records = [
        SimpleNamespace(
            assessment="correct",
            assessment_time=assessment_time,
            claim_time=claim_time,
            horizon_days=60,
            notes="Primary source call with original channel work.",
        ),
        SimpleNamespace(
            assessment="partially_correct",
            assessment_time=assessment_time,
            claim_time=claim_time,
            horizon_days=90,
            notes="Direction right, magnitude overstated.",
        ),
        SimpleNamespace(
            assessment="incorrect",
            assessment_time=assessment_time,
            claim_time=claim_time,
            horizon_days=30,
            notes="Missed the demand inflection.",
        ),
        SimpleNamespace(
            assessment="pending",
            assessment_time=None,
            claim_time=claim_time,
            horizon_days=None,
            notes=None,
        ),
    ]

    payload = SourceService._performance_history_payload(records)

    assert payload is not None
    assert payload["total_claims"] == 3
    assert payload["correct_claims"] == 1
    assert payload["incorrect_claims"] == 1
    assert payload["accuracy_rate"] == 0.5
    assert payload["originality_rate"] == 0.3333
    assert payload["timing_score"] == 0.5


def test_source_performance_history_labels_reliability_and_trajectory():
    assert SourceService._performance_reliability_label(0.86) == "very_high"
    assert SourceService._performance_reliability_label(0.62) == "medium"
    assert SourceService._performance_reliability_label(0.30) == "very_low"
    assert SourceService._performance_trajectory_label(0.75, 4) == "improving"
    assert SourceService._performance_trajectory_label(0.35, 4) == "degrading"
    assert SourceService._performance_trajectory_label(0.75, 2) == "stable"


def test_source_claim_review_due_time_uses_claim_horizon_policy():
    claim_time = datetime(2026, 1, 1, tzinfo=timezone.utc)
    record = SimpleNamespace(claim_time=claim_time, horizon_days=None)
    tactical_claim = SimpleNamespace(target_horizon="tactical", stale_after=None)
    strategic_claim = SimpleNamespace(target_horizon="strategic", stale_after=None)
    explicit_record = SimpleNamespace(claim_time=claim_time, horizon_days=5)
    stale_claim = SimpleNamespace(
        target_horizon="visionary",
        stale_after=datetime(2026, 2, 1, tzinfo=timezone.utc),
    )

    assert ReviewService._source_claim_due_at(record, tactical_claim) == datetime(
        2026, 1, 8, tzinfo=timezone.utc
    )
    assert ReviewService._source_claim_due_at(record, strategic_claim) == datetime(
        2026, 1, 31, tzinfo=timezone.utc
    )
    assert ReviewService._source_claim_due_at(explicit_record, stale_claim) == datetime(
        2026, 1, 6, tzinfo=timezone.utc
    )
    assert ReviewService._source_claim_due_at(record, stale_claim) == datetime(
        2026, 2, 1, tzinfo=timezone.utc
    )


def test_source_claim_review_copy_is_specific_and_actionable():
    service = ReviewService(session=None)
    item = SimpleNamespace(
        item_type="source_claim_record",
        trigger_reason="Pending source claim due for outcome assessment: HBM demand raises NAND mix.",
        created_at=datetime(2026, 2, 10, tzinfo=timezone.utc),
        coverage_weakness=0.0,
        contradiction_pressure=0.0,
        thesis_drift=0.0,
        catalyst_proximity=0.0,
    )
    ctx = {
        "label": "Memory Channel: HBM demand raises NAND mix",
        "source_name": "Memory Channel",
        "claim_statement": "HBM demand raises NAND mix and improves MEMB/MEMA pricing power.",
        "claim_time": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "claim_due_at": datetime(2026, 1, 31, tzinfo=timezone.utc),
        "claim_horizon": "strategic",
        "claim_ticker": "MEMB",
        "is_original_claim": True,
    }

    summary = service._why_now_summary(item, ctx)
    next_action = service._next_action(item, ctx)
    tags = service._signal_tags(item, ctx)

    assert "Memory Channel made the claim" in summary
    assert "HBM demand raises NAND mix" in summary
    assert "source reliability and trust trajectory" in summary
    assert "assess the claim as correct" in next_action
    assert tags == ["source outcome", "strategic", "MEMB", "original claim"]


def test_source_claim_auto_assessment_sanitizes_evidence_and_confidence():
    allowed_id = uuid4()
    rogue_id = uuid4()
    proposal = SourceService._sanitize_claim_assessment_proposal(
        {
            "assessment": "correct",
            "confidence": 0.92,
            "rationale": "Later evidence directly confirmed the claim.",
            "limitations": "Magnitude was approximate.",
            "assessment_evidence_ids": [str(allowed_id), str(rogue_id), "not-a-uuid"],
        },
        allowed_evidence_ids={allowed_id},
        min_confidence=0.8,
    )

    assert proposal["assessment"] == "correct"
    assert proposal["confidence"] == 0.92
    assert proposal["assessment_evidence"] == [allowed_id]
    assert proposal["should_apply"] is True

    low_confidence = SourceService._sanitize_claim_assessment_proposal(
        {
            "assessment": "incorrect",
            "confidence": 0.45,
            "rationale": "Contradicted.",
            "limitations": "",
            "assessment_evidence_ids": [str(allowed_id)],
        },
        allowed_evidence_ids={allowed_id},
        min_confidence=0.8,
    )
    assert low_confidence["should_apply"] is False

    unsupported = SourceService._sanitize_claim_assessment_proposal(
        {
            "assessment": "correct",
            "confidence": 0.95,
            "rationale": "Claims support it.",
            "limitations": "",
            "assessment_evidence_ids": [],
        },
        allowed_evidence_ids={allowed_id},
        min_confidence=0.8,
    )
    assert unsupported["should_apply"] is False


def test_source_claim_auto_assessment_notes_preserve_source_claim_and_evidence():
    evidence_id = uuid4()
    source = SimpleNamespace(name="Memory Channel", source_type="analyst")
    claim = SimpleNamespace(statement="HBM demand raises NAND pricing power.")
    notes = SourceService._claim_assessment_notes(
        proposal={
            "assessment": "partially_correct",
            "confidence": 0.81,
            "rationale": "Direction was right but timing lagged.",
            "limitations": "Only one later source tested the timing.",
            "assessment_evidence": [evidence_id],
        },
        source=source,
        claim=claim,
        evidence_lookup={
            evidence_id: {
                "node_type": "fact",
                "text": "NAND pricing improved after HBM allocation tightened supply.",
            }
        },
    )

    assert "Auto-assessed by source outcome assessor" in notes
    assert "source=Memory Channel" in notes
    assert "claim=HBM demand raises NAND pricing power" in notes
    assert "assessment=partially_correct" in notes
    assert f"fact:{evidence_id}" in notes


def test_source_claim_followup_query_names_claim_timing_and_direct_evidence_need():
    record = SimpleNamespace(
        ticker="MEMB",
        claim_time=datetime(2026, 1, 10, tzinfo=timezone.utc),
    )
    claim = SimpleNamespace(statement="HBM demand will tighten NAND supply by Q2.")

    query = SourceService._source_claim_followup_query(record, claim)

    assert "after 2026-01-10" in query
    assert "HBM demand will tighten NAND supply" in query
    assert "for MEMB" in query
    assert "direct outcome, timing, mechanism, and magnitude" in query


@pytest.mark.asyncio
async def test_source_claim_followup_research_uses_existing_research_path(monkeypatch):
    evidence_id = uuid4()
    captured = {}

    async def fake_run_ad_hoc_request(self, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            started=True,
            reason="ok",
            evidence_id=evidence_id,
            processed=False,
            query=kwargs["query"],
            title=kwargs["title"],
        )

    monkeypatch.setattr(
        "investos.services.research.ResearchService.run_ad_hoc_request",
        fake_run_ad_hoc_request,
    )

    service = SourceService(session=object())
    record = SimpleNamespace(
        id=uuid4(),
        source_id=uuid4(),
        ticker="MEMA",
        domain="memory",
        sector="semiconductors",
        regime="ai_infrastructure",
        claim_time=datetime(2026, 1, 10, tzinfo=timezone.utc),
    )
    claim = SimpleNamespace(
        id=uuid4(), statement="HBM demand tightens NAND supply and lifts MEMA margins."
    )

    result = await service._run_source_claim_followup_research(
        record=record,
        claim=claim,
        query="Find direct later evidence for MEMA NAND margin impact.",
    )

    assert result["started"] is True
    assert result["evidence_id"] == evidence_id
    assert captured["source_item_type"] == "source_claim_followup"
    assert captured["process_after_ingest"] is False
    assert captured["metadata_json"]["trigger"] == "source_claim_assessment_followup"
    assert captured["metadata_json"]["source_claim_record_id"] == str(record.id)
    assert captured["metadata_json"]["claim_id"] == str(claim.id)
    assert captured["metadata_json"]["ticker"] == "MEMA"


@pytest.mark.asyncio
async def test_source_claim_attempt_persists_retry_and_json_safe_research_trace():
    class FakeSession:
        def __init__(self):
            self.commits = 0

        async def commit(self):
            self.commits += 1

    session = FakeSession()
    service = SourceService(session=session)
    evidence_id = uuid4()
    record = SimpleNamespace(
        assessment_attempt_count=0,
        last_assessment_attempt_at=None,
        next_assessment_at=None,
        assessment_metadata=None,
    )

    await service._record_claim_assessment_attempt(
        record=record,
        result={
            "assessment": "indeterminate",
            "confidence": 0.35,
            "rationale": "Later evidence is adjacent but does not test the claim.",
            "limitations": "No direct outcome evidence.",
            "recommended_research_query": "Find direct later primary evidence.",
            "research_followup": {
                "started": True,
                "evidence_id": evidence_id,
            },
        },
        retry_hours=36,
    )

    assert session.commits == 1
    assert record.assessment_attempt_count == 1
    assert record.next_assessment_at > record.last_assessment_attempt_at
    assert (
        record.next_assessment_at - record.last_assessment_attempt_at
    ).total_seconds() == 36 * 3600
    assert record.assessment_metadata["attempt_count"] == 1
    assert record.assessment_metadata["research_followup"]["evidence_id"] == str(
        evidence_id
    )


def test_source_claim_batch_contract_exposes_queue_state_and_bounds():
    payload = SourceClaimBatchAssessmentCreate(
        scan_limit=750,
        retry_hours=48,
        research_missing_evidence=True,
    )
    response = SourceClaimBatchAssessmentResponse(
        scanned=500,
        due=3,
        eligible=497,
        deferred=6,
        proposed=3,
        applied=0,
    )

    assert payload.scan_limit == 750
    assert payload.retry_hours == 48
    assert response.eligible == 497
    assert response.deferred == 6


@pytest.mark.asyncio
async def test_source_claim_batch_dry_run_is_read_only_and_does_not_launch_research():
    record = SimpleNamespace(
        id=uuid4(),
        claim_time=datetime(2025, 1, 1, tzinfo=timezone.utc),
        horizon_days=30,
    )
    claim = SimpleNamespace(
        id=uuid4(),
        target_horizon="strategic",
        stale_after=None,
    )

    class FakeResult:
        def __init__(self, *, scalar=None, rows=None):
            self.scalar = scalar
            self.rows = rows or []

        def scalar_one(self):
            return self.scalar

        def all(self):
            return self.rows

    class FakeSession:
        def __init__(self):
            self.results = [
                FakeResult(scalar=4),
                FakeResult(rows=[(record, claim)]),
                FakeResult(rows=[]),
            ]
            self.commits = 0

        async def execute(self, _statement):
            return self.results.pop(0)

        async def commit(self):
            self.commits += 1

    session = FakeSession()
    service = SourceService(session=session)
    service.propose_claim_assessment = AsyncMock(
        return_value={
            "assessment": "indeterminate",
            "confidence": 0.0,
            "rationale": "No later evidence.",
            "limitations": "Needs follow-up.",
            "should_apply": False,
            "applied": False,
            "recommended_research_query": "Find later direct evidence.",
        }
    )
    service._run_source_claim_followup_research = AsyncMock()
    service._portfolio_claim_relevance = AsyncMock(
        return_value={
            claim.id: {
                "is_portfolio_relevant": True,
                "weight_pct": 12.5,
            }
        }
    )

    result = await service.assess_due_source_claims(
        apply=False,
        research_missing_evidence=True,
    )

    assert result["due"] == 1
    assert result["eligible"] == 1
    assert result["portfolio_relevant_eligible"] == 1
    assert result["selected_portfolio_relevant"] == 1
    assert result["deferred"] == 4
    assert session.commits == 0
    service._run_source_claim_followup_research.assert_not_awaited()


def test_source_claim_priority_uses_live_exposure_without_ticker_rules():
    now = datetime(2026, 7, 15, tzinfo=timezone.utc)
    record = SimpleNamespace(claim_time=datetime(2026, 6, 1, tzinfo=timezone.utc))
    claim = SimpleNamespace(importance="high", is_original=False)
    due_at = datetime(2026, 7, 1, tzinfo=timezone.utc)

    unrelated = source_claim_priority(record, claim, due_at, now)
    portfolio_linked = source_claim_priority(
        record,
        claim,
        due_at,
        now,
        portfolio_relevant=True,
        portfolio_weight_pct=18.0,
    )

    assert portfolio_linked > unrelated
    assert portfolio_linked - unrelated == pytest.approx(38.0)


def test_source_claim_label_candidates_cover_names_and_tickers_generically():
    candidates = SourceService._subject_label_candidates(
        "Memory Beta Inc. · MEMB (NASDAQ)"
    )

    assert "memory beta inc memb nasdaq" in candidates
    assert "memory beta inc" in candidates
    assert "memb" in candidates


def test_source_claim_batch_reserves_capacity_for_due_retries():
    fresh = [
        (
            SimpleNamespace(assessment_attempt_count=0),
            SimpleNamespace(id=f"fresh-{index}"),
            float(100 - index),
        )
        for index in range(5)
    ]
    retries = [
        (
            SimpleNamespace(assessment_attempt_count=2),
            SimpleNamespace(id=f"retry-{index}"),
            float(50 - index),
        )
        for index in range(3)
    ]

    selected = SourceService._select_fair_claim_batch(
        [*fresh, *retries],
        limit=4,
        retry_share=0.25,
    )

    selected_ids = [claim.id for _record, claim in selected]
    assert selected_ids[:3] == ["fresh-0", "fresh-1", "fresh-2"]
    assert selected_ids[3] == "retry-0"
