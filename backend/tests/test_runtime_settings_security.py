from __future__ import annotations

import json
import stat

from investos.config import settings
from investos.services.runtime_settings import RuntimeSettings, RuntimeSettingsStore


def test_runtime_secrets_are_atomic_private_and_redacted(monkeypatch, tmp_path):
    runtime_path = tmp_path / "runtime_settings.json"
    monkeypatch.setattr(settings, "RUNTIME_SETTINGS_PATH", str(runtime_path))
    runtime = RuntimeSettings()
    llm_value = "test-key-placeholder"
    research_value = "test-research-key-placeholder"
    mail_value = "test-password-placeholder"
    runtime.llm.api_key = llm_value
    runtime.research.api_key = research_value
    runtime.gmail.password = mail_value

    RuntimeSettingsStore.save(runtime)

    secrets_path = runtime_path.with_suffix(".json.secrets")
    public_text = runtime_path.read_text(encoding="utf-8")
    secret_payload = json.loads(secrets_path.read_text(encoding="utf-8"))
    assert llm_value not in public_text
    assert research_value not in public_text
    assert mail_value not in public_text
    assert secret_payload["llm_api_key"] == llm_value
    assert secret_payload["research_api_key"] == research_value
    assert secret_payload["gmail_password"] == mail_value
    assert stat.S_IMODE(runtime_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(secrets_path.stat().st_mode) == 0o600
    assert not list(tmp_path.glob(".runtime_settings.json.*"))

    reloaded = RuntimeSettingsStore.load()
    assert reloaded.llm.api_key == llm_value
    assert reloaded.research.api_key == research_value
    assert reloaded.gmail.password == mail_value
