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
from investos.core.provider_health import (
    LLMProviderCooldownError,
    ProviderHealthStore,
    guarded_provider_call,
)
from investos.core.storage import LocalStorage
from investos.models.entity import Entity
from investos.models.evidence import RawEvidence
from investos.models.portfolio import Transaction
from investos.models.watcher import ActiveWatcher
from investos.schemas.agent import AgentTurnRequest
from investos.services import agent_turn_jobs
from investos.services.agent import AgentService
from investos.services.chat_attachments import ChatAttachmentService
from investos.services.chat_turn_state import ChatTurnState
from investos.services.live_jobs import LiveJobTracker
from investos.services.reasoning import ReasoningService
from investos.services.retrieval import RetrievalService
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


@pytest.mark.parametrize("failure_stage", ["scope", "analysis", "independent_review"])
async def test_text_analysis_resumes_frozen_stages_after_restart_without_repeating_research(
    workflow_session,
    monkeypatch,
    failure_stage,
):
    session = workflow_session
    entity = Entity(name=f"Synthetic issuer {uuid4()}", entity_type="company")
    session.add(entity)
    await session.flush()

    @asynccontextmanager
    async def same_session():
        yield session

    monkeypatch.setattr(agent_turn_jobs, "async_session_maker", same_session)
    intent = AsyncMock(
        return_value={"route": "analyze", "requires_fresh_research": True}
    )
    plan = AsyncMock(
        return_value={
            "research_mode": "focused",
            "external_research_required": True,
            "retrieval_query": "Synthetic issuer demand",
            "coverage_scope": "question_specific",
        }
    )
    fresh = AsyncMock(
        return_value={
            "required": True,
            "status": "ok",
            "query": "Synthetic issuer demand",
            "checked_at": "2026-11-14T06:00:00Z",
            "results": [],
        }
    )
    analysis_result = {
        "stance": "uncertain",
        "confidence_band": "low",
        "thesis_summary": "Synthetic demand remains uncertain.",
        "reasoning": "The supplied evidence is insufficient.",
        "supporting_evidence_ids": [],
        "contradicting_evidence_ids": [],
        "material_assertions": [],
    }
    analysis = AsyncMock(
        side_effect=(
            [quota_error(), analysis_result] if failure_stage == "analysis" else None
        ),
        return_value=analysis_result,
    )
    monkeypatch.setattr(AgentService, "_classify_turn_intent", intent)
    monkeypatch.setattr(AgentService, "_plan_question_research", plan)
    monkeypatch.setattr(AgentService, "_maybe_fresh_research_context", fresh)
    monkeypatch.setattr(
        ReasoningService, "_reason_with_llm", guarded_provider_call(analysis)
    )
    resolve = AgentService.resolve_context
    scope_calls = 0

    async def resolve_scope(self, **kwargs):
        nonlocal scope_calls
        scope_calls += 1
        if failure_stage == "scope" and scope_calls == 1:
            try:
                await guarded_provider_call(AsyncMock(side_effect=quota_error()))()
            except httpx.HTTPStatusError as exc:
                raise RuntimeError("Scope selection temporarily unavailable") from exc
        return await resolve(self, **kwargs)

    monkeypatch.setattr(AgentService, "resolve_context", resolve_scope)
    monkeypatch.setattr(
        AgentService, "_dispatch_dynamic_subagents", AsyncMock(return_value={})
    )

    async def unchanged(self, payload, resolved, context, result, lenses):
        return result

    monkeypatch.setattr(AgentService, "_synthesize_and_refine", unchanged)
    monkeypatch.setattr(
        AgentService, "_maybe_auto_research_after_gap", AsyncMock(return_value=None)
    )

    async def answer_plan(self, **kwargs):
        return self._fast_path_orchestration(
            kwargs["payload"], kwargs["reasoning_result"]
        )

    monkeypatch.setattr(AgentService, "_plan_agent_actions", answer_plan)
    attempts = 0

    async def critique(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if failure_stage == "independent_review" and attempts == 1:
            raise quota_error()
        return None

    monkeypatch.setattr(
        ReasoningService, "_create_critique", guarded_provider_call(critique)
    )
    monkeypatch.setattr(llm, "call_llm_json", AsyncMock(return_value={"ok": True}))
    tracker = LiveJobTracker()
    deferred = asyncio.Event()
    original_defer = tracker.defer

    def capture(*args, **kwargs):
        original_defer(*args, **kwargs)
        deferred.set()

    monkeypatch.setattr(tracker, "defer", capture)
    result = await create_agent_turn_job(
        AgentTurnRequest(
            message="Investigate the issuer's current demand.",
            subject_id=entity.id,
            subject_type="entity",
            auto_execute=False,
        ),
        SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(live_jobs=tracker))),
        session,
    )
    await asyncio.wait_for(deferred.wait(), 10)
    record = tracker.get(result.job_id)
    stages = record.request_context["turn_state"]["stages"]
    assert "intent" in stages
    snapshot = stages.get("evidence", {}).get("value")
    if failure_stage != "scope":
        assert {"context", "evidence", "fresh_research"} <= stages.keys()
        # Evidence arriving while a request waits must not replace its captured
        # input. Recovery must not hydrate the live graph again.
        monkeypatch.setattr(
            RetrievalService,
            "hydrate_packet",
            AsyncMock(side_effect=AssertionError("Saved evidence was rehydrated")),
        )
    assert ("analysis" in stages) == (failure_stage == "independent_review")
    assert "independent_review" not in stages
    # Unattended work shares this outage; it must not spend another request or
    # count a successful health probe as completed research.
    background_model = AsyncMock(return_value={"completed": True})
    with pytest.raises(LLMProviderCooldownError):
        await guarded_provider_call(background_model)()
    background_model.assert_not_awaited()
    await tracker.shutdown()
    registry = ProviderHealthStore._read()
    for health in registry.records.values():
        health.next_probe_at = datetime.now(UTC) - timedelta(seconds=1)
    ProviderHealthStore._write(registry)
    resumed = LiveJobTracker()
    recovered = resumed.get(record.id)
    await asyncio.wait_for(agent_turn_jobs.start_agent_turn_job(resumed, recovered), 10)
    assert recovered.status == "completed", recovered.error
    assert await guarded_provider_call(background_model)() == {"completed": True}
    assert background_model.await_count == 1
    assert intent.await_count == plan.await_count == fresh.await_count == 1
    assert analysis.await_count == (2 if failure_stage == "analysis" else 1)
    assert scope_calls == (2 if failure_stage == "scope" else 1)
    assert attempts == (2 if failure_stage == "independent_review" else 1)
    if snapshot is not None:
        assert (
            recovered.request_context["turn_state"]["stages"]["evidence"]["value"]
            == snapshot
        )
    assert recovered.result["research_receipt"]["evidence_prepared_at"]
    answer = recovered.result["assistant_message"]
    messages = list(
        await session.scalars(
            select(RawEvidence).where(
                RawEvidence.metadata_json["chat_job_id"].astext == str(record.id)
            )
        )
    )
    assert sorted(row.metadata_json["role"] for row in messages) == [
        "assistant",
        "user",
    ]

    # A crash after the answer's DB commit and before its file checkpoint must
    # still recover the committed answer, without another model or search call.
    recovered.status, recovered.result = "running", None
    recovered.request_context["turn_state"]["stages"].pop("response")
    resumed._save_to_disk()
    repeated = LiveJobTracker()
    await asyncio.wait_for(
        agent_turn_jobs.start_agent_turn_job(repeated, repeated.get(record.id)), 10
    )
    assert repeated.get(record.id).result["assistant_message"] == answer
    assert attempts == (2 if failure_stage == "independent_review" else 1)
    assert analysis.await_count == (2 if failure_stage == "analysis" else 1)
    assert fresh.await_count == 1
    assert (
        await session.scalar(
            select(func.count(RawEvidence.id)).where(
                RawEvidence.metadata_json["chat_job_id"].astext == str(record.id)
            )
        )
        == 2
    )


async def test_checkpointed_action_boundary_is_not_replayed_after_restart(
    workflow_session, monkeypatch
):
    session = workflow_session
    entity = Entity(name=f"Synthetic action target {uuid4()}", entity_type="company")
    session.add(entity)
    await session.flush()

    @asynccontextmanager
    async def same_session():
        yield session

    monkeypatch.setattr(agent_turn_jobs, "async_session_maker", same_session)
    monkeypatch.setattr(
        AgentService,
        "_classify_turn_intent",
        AsyncMock(return_value={"route": "operate"}),
    )
    tracker = LiveJobTracker()
    payload = AgentTurnRequest(
        session_id=uuid4(),
        message="Create a watch",
        auto_execute=True,
        use_session_attachments=False,
        subject_id=entity.id,
        subject_type="entity",
    )
    calls = 0

    async def operate(self, **kwargs):
        nonlocal calls
        calls += 1
        raise asyncio.CancelledError()

    monkeypatch.setattr(AgentService, "_maybe_operating_context_answer", operate)
    result = await create_agent_turn_job(
        payload,
        SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(live_jobs=tracker))),
        session,
    )
    record = tracker.get(result.job_id)
    await record._task
    restored = LiveJobTracker()
    agent_turn_jobs.resume_agent_turns(restored)
    assert restored.get(record.id).status == "error"
    assert "automatic replay is disabled" in restored.get(record.id).error
    assert calls == 1


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


@pytest.mark.parametrize("status", [401, 410, "unsupported_protocol"])
@pytest.mark.parametrize("with_file", [False, True])
async def test_permanent_provider_errors_do_not_loop(status, with_file, monkeypatch):
    tracker = LiveJobTracker()
    payload = AgentTurnRequest(
        session_id=uuid4(),
        message="Read this file",
        attachments=[{"name": "synthetic.txt", "text": "EXMPL"}] if with_file else [],
        auto_execute=False,
        use_session_attachments=False,
    )
    record = tracker.create_job(
        request_message=payload.message,
        session_id=payload.session_id,
        request_context={
            "resume_payload": payload.model_dump(mode="json"),
            "turn_state": ChatTurnState.initial(),
        },
    )
    model = AsyncMock(
        side_effect=(
            quota_error(status)
            if isinstance(status, int)
            else httpx.UnsupportedProtocol("Synthetic invalid provider endpoint")
        )
    )
    monkeypatch.setattr("investos.services.agent.AgentService.handle_turn", model)
    await asyncio.wait_for(agent_turn_jobs.start_agent_turn_job(tracker, record), 5)
    assert record.status == "error" and model.await_count == 1
    assert not any(event.phase == "deferred" for event in record.events)


@pytest.mark.parametrize("stage", ["analysis", "thin_analysis", "review"])
@pytest.mark.parametrize("status", [401, 429])
async def test_checkpointed_model_stages_do_not_turn_provider_errors_into_answers(
    monkeypatch, stage, status
):
    service = ReasoningService(None)
    model = AsyncMock(side_effect=quota_error(status))
    monkeypatch.setattr("investos.services.reasoning.call_llm_json", model)
    monkeypatch.setattr(settings, "REASONING_INDEPENDENT_REVIEW_ENABLED", True)
    monkeypatch.setattr(service, "_active_provider_unconfigured_label", lambda: None)
    monkeypatch.setattr(
        service, "_is_thin_packet", lambda packet: stage == "thin_analysis"
    )
    with pytest.raises(httpx.HTTPStatusError) as failure:
        if stage == "review":
            await service._create_critique(None, {}, {}, defer_on_provider_failure=True)
        else:
            await service._reason_with_llm(
                {}, system_prompt="Synthetic analysis", defer_on_provider_failure=True
            )
    assert failure.value.response.status_code == status
    assert model.await_count == 1


@pytest.mark.parametrize("with_file", [False, True])
async def test_cancelled_waiting_question_is_not_restarted(with_file, monkeypatch):
    tracker = LiveJobTracker()
    payload = AgentTurnRequest(
        session_id=uuid4(),
        message="Read this file",
        attachments=[{"name": "synthetic.txt", "text": "EXMPL"}] if with_file else [],
        auto_execute=False,
        use_session_attachments=False,
    )
    record = tracker.create_job(
        request_message=payload.message,
        session_id=payload.session_id,
        request_context={
            "resume_payload": payload.model_dump(mode="json"),
            "turn_state": ChatTurnState.initial(),
        },
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
    agent_turn_jobs.resume_agent_turns(restored)
    assert restored.get(record.id).status == "cancelled"
    assert restored.get(record.id)._task is None


def test_legacy_action_request_is_never_automatically_replayed():
    tracker = LiveJobTracker()
    payload = AgentTurnRequest(message="Create a watch", auto_execute=True)
    record = tracker.create_job(
        request_message=payload.message,
        session_id=None,
        request_context={"resume_payload": payload.model_dump(mode="json")},
    )
    tracker = LiveJobTracker()
    agent_turn_jobs.resume_agent_turns(tracker)
    assert tracker.get(record.id).status == "error"
    assert "automatic replay is disabled" in tracker.get(record.id).error


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
