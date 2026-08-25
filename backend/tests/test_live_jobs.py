from __future__ import annotations

import pytest
from fastapi import HTTPException

from investos.api.routes.agent import _job_response as agent_job_response
from investos.api.routes.source import _media_job_response
from investos.config import settings
from investos.services.live_jobs import LiveJobTracker


def test_live_job_kinds_remain_isolated_and_persisted(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "STORAGE_DIR", str(tmp_path))
    tracker = LiveJobTracker()
    agent_job = tracker.create_job(
        request_message="Analyze MU",
        session_id=None,
    )
    media_job = tracker.create_job(
        request_message="https://youtu.be/dQw4w9WgXcQ",
        session_id=None,
        job_kind="youtube_ingestion",
        queued_message="Queued for YouTube transcript ingestion.",
    )
    tracker.complete(
        media_job.id,
        result={"ok": True},
        message="YouTube transcript ingestion finished.",
    )

    assert [item.id for item in tracker.list_jobs(job_kind="agent_turn")] == [
        agent_job.id
    ]
    assert [item.id for item in tracker.list_jobs(job_kind="youtube_ingestion")] == [
        media_job.id
    ]

    restored = LiveJobTracker()
    assert restored.get(media_job.id).job_kind == "youtube_ingestion"
    assert restored.get(media_job.id).result == {"ok": True}
    assert (
        restored.get(media_job.id).events[-1].message
        == "YouTube transcript ingestion finished."
    )


def test_job_response_routes_reject_other_job_kinds(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "STORAGE_DIR", str(tmp_path))
    tracker = LiveJobTracker()
    agent_job = tracker.create_job(
        request_message="Analyze MU",
        session_id=None,
    )
    media_job = tracker.create_job(
        request_message="https://youtu.be/dQw4w9WgXcQ",
        session_id=None,
        job_kind="youtube_ingestion",
    )

    with pytest.raises(HTTPException) as agent_error:
        agent_job_response(tracker, media_job.id)
    with pytest.raises(HTTPException) as media_error:
        _media_job_response(tracker, agent_job.id)

    assert agent_error.value.status_code == 404
    assert media_error.value.status_code == 404


def test_restart_marks_persisted_active_job_as_interrupted(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "STORAGE_DIR", str(tmp_path))
    tracker = LiveJobTracker()
    job = tracker.create_job(
        request_message="https://youtu.be/dQw4w9WgXcQ",
        session_id=None,
        job_kind="youtube_ingestion",
    )

    restored = LiveJobTracker()
    recovered = restored.get(job.id)

    assert recovered.status == "error"
    assert recovered.error == "Job interrupted by backend restart."
    assert recovered.events[-1].phase == "error"
