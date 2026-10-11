"""Synthetic language propagation and immutable versioned source derivatives."""

from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from investos.api.routes.integrations import router as integrations_router
from investos.config import settings
from investos.core.presentation_language import presentation_instruction
from investos.core.provider_health import ProviderHealthStore
from investos.models.evidence import RawEvidence, SourceItem
from investos.models.source import Source
from investos.models.translation import EvidenceTranslation
from investos.schemas.integrations import IntegrationSettingsUpdate
from investos.services.reasoning import ReasoningService
from investos.services.runtime_settings import RuntimeSettings, RuntimeSettingsStore
from investos.services.translation import TranslationService, protect_text, restore_text

ORIGINAL = "NVDA revenue was USD 12.50 on 2026-10-11; margin 45%. See [1] https://example.org/report."


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch, tmp_path):
    monkeypatch.setattr(
        settings, "RUNTIME_SETTINGS_PATH", str(tmp_path / "runtime.json")
    )
    monkeypatch.setattr(settings, "STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setattr(settings, "NVIDIA_API_KEY", "")
    monkeypatch.setattr(
        ProviderHealthStore, "public_status", lambda: {"state": "healthy"}
    )
    runtime = RuntimeSettings()
    runtime.llm.api_key = None
    runtime.research.api_key = None
    runtime.gmail.password = None
    runtime.plaid.client_id = runtime.plaid.secret = runtime.plaid.access_token = None
    RuntimeSettingsStore.save(runtime)


@pytest.mark.asyncio
async def test_language_preference_persists_through_existing_api(tmp_path):
    app = FastAPI()
    app.include_router(integrations_router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.get("/integrations/settings")).json()[
            "presentation_language"
        ] == "en"
        saved = await client.put(
            "/integrations/settings", json={"presentation_language": "es"}
        )
        assert saved.status_code == 200
        assert saved.json()["presentation_language"] == "es"
        assert RuntimeSettingsStore.load().presentation_language == "es"
        assert (await client.get("/integrations/settings")).json()[
            "presentation_language"
        ] == "es"
        assert (
            await client.put(
                "/integrations/settings", json={"presentation_language": "unsupported"}
            )
        ).status_code == 422
        assert RuntimeSettingsStore.load().presentation_language == "es"


def test_preference_is_presentation_only_and_separates_reasoning_cache():
    runtime = RuntimeSettingsStore.load()
    service = ReasoningService(None)
    RuntimeSettingsStore.save(runtime)
    english_key = service._analysis_cache_label()
    runtime.presentation_language = "zh-Hans"
    RuntimeSettingsStore.save(runtime)
    assert service._analysis_cache_label() != english_key
    instruction = presentation_instruction("assistant_message")
    assert "zh-Hans" in instruction and "assistant_message" in instruction
    assert "machine-readable assertions unchanged" in instruction
    assert "financial units" in instruction
    assert "zh-Hans" in service._analysis_system_prompt()
    with pytest.raises(ValidationError):
        IntegrationSettingsUpdate(presentation_language="unknown")


@pytest.mark.parametrize(
    "source",
    [ORIGINAL, "收入为 CNY 125.30；参考 [证据A]。", "Le rendement est 2.5% selon [2]."],
)
def test_protected_spans_survive_multilingual_text(source):
    masked, spans = protect_text(source)
    assert (
        restore_text(
            masked.replace("revenue was", "ingresos fueron"), spans, source
        ).count("⟦PH")
        == 0
    )
    for span in spans:
        assert span in restore_text(masked, spans, source)


@pytest.mark.parametrize(
    "damage", ["missing", "duplicate", "new_number", "changed_marker", "empty"]
)
def test_translation_rejects_citation_numeric_or_identifier_damage(damage):
    masked, spans = protect_text(ORIGINAL)
    damaged = {
        "missing": masked.replace("⟦PH00000⟧", ""),
        "duplicate": masked + " ⟦PH00000⟧",
        "new_number": masked + " 999",
        "changed_marker": masked.replace("⟦PH00000⟧", "NVDA"),
        "empty": "",
    }[damage]
    with pytest.raises(ValueError):
        restore_text(damaged, spans, ORIGINAL)


@pytest.mark.parametrize(
    "value",
    [
        "-$12.5 million",
        "$-12.5 million",
        "($125.50)",
        "−2.5%",
        "+30 bps",
        "1.2e-3 USD",
        "12 millones",
        "3 milliards",
        "12.5亿元",
        "125 shares",
    ],
)
def test_signed_values_and_financial_units_are_protected_as_one_span(value):
    original = f"Value: {value}."
    masked, spans = protect_text(original)
    assert value in spans
    assert value not in masked
    assert restore_text(masked, spans, original) == original
    with pytest.raises(ValueError):
        restore_text(masked.replace("⟦PH00000⟧", "125"), spans, original)


@pytest.mark.asyncio
async def test_versioned_translation_cache_preserves_original_and_knowledge(
    monkeypatch, tmp_path
):
    from investos.services import translation

    engine = create_async_engine(settings.SQLALCHEMY_DATABASE_URI, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            async with AsyncSession(
                bind=connection,
                expire_on_commit=False,
                join_transaction_mode="create_savepoint",
            ) as session:
                source = Source(
                    name="Synthetic multilingual source", source_type="manual"
                )
                session.add(source)
                await session.flush()
                root = tmp_path / "storage"
                root.mkdir()
                original_path = root / "original.txt"
                original_path.write_text(ORIGINAL, encoding="utf-8")
                evidence = RawEvidence(
                    source_id=source.id,
                    source_item_type="manual_note",
                    raw_content_ref="original.txt",
                    metadata_json={"synthetic": True},
                    is_processed=False,
                )
                session.add(evidence)
                await session.flush()
                service = TranslationService(session)
                masked, _ = protect_text(ORIGINAL)
                call = AsyncMock(
                    return_value={
                        "translated_text": masked.replace(
                            "revenue was", "ingresos fueron"
                        )
                    }
                )
                monkeypatch.setattr(translation, "call_llm_json", call)
                first = await service.view(evidence.id, "es")
                assert first["status"] == "untranslated" and call.await_count == 0
                session.add(
                    EvidenceTranslation(
                        evidence_id=evidence.id,
                        source_hash=first["source_hash"],
                        target_language="es",
                        recipe_version="protected-spans-v1",
                        provider="synthetic",
                        translated_text="Older unchecked derivative",
                    )
                )
                await session.flush()
                old_policy = await service.view(evidence.id, "es")
                assert (
                    old_policy["status"] == "stale"
                    and old_policy["translated_text"] is None
                )
                made = await service.view(evidence.id, "es", generate=True)
                assert made["status"] == "translated"
                assert (
                    "ingresos fueron" in made["translated_text"]
                    and "USD 12.50" in made["translated_text"]
                )
                assert str(evidence.id) in made["original_url"]
                cached = await service.view(evidence.id, "es", generate=True)
                assert cached["translation_id"] == made["translation_id"]
                assert cached["status"] == "cached" and call.await_count == 1
                assert original_path.read_text(encoding="utf-8") == ORIGINAL
                assert evidence.is_processed is False and evidence.metadata_json == {
                    "synthetic": True
                }
                assert (
                    await session.scalar(
                        select(func.count())
                        .select_from(SourceItem)
                        .where(SourceItem.raw_evidence_id == evidence.id)
                    )
                    == 0
                )
                changed = ORIGINAL.replace("12.50", "15.75")
                original_path.write_text(changed, encoding="utf-8")
                stale = await service.view(evidence.id, "es")
                assert stale["status"] == "stale" and stale["translated_text"] is None
                assert stale["source_hash"] != made["source_hash"]
                call.return_value = {"translated_text": protect_text(changed)[0]}
                regenerated = await service.view(evidence.id, "es", generate=True)
                assert regenerated["translation_id"] != made["translation_id"]
                assert (
                    await session.scalar(
                        select(func.count())
                        .select_from(EvidenceTranslation)
                        .where(EvidenceTranslation.evidence_id == evidence.id)
                    )
                    == 3
                )
            await transaction.rollback()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome",
    ["fidelity_failed", "provider_failed", "provider_deferred", "source_too_large"],
)
async def test_translation_failures_never_store_a_derivative(monkeypatch, outcome):
    from investos.services import translation

    engine = create_async_engine(settings.SQLALCHEMY_DATABASE_URI, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            async with AsyncSession(
                bind=connection,
                expire_on_commit=False,
                join_transaction_mode="create_savepoint",
            ) as session:
                source = Source(name="Synthetic failure source", source_type="manual")
                session.add(source)
                await session.flush()
                evidence = RawEvidence(
                    source_id=source.id,
                    source_item_type="manual_note",
                    is_processed=False,
                )
                session.add(evidence)
                await session.flush()
                service = TranslationService(session)
                content = ORIGINAL if outcome != "source_too_large" else "x" * 25000
                monkeypatch.setattr(
                    service,
                    "original",
                    AsyncMock(return_value=(evidence, content, "a" * 64)),
                )
                call = AsyncMock(
                    return_value={"translated_text": "Unfaithful translation 999"}
                )
                if outcome == "provider_failed":
                    call.side_effect = RuntimeError("secret provider diagnostic")
                if outcome == "provider_deferred":
                    monkeypatch.setattr(
                        ProviderHealthStore, "public_status", lambda: {"state": "open"}
                    )
                monkeypatch.setattr(translation, "call_llm_json", call)
                result = await service.view(evidence.id, "fr", generate=True)
                assert result["status"] == outcome and result["translated_text"] is None
                assert "secret" not in str(result)
                if outcome in {"provider_deferred", "source_too_large"}:
                    assert call.await_count == 0
                assert (
                    await session.scalar(
                        select(func.count())
                        .select_from(EvidenceTranslation)
                        .where(EvidenceTranslation.evidence_id == evidence.id)
                    )
                    == 0
                )
                assert evidence.is_processed is False
            await transaction.rollback()
    finally:
        await engine.dispose()
