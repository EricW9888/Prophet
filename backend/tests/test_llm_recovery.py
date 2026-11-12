from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from investos.core.llm import (
    AsyncRateLimiter,
    _call_nvidia_json,
    _call_ollama_json,
    _candidate_json_recovery_providers,
    _enforce_json_collection_bounds,
    _nvidia_chat_json_text,
    _resolve_ollama_model,
    _structured_response_format,
    _validate_json_response,
    call_llm_json,
)
from investos.core.providers import (
    StructuredGenerationError,
    llm_structured_request_options,
    selectable_llm_providers,
)


def test_local_llm_is_not_an_implicit_recovery_provider(monkeypatch):
    monkeypatch.setattr("investos.core.llm.settings.LLM_ALLOW_LOCAL_PROVIDER", False)
    monkeypatch.setattr("investos.core.llm.settings.LLM_RECOVERY_PROVIDERS", "")

    providers = _candidate_json_recovery_providers("nvidia_nim")

    assert "ollama" not in providers
    assert "codex_cli" not in providers


def test_codex_recovery_requires_explicit_configuration(monkeypatch):
    monkeypatch.setattr("investos.core.llm.settings.LLM_ALLOW_LOCAL_PROVIDER", False)
    monkeypatch.setattr(
        "investos.core.llm.settings.LLM_RECOVERY_PROVIDERS", "codex-cli"
    )

    assert _candidate_json_recovery_providers("nvidia_nim") == ["codex_cli"]


def test_local_llm_recovery_requires_explicit_opt_in(monkeypatch):
    monkeypatch.setattr("investos.core.llm.settings.LLM_ALLOW_LOCAL_PROVIDER", True)
    monkeypatch.setattr("investos.core.llm.settings.LLM_RECOVERY_PROVIDERS", "ollama")

    providers = _candidate_json_recovery_providers("nvidia_nim")

    assert "ollama" in providers


def test_provider_capabilities_drive_local_visibility():
    default_ids = {
        capability.provider
        for capability in selectable_llm_providers(allow_local=False)
    }
    opted_in_ids = {
        capability.provider for capability in selectable_llm_providers(allow_local=True)
    }

    assert "ollama" not in default_ids
    assert "ollama" in opted_in_ids
    assert {"nvidia_nim", "codex_cli"}.issubset(default_ids)


def test_nvidia_capability_uses_json_mode_with_application_schema_validation():
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {"answer": {"type": "string"}},
        "required": ["answer"],
    }

    assert _structured_response_format("nvidia_nim", schema) == {"type": "json_object"}
    assert _validate_json_response(
        {"answer": "ok"}, schema=schema, provider_name="test"
    ) == {"answer": "ok"}
    with pytest.raises(ValueError, match="did not match"):
        _validate_json_response({}, schema=schema, provider_name="test")


def test_structured_collection_bounds_are_enforced_from_the_schema():
    schema = {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "maxItems": 2,
                "items": {
                    "type": "object",
                    "properties": {"value": {"type": "number"}},
                },
            }
        },
    }

    assert _enforce_json_collection_bounds(
        {"items": [{"value": 1}, {"value": 2}, {"value": 3}]}, schema
    ) == {"items": [{"value": 1}, {"value": 2}]}


def test_structured_output_projects_unknown_properties_before_validation():
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {"value": {"type": "number"}},
                    "required": ["value"],
                },
            }
        },
        "required": ["items"],
    }

    assert _validate_json_response(
        {"items": [{"value": 1, "unsupported": "discard"}], "extra": True},
        schema=schema,
        provider_name="test",
    ) == {"items": [{"value": 1}]}


def test_structured_output_marks_missing_nullable_fields_as_unknown():
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "label": {"type": "string"},
            "numeric_value": {"type": ["number", "null"]},
        },
        "required": ["label", "numeric_value"],
    }

    assert _validate_json_response(
        {"label": "Gross margin"}, schema=schema, provider_name="test"
    ) == {"label": "Gross margin", "numeric_value": None}
    with pytest.raises(ValueError, match="must contain"):
        _validate_json_response(
            {"numeric_value": 42}, schema=schema, provider_name="test"
        )


def test_nemotron_3_disables_reasoning_only_for_structured_requests():
    assert llm_structured_request_options(
        "nvidia_nim", "nvidia/nemotron-3-ultra-550b-a55b"
    ) == {
        "chat_template_kwargs": {
            "enable_thinking": False,
            "force_nonempty_content": True,
        }
    }
    assert llm_structured_request_options("nvidia_nim", "nvidia/other-model") == {}


@pytest.mark.asyncio
async def test_nvidia_structured_call_sends_documented_reasoning_control(monkeypatch):
    request = AsyncMock(return_value='{"answer":"ok"}')
    monkeypatch.setattr("investos.core.llm._nvidia_chat_json_text", request)
    monkeypatch.setattr(
        "investos.services.runtime_settings.RuntimeSettingsStore.load",
        lambda: SimpleNamespace(
            llm=SimpleNamespace(
                api_key="test-key",
                hosted_model="nvidia/nemotron-3-ultra-550b-a55b",
                hosted_base_url="https://example.invalid/v1",
            )
        ),
    )
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {"answer": {"type": "string"}},
        "required": ["answer"],
    }

    result = await _call_nvidia_json(
        system_prompt="Answer from supplied evidence.",
        user_prompt="Test.",
        schema=schema,
        model=None,
        timeout_seconds=5,
    )

    assert result == {"answer": "ok"}
    payload = request.await_args.kwargs["payload"]
    assert payload["response_format"] == {"type": "json_object"}
    assert payload["chat_template_kwargs"] == {
        "enable_thinking": False,
        "force_nonempty_content": True,
    }
    assert payload["max_tokens"] > 4096


@pytest.mark.parametrize(
    ("content", "finish", "kind"),
    [(None, "stop", "empty_content"), ('{"ok":true}', "length", "truncated_output")],
)
async def test_hosted_response_contract_rejects_empty_or_truncated_content_without_retries(
    monkeypatch, content, finish, kind
):
    calls = []

    def transport(request):
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": finish,
                        "message": {
                            "content": content,
                            "reasoning_content": "private hidden model trace",
                        },
                    }
                ],
                "usage": {
                    "completion_tokens": 64,
                    "completion_tokens_details": {"reasoning_tokens": 50},
                },
            },
        )

    monkeypatch.setattr(
        "investos.core.llm._get_rate_limiter", lambda: AsyncRateLimiter(0, 1)
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        with pytest.raises(StructuredGenerationError) as captured:
            await _nvidia_chat_json_text(
                client=client,
                base_url="https://example.test",
                api_key="private-key",
                payload={"max_tokens": 64},
                timeout_seconds=5,
            )
    assert len(calls) == 1
    assert captured.value.diagnostic["kind"] == kind
    assert captured.value.diagnostic["completion_tokens"] == 64
    assert "private" not in str(captured.value)


@pytest.mark.parametrize(
    ("requested", "names"),
    [
        ("missing:9b", ["other:9b"]),
        (None, ["remote:cloud"]),
        ("remote:cloud", ["remote:cloud"]),
        (None, ["one:9b", "two:9b"]),
    ],
)
async def test_local_model_resolution_never_silently_changes_model_or_selects_cloud(
    requested, names
):
    async with httpx.AsyncClient(
        base_url="http://localhost:11434",
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, json={"models": [{"name": name} for name in names]}
            )
        ),
    ) as client:
        with pytest.raises(RuntimeError):
            await _resolve_ollama_model(client, requested)


async def test_local_output_is_schema_validated_instead_of_accepting_json_only(
    monkeypatch,
):
    from investos.core import llm

    original_client = httpx.AsyncClient

    def transport(request):
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "fixture:9b"}]})
        return httpx.Response(
            200, json={"response": '{"wrong":"private response must not leak"}'}
        )

    monkeypatch.setattr(
        llm.httpx,
        "AsyncClient",
        lambda **kwargs: original_client(
            transport=httpx.MockTransport(transport), **kwargs
        ),
    )
    monkeypatch.setattr(llm, "_ollama_base_url", lambda: "http://localhost:11434")
    with pytest.raises(StructuredGenerationError) as captured:
        await _call_ollama_json(
            system_prompt="Extract only supported information",
            user_prompt="Synthetic packet",
            schema={
                "type": "object",
                "properties": {"answer": {"type": "string"}},
                "required": ["answer"],
            },
            model="fixture:9b",
            timeout_seconds=5,
        )
    assert captured.value.diagnostic["kind"] == "schema_mismatch"
    assert "private" not in str(captured.value)


async def test_local_stream_rejects_a_truncated_json_object_and_uses_model_protocol(
    monkeypatch,
):
    from investos.core import llm

    original_client = httpx.AsyncClient
    requests = []

    def transport(request):
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "qwen3.5:9b"}]})
        import json

        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            content=b'{"response":"{\\"answer\\":\\"ok\\"}","done":true,"done_reason":"length","eval_count":32}\n',
        )

    monkeypatch.setattr(
        llm.httpx,
        "AsyncClient",
        lambda **kwargs: original_client(
            transport=httpx.MockTransport(transport), **kwargs
        ),
    )
    monkeypatch.setattr(llm, "_ollama_base_url", lambda: "http://localhost:11434")
    with pytest.raises(StructuredGenerationError) as captured:
        await llm._call_ollama_json_streaming(
            system_prompt="Return supported fields",
            user_prompt="Synthetic",
            schema={
                "type": "object",
                "properties": {"answer": {"type": "string"}},
                "required": ["answer"],
            },
            model="qwen3.5:9b",
            timeout_seconds=5,
            on_chunk=AsyncMock(),
        )
    assert captured.value.diagnostic["kind"] == "truncated_output"
    assert requests[0]["think"] is False


@pytest.mark.asyncio
async def test_local_provider_is_blocked_at_the_call_boundary(monkeypatch):
    monkeypatch.setattr("investos.core.llm.settings.LLM_ALLOW_LOCAL_PROVIDER", False)

    with pytest.raises(RuntimeError, match="disabled by policy"):
        await call_llm_json(
            system_prompt="Return JSON.",
            user_prompt="Test.",
            schema={"type": "object"},
            provider_override="ollama",
        )


@pytest.fixture(autouse=True)
def isolated_provider_health(monkeypatch, tmp_path):
    from investos.core.provider_health import ProviderHealthStore

    monkeypatch.setattr(
        ProviderHealthStore,
        "_path",
        staticmethod(lambda: tmp_path / "provider-health.json"),
    )
