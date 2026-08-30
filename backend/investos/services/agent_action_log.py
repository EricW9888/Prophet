from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from investos.config import settings


class AgentActionLogService:
    @staticmethod
    def _log_path() -> Path:
        path = Path(settings.STORAGE_DIR) / "_system" / "agent_actions.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    @classmethod
    def append(
        cls,
        *,
        source: str,
        action_type: str,
        status: str,
        summary: str,
        subject_id: str | None = None,
        subject_type: str | None = None,
        subject_name: str | None = None,
        session_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        entry = {
            "id": str(uuid4()),
            "timestamp": datetime.now(UTC).isoformat(),
            "source": source,
            "action_type": action_type,
            "status": status,
            "summary": summary,
            "subject_id": subject_id,
            "subject_type": subject_type,
            "subject_name": subject_name,
            "session_id": session_id,
            "metadata": metadata or {},
        }
        try:
            with cls._log_path().open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=True, default=str) + "\n")
        except Exception as exc:
            import logging

            logging.getLogger(__name__).exception("Masked failure caught")
            return

    @classmethod
    def recent(
        cls,
        limit: int = 40,
        *,
        source: str | None = None,
        action_type: str | None = None,
        status: str | None = None,
        session_id: str | None = None,
        include_internal: bool = False,
    ) -> list[dict[str, Any]]:
        path = cls._log_path()
        if not path.exists():
            return []
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except Exception as exc:
            import logging

            logging.getLogger(__name__).exception("Masked failure caught")
            return []
        entries: list[dict[str, Any]] = []
        for line in reversed(lines):
            raw = line.strip()
            if not raw:
                continue
            try:
                entry = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if source and str(entry.get("source") or "") != source:
                continue
            if action_type and str(entry.get("action_type") or "") != action_type:
                continue
            if status and str(entry.get("status") or "") != status:
                continue
            if session_id and str(entry.get("session_id") or "") != session_id:
                continue
            metadata = entry.get("metadata")
            if (
                not include_internal
                and isinstance(metadata, dict)
                and metadata.get("internal_state") is True
            ):
                continue
            entries.append(entry)
            if len(entries) >= limit:
                break
        return entries

    @classmethod
    def has_recent_question_attempt(
        cls,
        question_id: str,
        *,
        statuses: tuple[str, ...] = ("empty_result", "no_result", "research_failed"),
        within_seconds: int = 4 * 60 * 60,
        limit: int = 400,
    ) -> bool:
        cutoff = datetime.now(UTC) - timedelta(seconds=within_seconds)
        for entry in cls.recent(limit=limit, include_internal=True):
            metadata = entry.get("metadata")
            if not isinstance(metadata, dict):
                continue
            if str(metadata.get("question_id") or "") != question_id:
                continue
            if str(entry.get("status") or "") not in statuses:
                continue
            raw_timestamp = str(entry.get("timestamp") or "").strip()
            if not raw_timestamp:
                continue
            try:
                timestamp = datetime.fromisoformat(raw_timestamp)
            except ValueError:
                continue
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=UTC)
            if timestamp >= cutoff:
                return True
        return False

    @classmethod
    def has_recent_subject_attempt(
        cls,
        subject_id: str,
        *,
        subject_type: str | None = None,
        action_type: str | None = None,
        statuses: tuple[str, ...] | None = None,
        within_seconds: int = 24 * 60 * 60,
        limit: int = 400,
    ) -> bool:
        cutoff = datetime.now(UTC) - timedelta(seconds=within_seconds)
        for entry in cls.recent(limit=limit, include_internal=True):
            if action_type and str(entry.get("action_type") or "") != action_type:
                continue
            if statuses is not None and str(entry.get("status") or "") not in statuses:
                continue
            entry_subject_id = str(entry.get("subject_id") or "").strip()
            entry_subject_type = str(entry.get("subject_type") or "").strip()
            if not entry_subject_id:
                metadata = entry.get("metadata")
                if isinstance(metadata, dict):
                    entry_subject_id = str(metadata.get("subject_id") or "").strip()
                    entry_subject_type = str(metadata.get("subject_type") or "").strip()
            if entry_subject_id != subject_id:
                continue
            if subject_type is not None and entry_subject_type != subject_type:
                continue
            raw_timestamp = str(entry.get("timestamp") or "").strip()
            if not raw_timestamp:
                continue
            try:
                timestamp = datetime.fromisoformat(raw_timestamp)
            except ValueError:
                continue
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=UTC)
            if timestamp >= cutoff:
                return True
        return False

    @classmethod
    def has_recent_fingerprint(
        cls,
        fingerprint: str,
        *,
        action_type: str,
        within_seconds: int,
        limit: int = 400,
    ) -> bool:
        clean_fingerprint = str(fingerprint or "").strip()
        if not clean_fingerprint:
            return False
        cutoff = datetime.now(UTC) - timedelta(seconds=within_seconds)
        for entry in cls.recent(
            limit=limit,
            action_type=action_type,
            include_internal=True,
        ):
            metadata = entry.get("metadata")
            if not isinstance(metadata, dict):
                continue
            if str(metadata.get("decision_fingerprint") or "") != clean_fingerprint:
                continue
            raw_timestamp = str(entry.get("timestamp") or "").strip()
            if not raw_timestamp:
                continue
            try:
                timestamp = datetime.fromisoformat(raw_timestamp)
            except ValueError:
                continue
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=UTC)
            if timestamp >= cutoff:
                return True
        return False
