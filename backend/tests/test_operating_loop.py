from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

import investos.services.operating_loop as operating_loop_module
from investos.services.operating_loop import OperatingLoopService


@pytest.mark.asyncio
async def test_refresh_subject_routes_evidence_provenance_to_shadow(monkeypatch):
    subject_id = uuid4()
    evidence_id = uuid4()
    packet_id = uuid4()
    previous_state = SimpleNamespace(
        current_stance="neutral",
        confidence_band="low",
    )
    current_state = SimpleNamespace(
        current_stance="bullish",
        confidence_band="medium",
    )
    coverage = SimpleNamespace(
        contradiction_count=0,
        overall_coverage_score=64,
    )
    service = OperatingLoopService(session=None)
    monkeypatch.setattr(
        service,
        "_conclusion_state",
        AsyncMock(side_effect=[previous_state, current_state, current_state]),
    )
    monkeypatch.setattr(service, "_coverage_map", AsyncMock(return_value=coverage))

    retrieve_evidence = AsyncMock(return_value=SimpleNamespace(id=packet_id))
    run_analysis = AsyncMock(
        return_value=(
            SimpleNamespace(id=uuid4()),
            {"stance": "bullish", "confidence_band": "medium"},
        )
    )
    refresh_queue = AsyncMock(return_value=[])
    monkeypatch.setattr(
        operating_loop_module,
        "RetrievalService",
        lambda _session: SimpleNamespace(retrieve_evidence=retrieve_evidence),
    )
    monkeypatch.setattr(
        operating_loop_module,
        "ReasoningService",
        lambda _session: SimpleNamespace(run_analysis=run_analysis),
    )
    monkeypatch.setattr(
        operating_loop_module,
        "ReviewService",
        lambda _session: SimpleNamespace(refresh_queue=refresh_queue),
    )

    captured: dict[str, object] = {}

    async def trigger_shadow(
        *,
        subject_id,
        subject_type,
        subject_name,
        trigger_reason,
        previous_state,
        current_state,
        coverage,
        raw_evidence_id,
    ):
        captured.update(
            {
                "subject_id": subject_id,
                "subject_type": subject_type,
                "subject_name": subject_name,
                "trigger_reason": trigger_reason,
                "previous_state": previous_state,
                "current_state": current_state,
                "coverage": coverage,
                "raw_evidence_id": raw_evidence_id,
            }
        )
        return {"triggered": False, "reason": "trigger_threshold_not_met"}

    monkeypatch.setattr(service, "_maybe_trigger_shadow", trigger_shadow)

    result = await service.refresh_subject(
        subject_id=subject_id,
        subject_type="entity",
        subject_name="Test Company",
        trigger_reason="new evidence ingested",
        raw_evidence_id=evidence_id,
    )

    assert captured == {
        "subject_id": subject_id,
        "subject_type": "entity",
        "subject_name": "Test Company",
        "trigger_reason": "new evidence ingested",
        "previous_state": previous_state,
        "current_state": current_state,
        "coverage": coverage,
        "raw_evidence_id": evidence_id,
    }
    assert result["verification"] == {
        "triggered": False,
        "reason": "contradiction_pressure_below_threshold",
    }
    assert result["shadow"] == {
        "triggered": False,
        "reason": "trigger_threshold_not_met",
    }
    assert result["review_queue_items"] == 0
    retrieve_evidence.assert_awaited_once()
    run_analysis.assert_awaited_once_with(packet_id, include_critique=False)
    refresh_queue.assert_awaited_once_with()
