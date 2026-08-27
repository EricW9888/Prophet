from unittest.mock import AsyncMock

import pytest

from investos.services import media_investigation as investigation_module
from investos.services.media_investigation import MediaInvestigationPlanner


@pytest.mark.asyncio
async def test_media_planner_preserves_specific_gap_and_requested_pass(monkeypatch):
    response = {
        "materiality": "high",
        "first_pass_sufficient": False,
        "confidence": 0.9,
        "reason": "The captions reference an unreadable chart.",
        "resolved_points": ["Management discussed NAND pricing."],
        "unresolved_gaps": [
            {
                "description": "The chart's quarterly price series is absent.",
                "why_material": "It determines whether pricing accelerated.",
                "recommended_pass": "frame_ocr",
            }
        ],
        "requested_passes": ["frame_ocr"],
        "followup_questions": ["What values are shown in the pricing chart?"],
    }
    call = AsyncMock(return_value=response)
    monkeypatch.setattr(investigation_module, "call_llm_json", call)

    result = await MediaInvestigationPlanner().assess(
        transcript="As shown in the chart, pricing accelerated.",
        representation="caption_transcript",
        title="Memory update",
        source_url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    )

    assert result["status"] == "complete"
    assert result["requested_passes"] == ["frame_ocr"]
    assert result["unresolved_gaps"] == response["unresolved_gaps"]
    assert "do not infer unseen" in call.await_args.kwargs["system_prompt"]


@pytest.mark.asyncio
async def test_media_planner_failure_defers_without_inventing_followups(monkeypatch):
    monkeypatch.setattr(
        investigation_module,
        "call_llm_json",
        AsyncMock(side_effect=TimeoutError("provider timeout")),
    )

    result = await MediaInvestigationPlanner().assess(
        transcript="A complete transcript remains usable evidence.",
        representation="caption_transcript",
        title=None,
        source_url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    )

    assert result["status"] == "deferred"
    assert result["requested_passes"] == []
    assert "provider timeout" in result["reason"]
