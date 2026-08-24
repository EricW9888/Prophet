from types import SimpleNamespace

import pytest

from investos.services.runtime_settings import (
    LLMRuntimeSettings,
    RuntimeSettings,
    RuntimeSettingsStore,
)
from investos.services.setup import SetupService


def _runtime(
    *, llm: SimpleNamespace | None = None, research: SimpleNamespace | None = None
):
    return SimpleNamespace(
        llm=llm
        or SimpleNamespace(
            provider="nvidia_nim",
            api_key_set=True,
            ready=True,
            status_message="LLM ready.",
        ),
        research=research
        or SimpleNamespace(
            provider="tavily",
            provider_order=["searxng", "tavily"],
            searxng_base_url="",
            api_key_set=True,
            ready=True,
            status_message="Research ready.",
        ),
    )


def test_setup_llm_step_surfaces_missing_hosted_key():
    step = SetupService._llm_provider_step(
        _runtime(
            llm=SimpleNamespace(
                provider="nvidia_nim",
                api_key_set=False,
                ready=False,
                status_message="LLM API key is missing.",
            )
        )
    )

    assert step.id == "llm_provider"
    assert step.status == "pending"
    assert step.status_label == "Needs setup"
    assert "API key" in step.detail
    assert "Paste" in step.hint


def test_setup_llm_step_does_not_imply_prophet_starts_local_providers():
    step = SetupService._llm_provider_step(
        _runtime(
            llm=SimpleNamespace(
                provider="ollama",
                api_key_set=False,
                ready=False,
                status_message="Connection refused.",
            )
        )
    )

    assert step.status == "in_progress"
    assert "will not start local providers automatically" in step.hint


def test_setup_llm_step_keeps_config_complete_during_provider_cooldown():
    step = SetupService._llm_provider_step(
        _runtime(
            llm=SimpleNamespace(
                provider="nvidia_nim",
                api_key_set=True,
                ready=False,
                status_message="NVIDIA NIM is cooling down for 12s.",
            )
        )
    )

    assert step.status == "complete"
    assert step.status_label == "Complete"
    assert "cooling down" in step.detail
    assert "resume" in step.hint


def test_setup_research_step_surfaces_missing_discovery_provider():
    step = SetupService._research_provider_step(
        _runtime(
            research=SimpleNamespace(
                provider="tavily",
                provider_order=["searxng", "tavily"],
                searxng_base_url="",
                api_key_set=False,
                ready=False,
                status_message="Configure a SearXNG endpoint or Tavily API key.",
            )
        )
    )

    assert step.id == "research_provider"
    assert step.status == "pending"
    assert step.status_label == "Needs setup"
    assert "No web discovery provider" in step.detail
    assert "SearXNG endpoint" in step.hint


def test_setup_research_step_allows_rate_limited_but_configured_provider():
    step = SetupService._research_provider_step(
        _runtime(
            research=SimpleNamespace(
                provider="tavily",
                provider_order=["searxng", "tavily"],
                searxng_base_url="",
                api_key_set=True,
                ready=True,
                status_message="Research connector is configured; Tavily usage checks are currently rate-limited.",
            )
        )
    )

    assert step.status == "complete"
    assert step.status_label == "Complete"
    assert "rate-limited" in step.detail


def test_runtime_validation_allows_partial_hosted_llm_setup_without_key():
    runtime = RuntimeSettings(
        llm=LLMRuntimeSettings(
            provider="nvidia_nim",
            hosted_base_url="https://integrate.api.nvidia.com/v1",
            hosted_model="nvidia/nemotron-3-ultra-550b-a55b",
            api_key=None,
        )
    )

    RuntimeSettingsStore._validate(runtime)


def test_runtime_validation_rejects_local_provider_without_policy_opt_in(monkeypatch):
    monkeypatch.setattr(
        "investos.services.runtime_settings.settings.LLM_ALLOW_LOCAL_PROVIDER",
        False,
    )
    runtime = RuntimeSettings(
        llm=LLMRuntimeSettings(
            provider="ollama",
            hosted_base_url="http://localhost:11434",
            hosted_model="local-model",
            api_key=None,
        )
    )

    with pytest.raises(ValueError, match="disabled by policy"):
        RuntimeSettingsStore._validate(runtime)


def test_configured_provider_status_does_not_require_network_probe():
    runtime = RuntimeSettings(
        llm=LLMRuntimeSettings(
            provider="nvidia_nim",
            hosted_base_url="https://integrate.api.nvidia.com/v1",
            hosted_model="nvidia/nemotron-3-ultra-550b-a55b",
            api_key="test-key",
        )
    )
    runtime.research.api_key = "test-key"

    llm_ready, llm_message, research_ready, research_message = (
        RuntimeSettingsStore._configured_provider_status(runtime)
    )

    assert llm_ready is True
    assert research_ready is True
    assert "Live availability" in llm_message
    assert research_message == "Research discovery order: Tavily."


def test_configured_provider_status_reports_missing_keys():
    runtime = RuntimeSettings(
        llm=LLMRuntimeSettings(
            provider="nvidia_nim",
            hosted_base_url="https://integrate.api.nvidia.com/v1",
            hosted_model="nvidia/nemotron-3-ultra-550b-a55b",
            api_key=None,
        )
    )
    runtime.research.api_key = None

    llm_ready, llm_message, research_ready, research_message = (
        RuntimeSettingsStore._configured_provider_status(runtime)
    )

    assert llm_ready is False
    assert research_ready is False
    assert llm_message == "LLM API key is missing."
    assert research_message == "Configure a SearXNG endpoint or Tavily API key."
