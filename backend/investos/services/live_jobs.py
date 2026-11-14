from __future__ import annotations

import asyncio
import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from investos.services.chat_turn_state import ChatTurnState


@dataclass
class LiveJobEvent:
    phase: str
    message: str
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    detail: dict[str, Any] | None = None


@dataclass
class LiveJobRecord:
    id: UUID
    job_kind: str
    status: str
    created_at: datetime
    updated_at: datetime
    request_message: str
    session_id: UUID | None = None
    request_context: dict[str, Any] = field(default_factory=dict)
    retry_at: datetime | None = None
    events: list[LiveJobEvent] = field(default_factory=list)
    result: dict[str, Any] | None = None
    error: str | None = None
    _task: asyncio.Task | None = None


class LiveJobTracker:
    def __init__(self) -> None:
        from pathlib import Path

        from investos.config import settings

        self._jobs: dict[UUID, LiveJobRecord] = {}
        self._storage_path = Path(settings.STORAGE_DIR) / "_system" / "live_jobs.json"
        self._load_from_disk()
        if self._recover_interrupted_jobs():
            self._save_to_disk()

    def _save_to_disk(self) -> None:
        try:
            self._storage_path.parent.mkdir(parents=True, exist_ok=True)
            serializable = {}
            for job_id, record in self._jobs.items():
                # Don't persist active tasks or huge error blobs
                serializable[str(job_id)] = {
                    "id": str(record.id),
                    "job_kind": record.job_kind,
                    "status": record.status,
                    "created_at": record.created_at.isoformat(),
                    "updated_at": record.updated_at.isoformat(),
                    "request_message": record.request_message,
                    "request_context": record.request_context,
                    "retry_at": (
                        record.retry_at.isoformat() if record.retry_at else None
                    ),
                    "session_id": str(record.session_id) if record.session_id else None,
                    "events": [
                        {
                            "phase": e.phase,
                            "message": e.message,
                            "created_at": e.created_at.isoformat(),
                            "detail": e.detail,
                        }
                        for e in record.events
                    ],
                    "result": record.result,
                    "error": record.error[:1000] if record.error else None,
                }
            descriptor, temporary = tempfile.mkstemp(
                dir=self._storage_path.parent, prefix=".live-jobs-"
            )
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    json.dump(serializable, handle)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, self._storage_path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        except OSError as exc:
            raise RuntimeError("Chat job state could not be saved.") from exc

    def _load_from_disk(self) -> None:
        if not self._storage_path.exists():
            return
        try:
            with open(self._storage_path, "r") as f:
                data = json.load(f)
                for job_id_str, raw in data.items():
                    job_id = UUID(job_id_str)
                    self._jobs[job_id] = LiveJobRecord(
                        id=job_id,
                        job_kind=str(raw.get("job_kind") or "agent_turn"),
                        status=raw["status"],
                        created_at=datetime.fromisoformat(raw["created_at"]),
                        updated_at=datetime.fromisoformat(raw["updated_at"]),
                        request_message=raw["request_message"],
                        request_context=raw.get("request_context") or {},
                        retry_at=(
                            datetime.fromisoformat(raw["retry_at"])
                            if raw.get("retry_at")
                            else None
                        ),
                        session_id=(
                            UUID(raw["session_id"]) if raw.get("session_id") else None
                        ),
                        events=[
                            LiveJobEvent(
                                phase=e["phase"],
                                message=e["message"],
                                created_at=datetime.fromisoformat(e["created_at"]),
                                detail=e.get("detail"),
                            )
                            for e in raw.get("events", [])
                        ],
                        result=raw.get("result"),
                        error=raw.get("error"),
                    )
        except Exception:
            pass

    def create_job(
        self,
        *,
        request_message: str,
        session_id: UUID | None,
        job_kind: str = "agent_turn",
        queued_message: str = "Queued for analysis.",
        request_context: dict[str, Any] | None = None,
    ) -> LiveJobRecord:
        now = datetime.now(UTC)
        record = LiveJobRecord(
            id=uuid4(),
            job_kind=job_kind,
            status="queued",
            created_at=now,
            updated_at=now,
            request_message=request_message,
            request_context=request_context or {},
            session_id=session_id,
        )
        record.events.append(LiveJobEvent(phase="queued", message=queued_message))
        self._jobs[record.id] = record
        self._prune()
        try:
            self._save_to_disk()
        except Exception:
            self._jobs.pop(record.id, None)
            raise
        return record

    def mark_running(
        self, job_id: UUID, *, task: asyncio.Task, message: str = "Started analysis."
    ) -> None:
        record = self._jobs.get(job_id)
        if record is None:
            return
        record.status = "running"
        record.updated_at = datetime.now(UTC)
        record._task = task
        record.events.append(LiveJobEvent(phase="running", message=message))
        record.retry_at = None
        self._save_to_disk()

    def defer(self, job_id: UUID, *, retry_at: datetime, message: str) -> None:
        record = self._jobs.get(job_id)
        if record is None or record.status in {"cancelled", "completed"}:
            return
        record.status = "deferred"
        record.retry_at = retry_at
        record.error = None
        record.updated_at = datetime.now(UTC)
        record.events.append(
            LiveJobEvent(
                phase="deferred",
                message=message,
                detail={"retry_at": retry_at.isoformat()},
            )
        )
        self._save_to_disk()

    def cancel_job(self, job_id: UUID) -> bool:
        record = self._jobs.get(job_id)
        if record is None:
            return False
        if record.status in {"queued", "running", "deferred"}:
            if record._task:
                record._task.cancel()
            record.status = "cancelled"
            record.retry_at = None
            record.updated_at = datetime.now(UTC)
            record.events.append(
                LiveJobEvent(phase="cancelled", message="Job cancelled by user.")
            )
            self._save_to_disk()
            return True
        return False

    def add_event(
        self,
        job_id: UUID,
        *,
        phase: str,
        message: str,
        detail: dict[str, Any] | None = None,
    ) -> None:
        record = self._jobs.get(job_id)
        if record is None:
            return
        record.updated_at = datetime.now(UTC)
        record.events.append(LiveJobEvent(phase=phase, message=message, detail=detail))

    def complete(
        self,
        job_id: UUID,
        *,
        result: dict[str, Any],
        message: str = "Analysis finished.",
    ) -> None:
        record = self._jobs.get(job_id)
        if record is None or record.status == "cancelled":
            return
        record.status = "completed"
        record.retry_at = None
        record.updated_at = datetime.now(UTC)
        record.result = result
        record.events.append(LiveJobEvent(phase="completed", message=message))
        self._save_to_disk()

    def fail(self, job_id: UUID, *, error: str) -> None:
        record = self._jobs.get(job_id)
        if record is None or record.status == "cancelled":
            return
        record.status = "error"
        record.retry_at = None
        record.updated_at = datetime.now(UTC)
        record.error = error
        record.events.append(LiveJobEvent(phase="error", message=error))
        self._save_to_disk()

    def get(self, job_id: UUID) -> LiveJobRecord | None:
        return self._jobs.get(job_id)

    def list_jobs(
        self,
        *,
        status: str | None = None,
        job_kind: str | None = None,
        limit: int | None = 30,
    ) -> list[LiveJobRecord]:
        self._prune()
        records = sorted(
            self._jobs.values(), key=lambda record: record.updated_at, reverse=True
        )
        if job_kind:
            records = [record for record in records if record.job_kind == job_kind]
        if status == "active":
            records = [
                record
                for record in records
                if record.status in {"queued", "running", "deferred"}
            ]
        elif status:
            records = [record for record in records if record.status == status]
        return records if limit is None else records[: max(1, min(limit, 100))]

    def _prune(self) -> None:
        cutoff = datetime.now(UTC) - timedelta(hours=12)
        stale = [
            job_id
            for job_id, record in self._jobs.items()
            if record.updated_at < cutoff
            and record.status in {"completed", "error", "cancelled"}
        ]
        for job_id in stale:
            self._jobs.pop(job_id, None)

    def _recover_interrupted_jobs(self) -> bool:
        recovered = False
        now = datetime.now(UTC)
        for record in self._jobs.values():
            if record.status not in {"queued", "running", "deferred"}:
                continue
            recovered = True
            if (
                record.job_kind == "agent_turn"
                and record.request_context.get("resume_payload")
                and ChatTurnState.can_resume(record.request_context)
            ):
                record.status = "deferred"
                record.retry_at = record.retry_at or now
                record.events.append(
                    LiveJobEvent(
                        phase="deferred",
                        message="Resuming the saved question from completed stages after restart.",
                        created_at=now,
                    )
                )
                continue
            record.status = "error"
            record.updated_at = now
            record.error = (
                "State updates may have started; automatic replay is disabled. Review recorded activity before retrying."
                if record.request_context.get("resume_payload")
                else "Job interrupted by backend restart."
            )
            record.events.append(
                LiveJobEvent(
                    phase="error",
                    message=record.error,
                    created_at=now,
                )
            )
        return recovered

    async def shutdown(self) -> None:
        tasks = [
            record._task
            for record in self._jobs.values()
            if record._task and not record._task.done()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
