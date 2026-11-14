"""Completed stages for the existing single-instance chat job tracker."""

from copy import deepcopy
from datetime import UTC, datetime
from decimal import Decimal
from inspect import isawaitable
from time import perf_counter

from fastapi.encoders import jsonable_encoder


class ChatTurnState:
    def __init__(self, data=None, persist=None):
        self.enabled = data is not None and persist is not None
        self.data = data if data is not None else {}
        self.persist = persist
        if self.enabled:
            if self.data.get("version") != 1:
                raise ValueError(
                    "The saved analysis checkpoint version is unsupported."
                )
            if not isinstance(self.data.get("stages"), dict):
                raise ValueError("The saved analysis checkpoints are invalid.")

    @staticmethod
    def initial():
        return {"version": 1, "stages": {}}

    @staticmethod
    def can_resume(context):
        data = context.get("turn_state")
        if data is None:
            # Legacy resumable records were limited to file questions.
            request = context.get("resume_payload") or {}
            return bool(request.get("attachments") or request.get("attachment_ids"))
        if (
            not isinstance(data, dict)
            or data.get("version") != 1
            or not isinstance(data.get("stages"), dict)
        ):
            return False
        return not data.get("replay_blocked") or "response" in data.get("stages", {})

    def block_replay(self):
        if self.enabled and not self.data.get("replay_blocked"):
            self.data["replay_blocked"] = True
            self.persist()

    async def run(self, name, operation, *, session=None, encode=None, decode=None):
        stages = self.data.setdefault("stages", {})
        if self.enabled and name in stages:
            value = deepcopy(stages[name]["value"])
            value = decode(value) if decode else value
            return await value if isawaitable(value) else value
        started = perf_counter()
        value = await operation()
        if self.enabled:
            encoded = jsonable_encoder(
                encode(value) if encode else value, custom_encoder={Decimal: str}
            )
            # Durable database references must precede the file checkpoint.
            if session is not None:
                await session.commit()
            stages[name] = {
                "value": encoded,
                "completed_at": datetime.now(UTC).isoformat(),
                "duration_ms": round((perf_counter() - started) * 1000),
            }
            self.persist()
        return value
