from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from investos.core.llm import (
    _call_nvidia_json,
    _candidate_json_recovery_providers,
    _enforce_json_collection_bounds,
    _structured_response_format,
    _validate_json_response,
    call_llm_json,
)
from investos.core.providers import (
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
    ) == {"chat_template_kwargs": {"enable_thinking": False}}
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
    assert payload["chat_template_kwargs"] == {"enable_thinking": False}
    assert payload["max_tokens"] > 4096


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
