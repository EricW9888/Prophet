import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import func, select

from investos.api.routes.agent import create_agent_turn_job
from investos.config import settings
from investos.core import llm
from investos.core.provider_health import ProviderHealthStore, guarded_provider_call
from investos.core.storage import LocalStorage
from investos.models.evidence import RawEvidence
from investos.models.portfolio import Transaction
from investos.models.watcher import ActiveWatcher
from investos.schemas.agent import AgentTurnRequest
from investos.services import agent_turn_jobs
from investos.services.agent import AgentService
from investos.services.chat_attachments import ChatAttachmentService
from investos.services.live_jobs import LiveJobTracker
from investos.services.runtime_settings import RuntimeSettings, RuntimeSettingsStore
from tests.test_chat_attachments import pdf_bytes, upload
from tests.test_investor_workflows import workflow_session


@pytest.fixture(autouse=True)
def isolated_jobs(tmp_path, monkeypatch):
    runtime = RuntimeSettings()
    runtime.llm.api_key = "synthetic-test-credential"
    monkeypatch.setattr(RuntimeSettingsStore, "load", lambda: runtime)
    monkeypatch.setattr(settings, "STORAGE_DIR", str(tmp_path))
    monkeypatch.setattr(
        ProviderHealthStore, "_path", staticmethod(lambda: tmp_path / "health.json")
    )
    monkeypatch.setattr(llm, "_provider_cooldowns", {})
    storage = LocalStorage(str(tmp_path))
    for module in ("agent", "ingestion", "chat_attachments"):
        monkeypatch.setattr(f"investos.services.{module}.LocalStorage", lambda: storage)
    return storage


def quota_error(status=429):
    return httpx.HTTPStatusError(
        "Synthetic provider interruption",
        request=httpx.Request("POST", "https://llm.example.test/v1/chat/completions"),
        response=httpx.Response(status, headers={"Retry-After": "120"}),
    )


async def test_file_question_resumes_after_restart_without_rereading_or_duplicating_answer(
    workflow_session, isolated_jobs, monkeypatch
):
    session = workflow_session

    @asynccontextmanager
    async def same_session():
        yield session

    monkeypatch.setattr(agent_turn_jobs, "async_session_maker", same_session)
    row = await upload(
        ChatAttachmentService(session, isolated_jobs), "synthetic.pdf", pdf_bytes(2)
    )
    visual = AsyncMock(
        return_value={
            "pages": [
                {
                    "page": page,
                    "observations": f"Synthetic page {page}: EXMPL, 2 shares at USD 25",
                }
                for page in (1, 2)
            ]
        }
    )
    monkeypatch.setattr("investos.services.chat_attachments.call_llm_json", visual)
    attempts = 0

    async def generate(**kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise quota_error()
        return {
            "answer": "synthetic.pdf, pages 1–2: EXMPL has 2 shares at USD 25, or USD 50.",
            "rationale": "File analysis only.",
        }

    analyst = AsyncMock(side_effect=guarded_provider_call(generate))
    monkeypatch.setattr("investos.services.agent.call_llm_json", analyst)
    monkeypatch.setattr(llm, "call_llm_json", AsyncMock(return_value={"ok": True}))
    tracker = LiveJobTracker()
    deferred = asyncio.Event()
    original_defer = tracker.defer

    def capture(*args, **kwargs):
        original_defer(*args, **kwargs)
        deferred.set()

    monkeypatch.setattr(tracker, "defer", capture)
    counts = [
        await session.scalar(select(func.count(model.id)))
        for model in (Transaction, ActiveWatcher)
    ]
    result = await create_agent_turn_job(
        AgentTurnRequest(
            message="What does the file show?",
            attachment_ids=[row.id],
            auto_execute=True,
        ),
        SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(live_jobs=tracker))),
        session,
    )
    await asyncio.wait_for(deferred.wait(), 5)
    record = tracker.get(result.job_id)
    assert record.status == "deferred" and record.retry_at > datetime.now(UTC)
    saved = record.request_context["resume_payload"]
    assert saved["auto_execute"] is False and saved["use_session_attachments"] is False
    assert saved["attachment_ids"] == [str(row.id)] and saved["session_id"]
    await tracker.shutdown()

    # A persisted provider delay is made due in this isolated test; the real
    # shared probe and model-admission guards still own recovery.
    registry = ProviderHealthStore._read()
    for health in registry.records.values():
        health.next_probe_at = datetime.now(UTC) - timedelta(seconds=1)
    ProviderHealthStore._write(registry)
    resumed = LiveJobTracker()
    recovered = resumed.get(record.id)
    task = agent_turn_jobs.start_agent_turn_job(resumed, recovered)
    assert agent_turn_jobs.start_agent_turn_job(resumed, recovered) is task
    await asyncio.wait_for(task, 5)
    assert recovered.status == "completed", recovered.error
    assert attempts == 2 and visual.await_count == 1
    assert recovered.result["session_id"] == saved["session_id"]
    assert recovered.result["attachments"][0]["visual_pages_read"] == 2
    assert [
        await session.scalar(select(func.count(model.id)))
        for model in (Transaction, ActiveWatcher)
    ] == counts
    messages = (
        await session.scalars(
            select(RawEvidence).where(
                RawEvidence.metadata_json["chat_job_id"].astext == str(record.id)
            )
        )
    ).all()
    assert sorted(item.metadata_json["role"] for item in messages) == [
        "assistant",
        "user",
    ]

    # Simulate a crash after the DB answer commit but before the job completion
    # checkpoint. Recovery returns the saved answer without another generation.
    recovered.status, recovered.result = "running", None
    resumed._save_to_disk()
    repeated = LiveJobTracker()
    await asyncio.wait_for(
        agent_turn_jobs.start_agent_turn_job(repeated, repeated.get(record.id)), 5
    )
    assert repeated.get(record.id).status == "completed" and attempts == 2
    assert (
        await session.scalar(
            select(func.count(RawEvidence.id)).where(
                RawEvidence.metadata_json["chat_job_id"].astext == str(record.id)
            )
        )
        == 2
    )


@pytest.mark.parametrize("status", [401, 410])
async def test_permanent_provider_errors_do_not_loop(status, monkeypatch):
    tracker = LiveJobTracker()
    payload = AgentTurnRequest(
        session_id=uuid4(),
        message="Read this file",
        attachments=[{"name": "synthetic.txt", "text": "EXMPL"}],
        auto_execute=False,
        use_session_attachments=False,
    )
    record = tracker.create_job(
        request_message=payload.message,
        session_id=payload.session_id,
        request_context={"resume_payload": payload.model_dump(mode="json")},
    )
    model = AsyncMock(side_effect=quota_error(status))
    monkeypatch.setattr("investos.services.agent.AgentService.handle_turn", model)
    await asyncio.wait_for(agent_turn_jobs.start_agent_turn_job(tracker, record), 5)
    assert record.status == "error" and model.await_count == 1
    assert not any(event.phase == "deferred" for event in record.events)


async def test_cancelled_waiting_question_is_not_restarted(monkeypatch):
    tracker = LiveJobTracker()
    payload = AgentTurnRequest(
        session_id=uuid4(),
        message="Read this file",
        attachments=[{"name": "synthetic.txt", "text": "EXMPL"}],
        auto_execute=False,
        use_session_attachments=False,
    )
    record = tracker.create_job(
        request_message=payload.message,
        session_id=payload.session_id,
        request_context={"resume_payload": payload.model_dump(mode="json")},
    )
    paused = asyncio.Event()

    async def wait(*args):
        paused.set()
        await asyncio.Event().wait()

    record.status = "deferred"
    monkeypatch.setattr(agent_turn_jobs, "wait_for_capacity", wait)
    task = agent_turn_jobs.start_agent_turn_job(tracker, record)
    await asyncio.wait_for(paused.wait(), 5)
    assert tracker.cancel_job(record.id)
    await task
    restored = LiveJobTracker()
    agent_turn_jobs.resume_file_questions(restored)
    assert restored.get(record.id).status == "cancelled"
    assert restored.get(record.id)._task is None


def test_action_request_is_never_automatically_replayed():
    tracker = LiveJobTracker()
    payload = AgentTurnRequest(message="Create a watch", auto_execute=True)
    record = tracker.create_job(
        request_message=payload.message,
        session_id=None,
        request_context={"resume_payload": payload.model_dump(mode="json")},
    )
    tracker = LiveJobTracker()
    agent_turn_jobs.resume_file_questions(tracker)
    assert tracker.get(record.id).status == "error"
    assert "not an eligible read-only file question" in tracker.get(record.id).error


async def test_partial_conversation_commit_does_not_duplicate_the_user_message(
    workflow_session, isolated_jobs, monkeypatch
):
    session = workflow_session
    row = await upload(
        ChatAttachmentService(session, isolated_jobs),
        "synthetic.csv",
        b"ticker,shares,price\nEXMPL,2,25",
    )
    model = AsyncMock(
        return_value={
            "answer": "synthetic.csv: EXMPL has 2 shares at USD 25.",
            "rationale": "File analysis only.",
        }
    )
    monkeypatch.setattr("investos.services.agent.call_llm_json", model)
    agent = AgentService(session)
    original = agent._store_agent_memory
    interrupted = False

    async def store(**kwargs):
        nonlocal interrupted
        if kwargs["metadata_json"]["role"] == "assistant" and not interrupted:
            interrupted = True
            raise asyncio.CancelledError()
        await original(**kwargs)

    monkeypatch.setattr(agent, "_store_agent_memory", store)
    request_id, session_id = uuid4(), uuid4()
    payload = AgentTurnRequest(
        session_id=session_id,
        message="Read the file",
        attachment_ids=[row.id],
        use_session_attachments=False,
        auto_execute=False,
    )
    with pytest.raises(asyncio.CancelledError):
        await agent.handle_turn(payload, conversation_request_id=request_id)
    response = await agent.handle_turn(payload, conversation_request_id=request_id)
    history = await agent.conversation_history(session_id, None, "portfolio")
    assert response.assistant_message and model.await_count == 2
    assert len(history.entries) == 2
    assert {entry.job_id for entry in history.entries} == {request_id}
    assert [entry.role for entry in history.entries] == ["user", "assistant"]
