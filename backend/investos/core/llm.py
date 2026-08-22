import asyncio
import json
import random
import re
import shutil
import tempfile
import time
from datetime import datetime
from functools import wraps
from pathlib import Path
from typing import Any, Awaitable, Callable, TypeVar

import fastjsonschema
import httpx

from investos.config import settings
from investos.core.providers import (
    llm_provider_capability,
    llm_recovery_provider_ids,
    llm_structured_request_options,
    normalize_llm_provider,
)

REPO_ROOT = Path(__file__).resolve().parents[3]

T = TypeVar("T")


class AsyncRateLimiter:
    """Process-wide token bucket shared by every hosted-LLM caller.

    Two independent bounds:
    - pacing: at most `rate_per_minute` acquisitions, evenly spaced
    - concurrency: at most `max_concurrency` requests in flight at once

    This is the single throttle that keeps the manual Gmail backfill and the
    dozen background automation jobs from collectively blowing past the
    provider's per-minute quota (the cause of the 429 storms).
    """

    def __init__(self, rate_per_minute: int, max_concurrency: int) -> None:
        self._interval = (
            60.0 / rate_per_minute if rate_per_minute and rate_per_minute > 0 else 0.0
        )
        self._lock = asyncio.Lock()
        self._sem = asyncio.Semaphore(max(1, max_concurrency))
        self._next_allowed = 0.0

    async def acquire(self) -> None:
        await self._sem.acquire()
        try:
            async with self._lock:
                loop = asyncio.get_running_loop()
                now = loop.time()
                wait = self._next_allowed - now
                if wait > 0:
                    await asyncio.sleep(wait)
                    now = loop.time()
                self._next_allowed = max(now, self._next_allowed) + self._interval
        except BaseException:
            self._sem.release()
            raise

    def release(self) -> None:
        self._sem.release()

    async def __aenter__(self) -> "AsyncRateLimiter":
        await self.acquire()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        self.release()


class LLMProviderCooldownError(RuntimeError):
    """Raised when a hosted provider is temporarily held after rate limiting."""


class CodexCLIFailedError(RuntimeError):
    """Compact Codex CLI failure that avoids dumping prompts or raw stderr."""

    def __init__(self, *, exit_code: int, stderr_text: str):
        self.exit_code = exit_code
        self.stderr_summary = _summarize_process_stderr(stderr_text)
        super().__init__(
            f"Codex CLI failed with exit code {exit_code}: {self.stderr_summary}"
        )


_rate_limiter: "AsyncRateLimiter | None" = None
_provider_cooldowns: dict[str, float] = {}


def _provider_label_for_call(func_name: str, kwargs: dict[str, Any]) -> str:
    if (
        "nvidia" in func_name.lower()
        or "nvidia" in str(kwargs.get("base_url", "")).lower()
    ):
        return "nvidia_nim"
    return func_name


def _cooldown_remaining(provider: str) -> float:
    until = _provider_cooldowns.get(provider, 0.0)
    remaining = until - time.monotonic()
    if remaining <= 0:
        _provider_cooldowns.pop(provider, None)
        return 0.0
    return remaining


def _raise_if_provider_cooling_down(provider: str) -> None:
    remaining = _cooldown_remaining(provider)
    if remaining > 0:
        raise LLMProviderCooldownError(
            f"{provider} is cooling down for {remaining:.0f}s after provider rate limiting."
        )


def _open_provider_cooldown(provider: str, seconds: float) -> None:
    duration = max(30.0, min(seconds, 600.0))
    _provider_cooldowns[provider] = max(
        _provider_cooldowns.get(provider, 0.0),
        time.monotonic() + duration,
    )


def _get_rate_limiter() -> AsyncRateLimiter:
    """Lazily build the singleton so it binds to the running event loop."""
    global _rate_limiter
    if _rate_limiter is None:
        _rate_limiter = AsyncRateLimiter(
            rate_per_minute=settings.LLM_RATE_LIMIT_PER_MINUTE,
            max_concurrency=settings.LLM_MAX_CONCURRENCY,
        )
    return _rate_limiter


def _summarize_process_stderr(stderr_text: str, *, max_chars: int = 500) -> str:
    lines = [
        line.strip() for line in str(stderr_text or "").splitlines() if line.strip()
    ]
    if not lines:
        return "no stderr"
    priority_lines = [
        line
        for line in lines
        if (
            line.startswith("ERROR:")
            or "usage limit" in line.lower()
            or "invalid_json_schema" in line.lower()
            or "rate limit" in line.lower()
        )
    ]
    if not priority_lines:
        return f"stderr_lines={len(lines)} stderr_chars={len(stderr_text)}"
    summary = " | ".join(priority_lines[:3])
    return summary[:max_chars]


def compact_exception_message(exc: BaseException) -> str:
    message = str(exc).strip()
    if message:
        return f"{type(exc).__name__}: {message}"
    return type(exc).__name__


def _retry_after_seconds(exc: Exception) -> float | None:
    """Parse a Retry-After header (seconds form) from a 429/503 response."""
    if not isinstance(exc, httpx.HTTPStatusError):
        return None
    return _retry_after_seconds_from_headers(exc.response.headers)


def _retry_after_seconds_from_headers(headers: httpx.Headers) -> float | None:
    raw = headers.get("Retry-After")
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return None


def retry_with_backoff(
    max_retries: int = 3, base_delay: float = 1.0, max_delay: float = 10.0
):
    """
    Decorator to retry async functions with jittered exponential back-off.
    Retries on 429 (Rate Limit) and 5xx (Server Errors). Honors Retry-After.
    """

    def decorator(func: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        @wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> T:
            last_exc = Exception("Unknown failure in retry wrapper")
            provider_label = _provider_label_for_call(func.__name__, kwargs)
            _raise_if_provider_cooling_down(provider_label)
            timeout_hint = kwargs.get("timeout_seconds")
            try:
                timeout_value = (
                    float(timeout_hint) if timeout_hint is not None else None
                )
            except (TypeError, ValueError):
                timeout_value = None
            if timeout_value is not None and timeout_value <= 5:
                effective_max_retries = 0
            elif timeout_value is not None and timeout_value <= 10:
                effective_max_retries = min(max_retries, 1)
            else:
                effective_max_retries = max_retries
            for attempt in range(effective_max_retries + 1):
                try:
                    return await func(*args, **kwargs)
                except (httpx.HTTPStatusError, httpx.RequestError) as exc:
                    last_exc = exc
                    status_code = None
                    if isinstance(exc, httpx.HTTPStatusError):
                        status_code = exc.response.status_code

                    if status_code == 429:
                        cooldown_seconds = _retry_after_seconds(exc) or 300.0
                        _open_provider_cooldown(provider_label, cooldown_seconds)
                        import logging

                        logging.getLogger(__name__).warning(
                            f"{provider_label} returned 429; opening LLM cooldown for {cooldown_seconds:.0f}s."
                        )
                        raise

                    # Only retry on 5xx or general request errors (timeouts, connection issues)
                    should_retry = isinstance(exc, httpx.RequestError) or (
                        status_code and 500 <= status_code < 600
                    )

                    if not should_retry or attempt == effective_max_retries:
                        if (
                            should_retry
                            and effective_max_retries > 0
                            and provider_label == "nvidia_nim"
                            and status_code is None
                        ):
                            _open_provider_cooldown(provider_label, 60.0)
                            import logging

                            logging.getLogger(__name__).warning(
                                "%s request failures exhausted retries; opening a short provider cooldown.",
                                provider_label,
                            )
                        if isinstance(exc, httpx.HTTPStatusError):
                            import logging

                            logging.getLogger(__name__).error(
                                f"Permanent LLM failure (status={status_code}) after {attempt} retries: {exc.response.text}"
                            )
                        raise

                    retry_after = _retry_after_seconds(exc)
                    if retry_after is not None:
                        delay = min(retry_after, 60.0)
                    else:
                        delay = (
                            min(
                                base_delay * (3**attempt) + random.uniform(0, 1.0), 60.0
                            )
                            if status_code == 429
                            else min(
                                base_delay * (2**attempt) + random.uniform(0, 0.5),
                                max_delay,
                            )
                        )
                    import logging

                    msg = (
                        f"LLM request failed (status={status_code or 'conn_err'}). "
                        f"Retrying in {delay:.2f}s... (Attempt {attempt + 1}/{effective_max_retries})"
                    )
                    logging.getLogger(__name__).warning(msg)

                    # Also log to backfill_status.log if it exists
                    try:
                        log_path = REPO_ROOT / "data" / "backfill_status.log"
                        if log_path.parent.exists():
                            with open(log_path, "a") as f:
                                f.write(f"[{datetime.now().isoformat()}] {msg}\n")
                    except Exception:
                        pass

                    await asyncio.sleep(delay)
            raise last_exc

        return wrapper

    return decorator


def _extract_json_object(raw_text: str, *, provider_name: str) -> dict[str, Any]:
    text = _normalize_llm_text(raw_text)
    if not text:
        raise ValueError(f"Empty response from {provider_name}")

    for candidate in _json_candidates(text):
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    raise ValueError(f"No valid JSON object found in response from {provider_name}")


def _validate_json_response(
    payload: dict[str, Any],
    *,
    schema: dict[str, Any],
    provider_name: str,
) -> dict[str, Any]:
    payload = _enforce_json_collection_bounds(payload, schema)
    try:
        fastjsonschema.validate(schema, payload)
    except fastjsonschema.JsonSchemaException as exc:
        raise ValueError(
            f"{provider_name} JSON did not match the requested schema: {exc.message}"
        ) from exc
    return payload


def _enforce_json_collection_bounds(value: Any, schema: dict[str, Any]) -> Any:
    """Project output onto deterministic collection and property schema bounds."""

    schema_type = schema.get("type")
    if schema_type == "object" and isinstance(value, dict):
        properties = schema.get("properties") or {}
        items = value.items()
        if schema.get("additionalProperties") is False:
            items = ((key, item) for key, item in items if key in properties)
        normalized = {
            key: _enforce_json_collection_bounds(item, properties.get(key, {}))
            for key, item in items
        }
        for key in schema.get("required") or []:
            property_type = (properties.get(key) or {}).get("type")
            if (
                key not in normalized
                and isinstance(property_type, list)
                and "null" in property_type
            ):
                normalized[key] = None
        return normalized
    if schema_type == "array" and isinstance(value, list):
        max_items = schema.get("maxItems")
        bounded = value[:max_items] if isinstance(max_items, int) else value
        item_schema = schema.get("items") or {}
        return [_enforce_json_collection_bounds(item, item_schema) for item in bounded]
    return value


def _structured_response_format(
    provider: str, schema: dict[str, Any]
) -> dict[str, Any]:
    capability = llm_provider_capability(provider)
    mode = capability.structured_output_mode if capability else "json_schema"
    if mode == "json_object":
        return {"type": "json_object"}
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "investos_response",
            "schema": schema,
            "strict": False,
        },
    }


def _normalize_llm_text(raw_text: Any) -> str:
    if raw_text is None:
        return ""
    if isinstance(raw_text, str):
        return raw_text.strip()
    if isinstance(raw_text, list):
        parts = [_normalize_llm_text(item) for item in raw_text]
        return "\n".join(part for part in parts if part).strip()
    if isinstance(raw_text, dict):
        for key in ("text", "content", "output_text", "value"):
            if key in raw_text:
                return _normalize_llm_text(raw_text.get(key))
        try:
            return json.dumps(raw_text, ensure_ascii=False)
        except Exception as exc:
            import logging

            logging.getLogger(__name__).warning(
                "LLM response text normalization fell back: %s",
                compact_exception_message(exc),
            )
            return str(raw_text).strip()
    return str(raw_text).strip()


def _json_candidates(text: str) -> list[str]:
    candidates: list[str] = []
    stripped = text.strip()
    if stripped:
        candidates.append(stripped)

    fenced_blocks = re.findall(
        r"```(?:json)?\s*(.*?)```", text, flags=re.IGNORECASE | re.DOTALL
    )
    candidates.extend(block.strip() for block in fenced_blocks if block.strip())

    candidates.extend(_balanced_json_objects(text))

    # Preserve order while removing duplicates.
    unique: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        normalized = candidate.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        unique.append(normalized)
    return unique


def _balanced_json_objects(text: str) -> list[str]:
    results: list[str] = []
    start_index: int | None = None
    depth = 0
    in_string = False
    escape = False

    for index, char in enumerate(text):
        if start_index is None:
            if char == "{":
                start_index = index
                depth = 1
                in_string = False
                escape = False
            continue

        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                results.append(text[start_index : index + 1].strip())
                start_index = None

    return results


async def call_llm_json(
    system_prompt: str,
    user_prompt: str,
    schema: dict[str, Any],
    model: str | None = None,
    timeout_seconds: int | None = None,
    provider_override: str | None = None,
    on_chunk: Callable[[str], Awaitable[None]] | None = None,
) -> dict[str, Any]:
    provider = normalize_llm_provider(provider_override or _runtime_llm_provider())
    capability = _assert_llm_provider_allowed(provider)
    timeout = (
        timeout_seconds
        or settings.LLM_TIMEOUT_SECONDS
        or settings.CODEX_TIMEOUT_SECONDS
    )
    try:
        if on_chunk and capability.supports_streaming:
            return await call_llm_json_streaming(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                schema=schema,
                on_chunk=on_chunk,
                model=model,
                timeout_seconds=timeout,
                provider_override=provider,
            )

        if provider == "codex_cli":
            return await _call_codex_json(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                schema=schema,
                model=model,
                timeout_seconds=timeout,
            )
        if provider == "ollama":
            return await _call_ollama_json(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                schema=schema,
                model=model,
                timeout_seconds=timeout,
            )
        if provider == "nvidia_nim":
            return await _call_nvidia_json(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                schema=schema,
                model=model,
                timeout_seconds=timeout,
            )
    except Exception as exc:
        import logging

        if isinstance(exc, LLMProviderCooldownError):
            logging.getLogger(__name__).warning(str(exc))
        elif isinstance(exc, CodexCLIFailedError):
            if any(
                marker in exc.stderr_summary.lower()
                for marker in (
                    "requires a newer version",
                    "usage limit",
                    "purchase more credits",
                    "upgrade to pro",
                )
            ):
                _open_provider_cooldown("codex_cli", 600.0)
            logging.getLogger(__name__).warning(str(exc))
        elif isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 429:
            logging.getLogger(__name__).warning(
                "Hosted LLM returned 429 Too Many Requests; cooldown is active."
            )
        elif isinstance(exc, httpx.RequestError):
            logging.getLogger(__name__).warning(
                "Hosted LLM request failed: %s%s",
                type(exc).__name__,
                f": {str(exc)}" if str(exc).strip() else "",
            )
        else:
            logging.getLogger(__name__).warning(
                "LLM JSON call failed: %s",
                compact_exception_message(exc),
            )
        raise
    raise RuntimeError(f"Unsupported LLM_PROVIDER={settings.LLM_PROVIDER}")


async def call_llm_json_streaming(
    system_prompt: str,
    user_prompt: str,
    schema: dict[str, Any],
    on_chunk: Callable[[str], Awaitable[None]],
    model: str | None = None,
    timeout_seconds: int | None = None,
    provider_override: str | None = None,
) -> dict[str, Any]:
    provider = normalize_llm_provider(provider_override or _runtime_llm_provider())
    capability = _assert_llm_provider_allowed(provider)
    timeout = (
        timeout_seconds
        or settings.LLM_TIMEOUT_SECONDS
        or settings.CODEX_TIMEOUT_SECONDS
    )
    if not capability.supports_streaming:
        raise RuntimeError(f"Streaming not supported for provider: {provider}")

    if provider == "nvidia_nim":
        return await _call_nvidia_json_streaming(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            schema=schema,
            on_chunk=on_chunk,
            model=model,
            timeout_seconds=timeout,
        )
    if provider == "ollama":
        return await _call_ollama_json_streaming(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            schema=schema,
            on_chunk=on_chunk,
            model=model,
            timeout_seconds=timeout,
        )

    raise RuntimeError(f"Streaming not supported for provider: {provider}")


def _runtime_llm_provider() -> str:
    try:
        from investos.services.runtime_settings import RuntimeSettingsStore

        provider = RuntimeSettingsStore.load().llm.provider
        if provider:
            return provider.strip().lower()
    except Exception as exc:
        import logging

        logging.getLogger(__name__).warning(
            "Runtime LLM provider settings fallback used: %s",
            compact_exception_message(exc),
        )
        pass
    return normalize_llm_provider(settings.LLM_PROVIDER or "nvidia_nim")


def _assert_llm_provider_allowed(provider: str):
    capability = llm_provider_capability(provider)
    if capability is None:
        raise RuntimeError(f"Unsupported LLM provider: {provider}")
    if capability.is_local and not settings.LLM_ALLOW_LOCAL_PROVIDER:
        raise RuntimeError("Local LLM providers are disabled by policy.")
    return capability


def _ollama_base_url() -> str:
    try:
        from investos.services.runtime_settings import RuntimeSettingsStore

        base_url = RuntimeSettingsStore.load().llm.hosted_base_url
    except Exception:
        base_url = None
    if not base_url or "nvidia" in str(base_url).lower():
        base_url = settings.OLLAMA_BASE_URL
    return str(base_url).replace("/v1", "").rstrip("/")


def _ollama_requested_model(model: str | None = None) -> str | None:
    try:
        from investos.services.runtime_settings import RuntimeSettingsStore

        runtime_model = RuntimeSettingsStore.load().llm.hosted_model
    except Exception:
        runtime_model = None
    requested_model = model or runtime_model or settings.OLLAMA_MODEL
    if requested_model and "kimi" in requested_model.lower():
        return None
    return requested_model


def _candidate_json_recovery_providers(
    primary_provider: str | None = None,
) -> list[str]:
    configured = {
        normalize_llm_provider(item)
        for item in str(settings.LLM_RECOVERY_PROVIDERS or "").split(",")
        if normalize_llm_provider(item)
    }
    return [
        provider
        for provider in llm_recovery_provider_ids(
            primary_provider or _runtime_llm_provider(),
            allow_local=settings.LLM_ALLOW_LOCAL_PROVIDER,
        )
        if provider in configured
    ]


async def available_llm_json_recovery_providers(
    primary_provider: str | None = None,
) -> list[str]:
    """Return ready structured-output providers other than the active one."""
    available: list[str] = []
    for provider in _candidate_json_recovery_providers(primary_provider):
        ready, _ = await verify_llm_provider_readiness(provider)
        if ready:
            available.append(provider)
    return available


async def verify_llm_readiness() -> tuple[bool, str]:
    return await verify_llm_provider_readiness(_runtime_llm_provider())


async def verify_llm_provider_readiness(provider: str) -> tuple[bool, str]:
    provider = normalize_llm_provider(provider)
    capability = llm_provider_capability(provider)
    if capability is None:
        return False, f"Unknown provider: {provider}"
    if capability.is_local and not settings.LLM_ALLOW_LOCAL_PROVIDER:
        return False, "Local LLM providers are disabled by policy."
    try:
        if provider == "codex_cli":
            remaining = _cooldown_remaining("codex_cli")
            if remaining > 0:
                return (
                    False,
                    f"Codex CLI recovery is cooling down for {remaining:.0f}s after a non-retryable failure.",
                )
            executable = shutil.which(settings.CODEX_BIN) or settings.CODEX_BIN
            if (
                not Path(executable).exists()
                and shutil.which(settings.CODEX_BIN) is None
            ):
                return False, f"Codex CLI executable not found: {settings.CODEX_BIN}"
            cmd = [executable, "--help"]
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=5)
            if process.returncode == 0:
                return True, "Codex CLI is ready."
            return False, (
                f"Codex CLI returned {process.returncode}: "
                f"{_summarize_process_stderr(stderr.decode(errors='ignore'))}"
            )

        if provider == "ollama":
            async with httpx.AsyncClient(
                base_url=_ollama_base_url(), timeout=5
            ) as client:
                response = await client.get("/api/tags")
                response.raise_for_status()
                payload = response.json()
                models = payload.get("models") or []
                if models:
                    requested = _ollama_requested_model()
                    if requested:
                        model_names = {
                            str(item.get("name"))
                            for item in models
                            if isinstance(item, dict) and item.get("name")
                        }
                        if requested not in model_names:
                            return (
                                True,
                                f"Ollama is ready with {len(models)} models; configured model {requested} is not installed, so a local model will be selected.",
                            )
                    return True, f"Ollama is ready with {len(models)} models."
                return False, "Ollama is running but no models are loaded."

        if provider == "nvidia_nim":
            remaining = _cooldown_remaining("nvidia_nim")
            if remaining > 0:
                return False, f"NVIDIA NIM is cooling down for {remaining:.0f}s."
            from investos.services.runtime_settings import RuntimeSettingsStore

            runtime = RuntimeSettingsStore.load().llm
            if not runtime.api_key:
                return False, "NVIDIA API key is missing."
            base_url = (runtime.hosted_base_url or settings.NVIDIA_BASE_URL).rstrip("/")
            async with httpx.AsyncClient(timeout=5) as client:
                response = await client.get(
                    f"{base_url}/models",
                    headers={"Authorization": f"Bearer {runtime.api_key}"},
                )
                if response.status_code == 200:
                    return True, "NVIDIA NIM is ready."
                return (
                    False,
                    f"NVIDIA NIM error {response.status_code}: {response.text[:100]}",
                )

        return False, f"Provider adapter is not implemented: {provider}"
    except Exception as exc:
        return False, f"Connection failed: {str(exc)}"


async def _call_codex_json(
    *,
    system_prompt: str,
    user_prompt: str,
    schema: dict[str, Any],
    model: str | None,
    timeout_seconds: int,
) -> dict[str, Any]:
    prompt = (
        "You are the LLM layer for Prophet. "
        "Return only valid JSON that matches the provided schema.\n\n"
        f"System instructions:\n{system_prompt}\n\n"
        f"User task:\n{user_prompt}\n"
    )
    cmd = [
        settings.CODEX_BIN,
        "exec",
        "--skip-git-repo-check",
        "--ephemeral",
        "--sandbox",
        settings.CODEX_SANDBOX,
    ]
    if model or settings.CODEX_MODEL:
        cmd.extend(["--model", model or settings.CODEX_MODEL])

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False
    ) as schema_file:
        json.dump(schema, schema_file)
        schema_path = schema_file.name
    with tempfile.NamedTemporaryFile(
        mode="w+", suffix=".json", delete=False
    ) as output_file:
        output_path = output_file.name

    cmd.extend(
        ["--output-schema", schema_path, "--output-last-message", output_path, "-"]
    )

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(REPO_ROOT),
    )
    stdout_bytes, stderr_bytes = await asyncio.wait_for(
        process.communicate(prompt.encode("utf-8")),
        timeout=timeout_seconds,
    )
    stdout_text = stdout_bytes.decode("utf-8", errors="ignore")
    stderr_text = stderr_bytes.decode("utf-8", errors="ignore")

    try:
        output_text = Path(output_path).read_text()
    except FileNotFoundError:
        output_text = stdout_text

    Path(schema_path).unlink(missing_ok=True)
    Path(output_path).unlink(missing_ok=True)

    if process.returncode != 0:
        raise CodexCLIFailedError(exit_code=process.returncode, stderr_text=stderr_text)

    try:
        return _extract_json_object(output_text, provider_name="Codex CLI")
    except Exception as exc:
        raise RuntimeError(
            "Codex CLI returned non-JSON output. "
            f"stdout_chars={len(stdout_text)} stderr={_summarize_process_stderr(stderr_text)}"
        ) from exc


async def _call_ollama_json(
    *,
    system_prompt: str,
    user_prompt: str,
    schema: dict[str, Any],
    model: str | None,
    timeout_seconds: int,
) -> dict[str, Any]:
    async with httpx.AsyncClient(
        base_url=_ollama_base_url(),
        timeout=timeout_seconds,
    ) as client:
        request_model = await _resolve_ollama_model(
            client, _ollama_requested_model(model)
        )
        response = await client.post(
            "/api/generate",
            json={
                "model": request_model,
                "system": (
                    "You are the LLM layer for Prophet. "
                    "Return only valid JSON that matches the provided schema.\n\n"
                    f"{system_prompt}"
                ),
                "prompt": user_prompt,
                "format": schema,
                "stream": False,
                "options": {
                    "temperature": 0.1,
                },
            },
        )
    response.raise_for_status()
    payload = response.json()
    raw_text = _normalize_llm_text(payload.get("response"))
    try:
        return _extract_json_object(raw_text, provider_name="Ollama")
    except Exception as exc:
        raise RuntimeError(
            f"Ollama returned non-JSON output for model {request_model}: {raw_text[:400]}"
        ) from exc


# NOTE: retries live at the network layer (_nvidia_chat_json_text). This
# function must NOT also be retry-decorated, or a single logical request
# fans out to outer*inner attempts and amplifies the 429 storm.
async def _call_nvidia_json(
    *,
    system_prompt: str,
    user_prompt: str,
    schema: dict[str, Any],
    model: str | None,
    timeout_seconds: int,
) -> dict[str, Any]:
    from investos.services.runtime_settings import RuntimeSettingsStore

    runtime = RuntimeSettingsStore.load().llm
    if not runtime.api_key:
        raise RuntimeError("NVIDIA NIM API key is not configured.")

    request_model = model or runtime.hosted_model or settings.NVIDIA_MODEL
    base_url = (runtime.hosted_base_url or settings.NVIDIA_BASE_URL).rstrip("/")
    response_format = _structured_response_format("nvidia_nim", schema)
    schema_instruction = (
        "\n\nReturn a JSON object matching this schema exactly:\n"
        f"{json.dumps(schema, ensure_ascii=False)}"
        if response_format.get("type") == "json_object"
        else ""
    )
    payload = {
        "model": request_model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are the LLM layer for Prophet. "
                    "Return only valid JSON that matches the provided schema.\n\n"
                    f"{system_prompt}{schema_instruction}"
                ),
            },
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.0,
        "top_p": 0.9,
        "max_tokens": settings.LLM_STRUCTURED_MAX_TOKENS,
        "stream": False,
        "response_format": response_format,
        **llm_structured_request_options("nvidia_nim", request_model),
    }

    async with httpx.AsyncClient(timeout=timeout_seconds) as client:
        raw_text = await _nvidia_chat_json_text(
            client=client,
            base_url=base_url,
            api_key=runtime.api_key,
            payload=payload,
            timeout_seconds=timeout_seconds,
        )
        try:
            return _validate_json_response(
                _extract_json_object(raw_text, provider_name="NVIDIA NIM"),
                schema=schema,
                provider_name="NVIDIA NIM",
            )
        except Exception as exc:
            import logging

            logging.getLogger(__name__).warning(
                "NVIDIA NIM returned malformed JSON; attempting repair: %s",
                compact_exception_message(exc),
            )

            repair_payload = {
                "model": request_model,
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "You repair malformed model output into strict JSON. "
                            "Return only a valid JSON object that matches the provided schema."
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            "The previous response did not parse as JSON.\n\n"
                            f"Schema:\n{json.dumps(schema, ensure_ascii=False)}\n\n"
                            f"Malformed output:\n{raw_text}"
                        ),
                    },
                ],
                "temperature": 0.0,
                "top_p": 0.9,
                "max_tokens": settings.LLM_STRUCTURED_MAX_TOKENS,
                "stream": False,
                "response_format": response_format,
                **llm_structured_request_options("nvidia_nim", request_model),
            }
            repaired_text = await _nvidia_chat_json_text(
                client=client,
                base_url=base_url,
                api_key=runtime.api_key,
                payload=repair_payload,
                timeout_seconds=timeout_seconds,
            )
            try:
                return _validate_json_response(
                    _extract_json_object(
                        repaired_text, provider_name="NVIDIA NIM repair"
                    ),
                    schema=schema,
                    provider_name="NVIDIA NIM repair",
                )
            except Exception as repair_exc:
                raise RuntimeError(
                    "NVIDIA NIM could not satisfy the structured output contract "
                    f"for model {request_model}. Initial response: "
                    f"{compact_exception_message(exc)}. Repair response: "
                    f"{compact_exception_message(repair_exc)}."
                ) from repair_exc


async def call_llm_tools(
    system_prompt: str,
    user_prompt: str,
    tools: list[dict[str, Any]],
    model: str | None = None,
    timeout_seconds: int | None = None,
) -> dict[str, Any]:
    provider = _runtime_llm_provider()
    timeout = (
        timeout_seconds
        or settings.LLM_TIMEOUT_SECONDS
        or settings.CODEX_TIMEOUT_SECONDS
    )
    if provider == "nvidia_nim":
        return await _call_nvidia_tools(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            tools=tools,
            model=model,
            timeout_seconds=timeout,
        )
    # Fallback to JSON mode if tools aren't supported by the provider
    # but the agent will have to handle this fallback gracefully.
    raise RuntimeError(
        f"Tool calling is not yet implemented for LLM_PROVIDER={provider}"
    )


@retry_with_backoff(max_retries=4, base_delay=1.0)
async def _call_nvidia_tools(
    *,
    system_prompt: str,
    user_prompt: str,
    tools: list[dict[str, Any]],
    model: str | None,
    timeout_seconds: int,
) -> dict[str, Any]:
    from investos.services.runtime_settings import RuntimeSettingsStore

    runtime = RuntimeSettingsStore.load().llm
    if not runtime.api_key:
        raise RuntimeError("NVIDIA NIM API key is not configured for tool calling.")

    request_model = model or runtime.hosted_model or settings.NVIDIA_MODEL
    base_url = (runtime.hosted_base_url or settings.NVIDIA_BASE_URL).rstrip("/")
    payload = {
        "model": request_model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "tools": tools,
        "tool_choice": "auto",
        "temperature": 0.1,
        "max_tokens": 4096,
        "stream": False,
    }

    async with httpx.AsyncClient(timeout=timeout_seconds) as client:
        async with _get_rate_limiter():
            response = await client.post(
                f"{base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {runtime.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
        response.raise_for_status()
        body = response.json()
        choices = body.get("choices") or []
        if not choices or not isinstance(choices[0], dict):
            raise RuntimeError("NVIDIA NIM returned no choices for tool call.")

        return choices[0].get("message") or {}


@retry_with_backoff(max_retries=4, base_delay=1.0)
async def _nvidia_chat_json_text(
    *,
    client: httpx.AsyncClient,
    base_url: str,
    api_key: str,
    payload: dict[str, Any],
    timeout_seconds: int | None = None,
) -> str:
    async with _get_rate_limiter():
        response = await client.post(
            f"{base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
    response.raise_for_status()
    body = response.json()
    choices = body.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        raise RuntimeError("NVIDIA NIM returned no choices.")
    message = choices[0].get("message") or {}
    return _normalize_llm_text(message.get("content"))


async def _call_nvidia_json_streaming(
    *,
    system_prompt: str,
    user_prompt: str,
    schema: dict[str, Any],
    on_chunk: Callable[[str], Awaitable[None]],
    model: str | None,
    timeout_seconds: int,
) -> dict[str, Any]:
    from investos.services.runtime_settings import RuntimeSettingsStore

    runtime = RuntimeSettingsStore.load().llm
    request_model = model or runtime.hosted_model or settings.NVIDIA_MODEL
    base_url = (runtime.hosted_base_url or settings.NVIDIA_BASE_URL).rstrip("/")
    response_format = _structured_response_format("nvidia_nim", schema)
    schema_instruction = (
        "\n\nReturn a JSON object matching this schema exactly:\n"
        f"{json.dumps(schema, ensure_ascii=False)}"
        if response_format.get("type") == "json_object"
        else ""
    )

    payload = {
        "model": request_model,
        "messages": [
            {
                "role": "system",
                "content": (
                    f"Return only valid JSON matching schema.\n\n"
                    f"{system_prompt}{schema_instruction}"
                ),
            },
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.0,
        "max_tokens": settings.LLM_STRUCTURED_MAX_TOKENS,
        "stream": True,
        "response_format": response_format,
        **llm_structured_request_options("nvidia_nim", request_model),
    }

    full_text = ""
    last_reasoning = ""
    _raise_if_provider_cooling_down("nvidia_nim")
    async with httpx.AsyncClient(timeout=timeout_seconds) as client:
        await _get_rate_limiter().acquire()
        try:
            async with client.stream(
                "POST",
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {runtime.api_key}"},
                json=payload,
            ) as response:
                if response.status_code == 429:
                    cooldown_seconds = (
                        _retry_after_seconds_from_headers(response.headers) or 300.0
                    )
                    _open_provider_cooldown("nvidia_nim", cooldown_seconds)
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    if line == "data: [DONE]":
                        break
                    try:
                        chunk = json.loads(line[6:])
                        delta = chunk["choices"][0]["delta"].get("content", "")
                        full_text += delta
                        # Robust regex for 'reasoning' field extraction from a growing JSON stream
                        # Handles escaped quotes and ignores other fields
                        reasoning_match = re.search(
                            r'"reasoning":\s*"((?:[^"\\]|\\.)*)', full_text
                        )
                        if reasoning_match:
                            current_reasoning = (
                                reasoning_match.group(1)
                                .replace('\\"', '"')
                                .replace("\\n", "\n")
                            )
                            if current_reasoning != last_reasoning:
                                await on_chunk(current_reasoning)
                                last_reasoning = current_reasoning
                    except Exception:
                        continue
        finally:
            _get_rate_limiter().release()
    return _validate_json_response(
        _extract_json_object(full_text, provider_name="NVIDIA NIM Streaming"),
        schema=schema,
        provider_name="NVIDIA NIM Streaming",
    )


async def _call_ollama_json_streaming(
    *,
    system_prompt: str,
    user_prompt: str,
    schema: dict[str, Any],
    on_chunk: Callable[[str], Awaitable[None]],
    model: str | None,
    timeout_seconds: int,
) -> dict[str, Any]:
    async with httpx.AsyncClient(
        base_url=_ollama_base_url(), timeout=timeout_seconds
    ) as client:
        request_model = await _resolve_ollama_model(
            client, _ollama_requested_model(model)
        )
        full_text = ""
        last_reasoning = ""
        async with client.stream(
            "POST",
            "/api/generate",
            json={
                "model": request_model,
                "system": f"Return valid JSON.\n\n{system_prompt}",
                "prompt": user_prompt,
                "format": schema,
                "stream": True,
            },
        ) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line:
                    continue
                try:
                    chunk = json.loads(line)
                    delta = chunk.get("response", "")
                    full_text += delta
                    reasoning_match = re.search(r'"reasoning":\s*"([^"]*)', full_text)
                    if reasoning_match:
                        current_reasoning = reasoning_match.group(1)
                        if current_reasoning != last_reasoning:
                            await on_chunk(current_reasoning)
                            last_reasoning = current_reasoning
                except Exception:
                    continue
    return _extract_json_object(full_text, provider_name="Ollama Streaming")


async def _resolve_ollama_model(
    client: httpx.AsyncClient,
    requested_model: str | None,
) -> str:
    response = await client.get("/api/tags")
    response.raise_for_status()
    payload = response.json()
    models = payload.get("models") or []
    if not isinstance(models, list) or not models:
        raise RuntimeError("Ollama is running but no models are available.")
    model_names = [
        str(item.get("name"))
        for item in models
        if isinstance(item, dict) and item.get("name")
    ]
    if requested_model and requested_model in model_names:
        return requested_model
    local_candidates = [name for name in model_names if not name.endswith(":cloud")]
    if local_candidates:
        return local_candidates[0]
    if model_names:
        return model_names[0]
    raise RuntimeError("Unable to determine an Ollama model to use.")
