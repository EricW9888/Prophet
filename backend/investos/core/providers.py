from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class StructuredGenerationError(ValueError):
    """A contract failure with diagnostic metadata, never generated content."""

    def __init__(
        self,
        kind: str,
        *,
        finish_reason=None,
        content_chars=None,
        completion_tokens=None,
        reasoning_tokens=None,
        max_tokens=None,
    ):
        self.diagnostic = {"kind": kind}
        if finish_reason in {"stop", "length", "tool_calls", "content_filter"}:
            self.diagnostic["finish_reason"] = finish_reason
        for name, value in dict(
            content_chars=content_chars,
            completion_tokens=completion_tokens,
            reasoning_tokens=reasoning_tokens,
            max_tokens=max_tokens,
        ).items():
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                self.diagnostic[name] = value
        super().__init__(f"Structured generation failed: {self.diagnostic}")


@dataclass(frozen=True)
class LLMProviderCapability:
    provider: str
    label: str
    is_local: bool
    requires_api_key: bool
    accepts_model: bool
    accepts_base_url: bool
    supports_streaming: bool
    recovery_priority: int
    structured_output_mode: str = "json_schema"
    default_model: str = ""
    default_base_url: str = ""

    def public_dict(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "label": self.label,
            "is_local": self.is_local,
            "requires_api_key": self.requires_api_key,
            "accepts_model": self.accepts_model,
            "accepts_base_url": self.accepts_base_url,
            "supports_streaming": self.supports_streaming,
            "structured_output_mode": self.structured_output_mode,
            "default_model": self.default_model,
            "default_base_url": self.default_base_url,
        }


@dataclass(frozen=True)
class LLMModelCapability:
    """Request-level protocol features documented for a hosted model family."""

    provider: str
    model_prefixes: tuple[str, ...]
    structured_request_options: tuple[tuple[str, Any], ...] = ()

    def matches(self, provider: str, model: str | None) -> bool:
        normalized_model = str(model or "").strip().lower()
        return self.provider == normalize_llm_provider(provider) and any(
            normalized_model.startswith(prefix) for prefix in self.model_prefixes
        )


LLM_PROVIDER_CAPABILITIES: dict[str, LLMProviderCapability] = {
    "nvidia_nim": LLMProviderCapability(
        provider="nvidia_nim",
        label="NVIDIA NIM (Cloud)",
        is_local=False,
        requires_api_key=True,
        accepts_model=True,
        accepts_base_url=True,
        supports_streaming=True,
        recovery_priority=20,
        structured_output_mode="json_object",
        default_model="nvidia/nemotron-3-ultra-550b-a55b",
        default_base_url="https://integrate.api.nvidia.com/v1",
    ),
    "codex_cli": LLMProviderCapability(
        provider="codex_cli",
        label="Codex CLI",
        is_local=False,
        requires_api_key=False,
        accepts_model=False,
        accepts_base_url=False,
        supports_streaming=False,
        recovery_priority=10,
    ),
    "ollama": LLMProviderCapability(
        provider="ollama",
        label="Ollama (Local)",
        is_local=True,
        requires_api_key=False,
        accepts_model=True,
        accepts_base_url=True,
        supports_streaming=True,
        recovery_priority=30,
        default_base_url="http://localhost:11434",
    ),
}


LLM_MODEL_CAPABILITIES: tuple[LLMModelCapability, ...] = (
    LLMModelCapability(
        provider="ollama",
        model_prefixes=("qwen3:", "qwen3.5:"),
        structured_request_options=(("think", False),),
    ),
    LLMModelCapability(
        provider="nvidia_nim",
        model_prefixes=("nvidia/nemotron-3-ultra-",),
        structured_request_options=(
            (
                "chat_template_kwargs",
                {"enable_thinking": False, "force_nonempty_content": True},
            ),
        ),
    ),
    LLMModelCapability(
        provider="nvidia_nim",
        model_prefixes=("nvidia/nemotron-3-",),
        structured_request_options=(
            ("chat_template_kwargs", {"enable_thinking": False}),
        ),
    ),
)


def normalize_llm_provider(provider: str | None) -> str:
    return str(provider or "").strip().lower().replace("-", "_")


def llm_provider_capability(provider: str | None) -> LLMProviderCapability | None:
    return LLM_PROVIDER_CAPABILITIES.get(normalize_llm_provider(provider))


def llm_structured_request_options(
    provider: str | None, model: str | None
) -> dict[str, Any]:
    """Return only model-family options known to be accepted by the provider API."""

    for capability in LLM_MODEL_CAPABILITIES:
        if capability.matches(normalize_llm_provider(provider), model):
            return dict(capability.structured_request_options)
    return {}


def selectable_llm_providers(*, allow_local: bool) -> list[LLMProviderCapability]:
    return [
        capability
        for capability in LLM_PROVIDER_CAPABILITIES.values()
        if allow_local or not capability.is_local
    ]


def llm_recovery_provider_ids(
    primary_provider: str | None,
    *,
    allow_local: bool,
) -> list[str]:
    primary = normalize_llm_provider(primary_provider)
    capabilities = sorted(
        selectable_llm_providers(allow_local=allow_local),
        key=lambda capability: capability.recovery_priority,
    )
    return [
        capability.provider
        for capability in capabilities
        if capability.provider != primary
    ]
