"""An opportunity scan tells the evaluator which companies are its candidates."""

import json

from investos.services import shadow as shadow_module
from investos.services.shadow import ShadowService


async def _prompt_for(monkeypatch, **kwargs):
    seen = {}

    async def fake_llm(*, system_prompt, user_prompt, schema, **_):
        seen["prompt"] = json.loads(user_prompt)
        return {}

    monkeypatch.setattr(shadow_module, "call_llm_json", fake_llm)
    context = {
        "captured_at": "2026-11-26T00:00:00+00:00",
        "portfolio": {},
        "evidence_registry": [],
    }
    await ShadowService(None).evaluate_discovery_context(context, **kwargs)
    return seen["prompt"]


async def test_a_scan_names_its_candidates_and_keeps_holdings_as_context(monkeypatch):
    prompt = await _prompt_for(monkeypatch, focus_tickers=["syna", " SYNB ", "SYNA"])

    assert prompt["focus_tickers"] == ["SYNA", "SYNB"]
    assert "only among those companies" in prompt["instruction"]
    assert "not candidates" in prompt["instruction"]


async def test_the_shadow_lab_prompt_is_unchanged_without_a_focus(monkeypatch):
    prompt = await _prompt_for(monkeypatch)

    assert "focus_tickers" not in prompt
    assert prompt["instruction"] == (
        "Identify at most one paper-trading opportunity or risk-control experiment. "
        "Use only information whose evidence_ref is present in this packet."
    )
