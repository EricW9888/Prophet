from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4


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
        import json

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
            with open(self._storage_path, "w") as f:
                json.dump(serializable, f)
        except Exception:
            pass

    def _load_from_disk(self) -> None:
        import json

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
    ) -> LiveJobRecord:
        now = datetime.now(UTC)
        record = LiveJobRecord(
            id=uuid4(),
            job_kind=job_kind,
            status="queued",
            created_at=now,
            updated_at=now,
            request_message=request_message,
            session_id=session_id,
        )
        record.events.append(LiveJobEvent(phase="queued", message=queued_message))
        self._jobs[record.id] = record
        self._prune()
        self._save_to_disk()
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

    def cancel_job(self, job_id: UUID) -> bool:
        record = self._jobs.get(job_id)
        if record is None:
            return False
        if record.status == "running" and record._task:
            record._task.cancel()
            record.status = "cancelled"
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
        if record is None:
            return
        record.status = "completed"
        record.updated_at = datetime.now(UTC)
        record.result = result
        record.events.append(LiveJobEvent(phase="completed", message=message))
        self._save_to_disk()

    def fail(self, job_id: UUID, *, error: str) -> None:
        record = self._jobs.get(job_id)
        if record is None:
            return
        record.status = "error"
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
        limit: int = 30,
    ) -> list[LiveJobRecord]:
        self._prune()
        records = sorted(
            self._jobs.values(), key=lambda record: record.updated_at, reverse=True
        )
        if job_kind:
            records = [record for record in records if record.job_kind == job_kind]
        if status == "active":
            records = [
                record for record in records if record.status in {"queued", "running"}
            ]
        elif status:
            records = [record for record in records if record.status == status]
        return records[: max(1, min(limit, 100))]

    def _prune(self) -> None:
        cutoff = datetime.now(UTC) - timedelta(hours=12)
        stale = [
            job_id
            for job_id, record in self._jobs.items()
            if record.updated_at < cutoff and record.status in {"completed", "error"}
        ]
        for job_id in stale:
            self._jobs.pop(job_id, None)

    def _recover_interrupted_jobs(self) -> bool:
        recovered = False
        now = datetime.now(UTC)
        for record in self._jobs.values():
            if record.status not in {"queued", "running"}:
                continue
            recovered = True
            record.status = "error"
            record.updated_at = now
            record.error = "Job interrupted by backend restart."
            record.events.append(
                LiveJobEvent(
                    phase="error",
                    message="Job interrupted by backend restart.",
                    created_at=now,
                )
            )
        return recovered
