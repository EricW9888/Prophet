"""Resume checkpointed chat work until a non-replayable state-change boundary."""

import asyncio
from datetime import UTC, datetime, timedelta

from investos.config import settings
from investos.core.llm import _cooldown_remaining
from investos.core.provider_health import (
    ProviderHealthStore,
    probe_llm_provider,
    retryable_provider_failure,
)
from investos.core.providers import llm_provider_capability
from investos.db import async_session_maker
from investos.schemas.agent import AgentTurnRequest
from investos.services.agent import AgentService
from investos.services.chat_turn_state import ChatTurnState
from investos.services.runtime_settings import RuntimeSettingsStore

PERMANENT_CODES = {
    "http_400",
    "http_401",
    "http_403",
    "http_404",
    "http_410",
    "http_422",
}


def retryable(exc):
    return retryable_provider_failure(exc)


def waiting_configuration(record):
    llm = RuntimeSettingsStore.load().llm
    capability = llm_provider_capability(llm.provider)
    model = (
        (llm.vision_model or (capability.default_vision_model if capability else ""))
        if record.request_context.get("wait_role") == "reader"
        else None
    )
    return llm.provider, model


def retry_time(health, cooldown=0):
    now = datetime.now(UTC)
    candidates = [
        now + timedelta(seconds=max(cooldown, settings.CHAT_JOB_HEALTH_POLL_SECONDS))
    ]
    for field in ("next_probe_at", "lease_until"):
        if health.get(field):
            candidates.append(
                datetime.fromisoformat(health[field].replace("Z", "+00:00"))
            )
    return max(candidates)


async def wait_for_capacity(tracker, record):
    while record.status == "deferred":
        provider, model = waiting_configuration(record)
        health = ProviderHealthStore.public_status(provider, model)
        if health.get("failure_code") in PERMANENT_CODES or health["state"] in {
            "waiting_for_config",
            "storage_unavailable",
        }:
            raise RuntimeError(
                "The AI provider needs configuration or repair before this question can continue."
            )
        cooldown = _cooldown_remaining(provider, model)
        if ProviderHealthStore.can_admit_work(health) and not cooldown:
            return
        next_probe = health.get("next_probe_at")
        if not cooldown and (
            not next_probe
            or datetime.fromisoformat(next_probe.replace("Z", "+00:00"))
            <= datetime.now(UTC)
        ):
            health = await probe_llm_provider(provider=provider, model=model)
            if ProviderHealthStore.can_admit_work(health):
                return
        planned = retry_time(health, cooldown)
        record.retry_at = planned
        delay = min(
            settings.CHAT_JOB_HEALTH_POLL_SECONDS,
            max(0.05, (planned - datetime.now(UTC)).total_seconds()),
        )
        await asyncio.sleep(delay)


def start_agent_turn_job(tracker, record, payload=None):
    if record._task and not record._task.done():
        return record._task
    if record.status in {"completed", "error", "cancelled"}:
        return None
    saved = record.request_context.get("resume_payload")
    request = AgentTurnRequest.model_validate(saved) if saved else payload
    if request is None:
        raise ValueError("The original question is unavailable.")
    if saved and (
        request.use_session_attachments
        or request.session_id is None
        or record.session_id != request.session_id
        or not ChatTurnState.can_resume(record.request_context)
        or (not record.request_context.get("turn_state") and request.auto_execute)
    ):
        raise ValueError(
            "The saved request cannot be safely replayed; inspect its activity before retrying."
        )

    state = (
        ChatTurnState(record.request_context["turn_state"], tracker._save_to_disk)
        if "turn_state" in record.request_context
        else None
    )

    async def run():
        role = record.request_context.get("wait_role", "analyst")

        async def progress(phase, message, detail=None):
            nonlocal role
            if phase == "reading_files" and (detail or {}).get("model"):
                role = "reader"
            elif phase == "analyzing_files":
                role = "analyst"
            tracker.add_event(record.id, phase=phase, message=message, detail=detail)

        try:
            while record.status not in {"cancelled", "completed", "error"}:
                if record.status == "deferred":
                    await wait_for_capacity(tracker, record)
                tracker.mark_running(
                    record.id,
                    task=asyncio.current_task(),
                    message=(
                        "Continuing this question."
                        if saved
                        else "Prophet started this turn."
                    ),
                )
                try:
                    async with async_session_maker() as session:
                        result = await AgentService(session).handle_turn(
                            request,
                            progress_callback=progress,
                            **({"conversation_request_id": record.id} if saved else {}),
                            **({"turn_state": state} if state is not None else {}),
                        )
                    tracker.complete(record.id, result=result.model_dump(mode="json"))
                    return
                except Exception as exc:
                    if (
                        not saved
                        or not ChatTurnState.can_resume(record.request_context)
                        or not retryable(exc)
                    ):
                        raise
                    record.request_context["wait_role"] = role
                    provider, model = waiting_configuration(record)
                    health = ProviderHealthStore.public_status(provider, model)
                    if health.get("failure_code") in PERMANENT_CODES:
                        raise
                    tracker.defer(
                        record.id,
                        retry_at=retry_time(
                            health, _cooldown_remaining(provider, model)
                        ),
                        message="Waiting for the AI provider. Completed work is saved; this question will resume automatically.",
                    )
        except asyncio.CancelledError:
            # User cancellation is already marked; shutdown leaves a resumable job.
            pass
        except Exception as exc:
            tracker.fail(record.id, error=str(exc))
        finally:
            if record._task is asyncio.current_task():
                record._task = None

    task = asyncio.create_task(run())
    record._task = task
    return task


def resume_agent_turns(tracker):
    for record in tracker.list_jobs(status="active", job_kind="agent_turn", limit=None):
        if record.request_context.get("resume_payload"):
            try:
                start_agent_turn_job(tracker, record)
            except ValueError as exc:
                tracker.fail(record.id, error=str(exc))
