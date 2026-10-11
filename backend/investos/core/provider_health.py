"""Durable single-instance provider circuit breaker, separate from investment state."""

import asyncio
import hashlib
import json
import threading
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from functools import wraps
from pathlib import Path
from typing import Literal
from uuid import uuid4

import httpx
from pydantic import AwareDatetime, BaseModel, Field

from investos.config import settings
from investos.core.providers import normalize_llm_provider

_call_depth = ContextVar("provider_call_depth", default=0)
_probe_call = ContextVar("provider_probe_call", default=False)


class LLMProviderCooldownError(RuntimeError):
    """Provider work is deferred; only the shared health probe can reopen it."""


class ProviderHealthRecord(BaseModel):
    provider: str
    state: Literal["unobserved", "healthy", "open", "probing"] = "unobserved"
    revision: int = Field(default=0, ge=0)
    failure_count: int = Field(default=0, ge=0)
    failure_code: str | None = None
    next_probe_at: AwareDatetime | None = None
    lease_until: AwareDatetime | None = None
    probe_token: str | None = None
    last_success_at: AwareDatetime | None = None


class ProviderHealthRegistry(BaseModel):
    records: dict[str, ProviderHealthRecord] = Field(default_factory=dict)


class ProviderHealthStore:
    # Prophet owns one primary process. The lock serializes its threads; an
    # atomic private file and persisted probe lease survive process restarts.
    _lock = threading.RLock()

    @staticmethod
    def _path():
        return Path(settings.RUNTIME_SETTINGS_PATH).with_suffix(".provider-health.json")

    @staticmethod
    def configuration(provider=None, model=None):
        from investos.services.runtime_settings import RuntimeSettingsStore

        runtime = RuntimeSettingsStore.load().llm
        provider = normalize_llm_provider(provider or runtime.provider)
        identity = [provider]
        if provider == "nvidia_nim":
            identity += [
                (runtime.hosted_base_url or settings.NVIDIA_BASE_URL).rstrip("/"),
                model or runtime.hosted_model or settings.NVIDIA_MODEL,
                hashlib.sha256((runtime.api_key or "").encode()).hexdigest(),
            ]
        elif provider == "codex_cli":
            identity += [settings.CODEX_BIN, settings.CODEX_MODEL]
        elif provider == "ollama":
            identity += [settings.OLLAMA_BASE_URL, model or settings.OLLAMA_MODEL]
        key = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
        return key, provider

    @classmethod
    def _read(cls):
        path = cls._path()
        if not path.exists():
            return ProviderHealthRegistry()
        try:
            return ProviderHealthRegistry.model_validate_json(
                path.read_text(encoding="utf-8")
            )
        except (OSError, ValueError) as exc:
            raise LLMProviderCooldownError(
                "Provider health storage is unavailable; model work is deferred."
            ) from exc

    @classmethod
    def _write(cls, registry):
        from investos.services.runtime_settings import RuntimeSettingsStore

        try:
            RuntimeSettingsStore._write_private_json(cls._path(), registry)
        except OSError as exc:
            raise LLMProviderCooldownError(
                "Provider health storage is unavailable; model work is deferred."
            ) from exc

    @classmethod
    def read(cls, provider=None, model=None):
        key, provider = cls.configuration(provider, model)
        with cls._lock:
            record = cls._read().records.get(
                key, ProviderHealthRecord(provider=provider)
            )
            return record.model_copy(deep=True)

    @classmethod
    def public_status(cls):
        from investos.services.runtime_settings import RuntimeSettingsStore

        try:
            record = cls.read()
            result = record.model_dump(mode="json", exclude={"revision", "probe_token"})
            runtime = RuntimeSettingsStore.load().llm
            if record.provider == "nvidia_nim" and not runtime.api_key:
                result["state"] = "waiting_for_config"
            return result
        except LLMProviderCooldownError:
            return {
                "state": "storage_unavailable",
                "failure_code": "health_storage_unavailable",
            }

    @classmethod
    def require_available(cls, provider=None, model=None):
        record = cls.read(provider, model)
        if record.state in {"open", "probing"}:
            raise LLMProviderCooldownError(
                f"{record.provider} work deferred ({record.failure_code or record.state}); "
                f"next shared health probe: {record.next_probe_at.isoformat() if record.next_probe_at else 'pending'}."
            )
        return record.revision

    @classmethod
    def succeeded(cls, key, provider, revision):
        with cls._lock:
            registry = cls._read()
            record = registry.records.setdefault(
                key, ProviderHealthRecord(provider=provider)
            )
            # An older in-flight success cannot clear a newer failure.
            if record.revision != revision or record.state in {"open", "probing"}:
                return
            record.state = "healthy"
            record.last_success_at = datetime.now(UTC)
            cls._write(registry)

    @classmethod
    def failed(cls, key, provider, exc, *, probe_token=None):
        with cls._lock:
            registry = cls._read()
            record = registry.records.setdefault(
                key, ProviderHealthRecord(provider=provider)
            )
            if probe_token is not None and record.probe_token != probe_token:
                return
            record.revision += 1
            record.failure_count += 1
            record.state = "open"
            record.probe_token = None
            record.lease_until = None
            record.failure_code = (
                f"http_{exc.response.status_code}"
                if isinstance(exc, httpx.HTTPStatusError)
                else (
                    "provider_timeout"
                    if isinstance(exc, (httpx.TimeoutException, TimeoutError))
                    else (
                        "network_error"
                        if isinstance(exc, httpx.RequestError)
                        else (
                            "provider_cooldown"
                            if isinstance(exc, LLMProviderCooldownError)
                            else (
                                "invalid_response"
                                if isinstance(exc, ValueError)
                                else "provider_error"
                            )
                        )
                    )
                )
            )
            delay = min(3600, 60 * 2 ** min(record.failure_count - 1, 6))
            if isinstance(exc, httpx.HTTPStatusError):
                try:
                    delay = max(
                        delay,
                        min(3600, float(exc.response.headers.get("Retry-After", "0"))),
                    )
                except ValueError:
                    pass
                if exc.response.status_code in {401, 403, 429}:
                    delay = max(delay, 300)
            record.next_probe_at = datetime.now(UTC) + timedelta(seconds=delay)
            cls._write(registry)

    @classmethod
    def claim_probe(cls):
        key, provider = cls.configuration()
        with cls._lock:
            registry = cls._read()
            record = registry.records.setdefault(
                key, ProviderHealthRecord(provider=provider)
            )
            now = datetime.now(UTC)
            if record.lease_until and record.lease_until > now:
                return None
            if record.next_probe_at and record.next_probe_at > now:
                return None
            if record.state == "healthy":
                return None
            record.state = "probing"
            record.revision += 1
            record.probe_token = uuid4().hex
            record.lease_until = now + timedelta(seconds=30)
            cls._write(registry)
            return key, provider, record.probe_token

    @classmethod
    def complete_probe(cls, key, token):
        with cls._lock:
            registry = cls._read()
            record = registry.records[key]
            if record.probe_token != token:
                return False
            record.state = "healthy"
            record.failure_count = 0
            record.failure_code = None
            record.next_probe_at = record.lease_until = None
            record.probe_token = None
            record.last_success_at = datetime.now(UTC)
            cls._write(registry)
            return True


def guarded_provider_call(function):
    @wraps(function)
    async def guarded(*args, **kwargs):
        if _probe_call.get() or _call_depth.get():
            return await function(*args, **kwargs)
        provider = kwargs.get("provider_override")
        model = kwargs.get("model")
        key, provider = ProviderHealthStore.configuration(provider, model)
        revision = ProviderHealthStore.require_available(provider, model)
        depth = _call_depth.set(1)
        try:
            result = await function(*args, **kwargs)
        except Exception as exc:
            ProviderHealthStore.failed(key, provider, exc)
            raise
        else:
            ProviderHealthStore.succeeded(key, provider, revision)
            return result
        finally:
            _call_depth.reset(depth)

    return guarded


async def probe_llm_provider():
    from investos.core.llm import call_llm_json

    status = ProviderHealthStore.public_status()
    if status["state"] in {"waiting_for_config", "storage_unavailable"}:
        return status
    claimed = ProviderHealthStore.claim_probe()
    if claimed is None:
        return ProviderHealthStore.public_status()
    key, provider, token = claimed
    probe = _probe_call.set(True)
    try:
        async with asyncio.timeout(20):
            result = await call_llm_json(
                system_prompt='This is a connectivity health probe. Return only {"ok":true}.',
                user_prompt="Confirm structured generation is available.",
                schema={
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {"ok": {"const": True}},
                    "required": ["ok"],
                },
                timeout_seconds=15,
                provider_override=provider,
            )
        if result != {"ok": True}:
            raise ValueError("Probe did not satisfy the generation contract")
        ProviderHealthStore.complete_probe(key, token)
    except Exception as exc:
        ProviderHealthStore.failed(key, provider, exc, probe_token=token)
    finally:
        _probe_call.reset(probe)
    return ProviderHealthStore.public_status()
