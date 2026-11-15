"""Synthetic PostgreSQL receipts exercise rejection, promotion and rollback."""

import asyncio
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from investos.config import settings
from investos.core.llm import LLMProviderCooldownError
from investos.db import get_session
from investos.main import app
from investos.models.entity import Entity, Security
from investos.models.evidence import RawEvidence
from investos.models.market_setup import MarketSetupSignal
from investos.models.reasoning import ReasoningRun
from investos.models.source import Source
from investos.services.dashboard import DashboardService
from investos.services.pattern_discovery import PatternDiscoveryService
from investos.services.reasoning_trace import ReasoningTraceService
from tests.test_pattern_discovery import _result


@pytest.fixture
async def pattern_session():
    engine = create_async_engine(settings.SQLALCHEMY_DATABASE_URI, poolclass=NullPool)
    async with engine.connect() as connection:
        transaction = await connection.begin()
        async with AsyncSession(
            bind=connection,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        ) as session:
            yield session
        await transaction.rollback()
    await engine.dispose()


@pytest.fixture
async def discovery(pattern_session, monkeypatch):
    session = pattern_session
    tracked = {}
    signals = []
    for ticker in ("MEMA", "MEMB"):
        entity = Entity(name=f"Synthetic {ticker} {uuid4().hex}", entity_type="company")
        source = Source(
            name=f"Synthetic {ticker}",
            source_type="web_research",
            url=f"https://{ticker.lower()}.example.test",
        )
        session.add_all([entity, source])
        await session.flush()
        security = Security(
            entity_id=entity.id,
            ticker=ticker,
            asset_class="equity",
            instrument_type="common_stock",
        )
        raw = RawEvidence(
            source_id=source.id,
            source_item_type="web_research",
            title="Synthetic capacity report",
            url=f"{source.url}/capacity",
        )
        session.add_all([security, raw])
        await session.flush()
        signal = MarketSetupSignal(
            ticker=ticker,
            entity_id=entity.id,
            security_id=security.id,
            raw_evidence_id=raw.id,
            signal_family="supply",
            signal_name=f"{ticker} capacity",
            setup_context="Synthetic supply report, not a market prediction.",
            as_of=datetime.now(UTC) - timedelta(days=1),
            public_time=None,
        )
        session.add(signal)
        await session.flush()
        signals.append(signal)
        tracked[ticker] = {
            "ticker": ticker,
            "entity_id": entity.id,
            "security_id": security.id,
            "entity_name": entity.name,
        }
    await session.commit()
    service = PatternDiscoveryService(session)
    monkeypatch.setattr(service, "_tracked_universe", AsyncMock(return_value=tracked))
    monkeypatch.setattr(service, "_historical_episodes", AsyncMock(return_value=[]))
    monkeypatch.setattr(service, "_current_regime", AsyncMock(return_value=None))
    monkeypatch.setattr(
        "investos.services.pattern_discovery.PortfolioPeerContextService.peer_exposures",
        AsyncMock(return_value=[]),
    )
    refs = [f"signal:{index}" for index, _signal in enumerate(signals, start=1)]
    result = _result(evidence_refs=refs)
    result["hypothesis"]["historical_episode_id"] = ""
    model = AsyncMock(return_value=result)
    monkeypatch.setattr("investos.services.pattern_discovery.call_llm_json", model)
    return service, model, signals


async def load_receipt(session, run_id):
    trace = await ReasoningTraceService(session).get_run_trace(UUID(run_id))
    assert trace is not None
    assert trace.evidence_packet is None
    return trace.structured_output_json


async def test_rejected_proposal_and_exact_input_survive_later_source_changes(
    pattern_session, discovery
):
    service, model, signals = discovery
    model.return_value["hypothesis"]["confidence"] = 0.1
    original = deepcopy(model.return_value)
    before = await pattern_session.scalar(
        select(func.count()).select_from(MarketSetupSignal)
    )
    usage_before = await DashboardService(pattern_session)._llm_usage()
    outcome = await service.discover()
    usage_after = await DashboardService(pattern_session)._llm_usage()
    assert usage_after.analysis_runs_24h == usage_before.analysis_runs_24h
    assert usage_after.live_analysis_runs_24h == usage_before.live_analysis_runs_24h
    assert usage_after.total_input_tokens_24h > usage_before.total_input_tokens_24h
    assert outcome["created"] == 0
    receipt = await load_receipt(pattern_session, outcome["reasoning_run_id"])
    assert receipt["proposal"] == original
    assert receipt["review"]["status"] == "rejected"
    assert receipt["review"]["detail"] == "pattern_confidence_below_threshold"
    assert (
        receipt["snapshot"]["input"]["source_dated_signals"][0]["public_time"] is None
    )
    assert receipt["snapshot"]["input"]["source_dated_signals"][0][
        "evidence_url"
    ].endswith("/capacity")
    assert (
        receipt["snapshot"]["generation"]["provider"]
        == model.call_args.kwargs["provider_override"]
    )
    assert "api_key" not in str(receipt)
    signals[0].setup_context = "Later source correction"
    model.return_value["hypothesis"][
        "proposed_mechanism"
    ] = "Changed outside the receipt"
    await pattern_session.commit()
    pattern_session.expire_all()
    assert await load_receipt(pattern_session, outcome["reasoning_run_id"]) == receipt
    assert (
        await pattern_session.scalar(
            select(func.count()).select_from(MarketSetupSignal)
        )
        == before
    )


async def test_provisional_output_and_duplicate_each_keep_their_receipt(
    pattern_session, discovery
):
    service, _model, _signals = discovery
    first = await service.discover()
    assert first["created"] == 1
    receipt = await load_receipt(pattern_session, first["reasoning_run_id"])
    assert receipt["review"]["status"] == "provisional"
    assert receipt["review"]["outcome"]["signal_id"] == first["signal_id"]
    assert all(ref.startswith("setup:") for ref in first["hypothesis"]["evidence_refs"])
    assert set(first["hypothesis"]["evidence_refs"]) == set(
        receipt["snapshot"]["source_reference_map"].values()
    )
    assert first["question_id"]
    second = await service.discover()
    assert second["created"] == 0
    assert second["existing_signal_id"] == first["signal_id"]
    duplicate = await load_receipt(pattern_session, second["reasoning_run_id"])
    assert duplicate["review"]["status"] == "duplicate"
    assert duplicate["proposal"] == receipt["proposal"]


@pytest.mark.parametrize(
    "error", [TimeoutError("private provider detail"), asyncio.CancelledError()]
)
async def test_generation_failure_preserves_input_without_fabricating_proposal(
    pattern_session, discovery, error
):
    service, model, _signals = discovery

    async def fail(**_kwargs):
        # The started receipt is already committed before provider work begins.
        run = await pattern_session.scalar(
            select(ReasoningRun)
            .where(ReasoningRun.run_type == "pattern_discovery")
            .order_by(ReasoningRun.created_at.desc())
            .limit(1)
        )
        assert run.structured_output_json["review"]["status"] == "generating"
        raise error

    model.side_effect = fail
    with pytest.raises(type(error)):
        await service.discover()
    pattern_session.expire_all()
    run = await pattern_session.scalar(
        select(ReasoningRun)
        .where(ReasoningRun.run_type == "pattern_discovery")
        .order_by(ReasoningRun.created_at.desc())
        .limit(1)
    )
    receipt = run.structured_output_json
    assert receipt["snapshot"]["input"]["source_dated_signals"]
    assert "proposal" not in receipt
    assert "private provider detail" not in str(receipt)
    assert receipt["review"]["status"] == (
        "interrupted" if isinstance(error, asyncio.CancelledError) else "failed"
    )


async def test_failed_output_persistence_rolls_back_domain_rows_but_keeps_proposal(
    pattern_session, discovery, monkeypatch
):
    service, model, _signals = discovery

    async def fail(**_kwargs):
        pattern_session.add(
            MarketSetupSignal(
                signal_name="Must roll back", signal_family="pattern_hypothesis"
            )
        )
        await pattern_session.flush()
        raise ValueError("synthetic domain persistence failure")

    monkeypatch.setattr(service, "_persist_hypothesis", fail)
    with pytest.raises(ValueError):
        await service.discover()
    assert (
        await pattern_session.scalar(
            select(func.count())
            .select_from(MarketSetupSignal)
            .where(MarketSetupSignal.signal_name == "Must roll back")
        )
        == 0
    )
    run = await pattern_session.scalar(
        select(ReasoningRun)
        .where(ReasoningRun.run_type == "pattern_discovery")
        .order_by(ReasoningRun.created_at.desc())
        .limit(1)
    )
    assert run.structured_output_json["proposal"] == model.return_value
    assert (
        run.structured_output_json["review"]["detail"] == "proposal_processing_failed"
    )


async def test_explicit_preview_does_not_write_receipts_or_domain_state(
    pattern_session, discovery
):
    service, _model, _signals = discovery
    count = await pattern_session.scalar(select(func.count()).select_from(ReasoningRun))
    outcome = await service.discover(apply=False)
    assert outcome["status"] == "preview"
    assert outcome["reasoning_run_id"] is None
    assert (
        await pattern_session.scalar(select(func.count()).select_from(ReasoningRun))
        == count
    )


async def test_partial_identifiers_are_retained_but_never_guessed(
    pattern_session, discovery
):
    service, model, signals = discovery
    model.return_value["hypothesis"]["evidence_refs"] = [
        f"setup:{str(signals[0].id)[:8]}",
        "signal:2",
    ]
    outcome = await service.discover()
    assert outcome["created"] == 0
    assert outcome["detail"] == "pattern_referenced_unknown_evidence"
    receipt = await load_receipt(pattern_session, outcome["reasoning_run_id"])
    assert (
        receipt["proposal"]["hypothesis"]["evidence_refs"]
        == model.return_value["hypothesis"]["evidence_refs"]
    )
    assert receipt["snapshot"]["source_reference_map"]["signal:1"].startswith("setup:")


async def test_provider_deferral_is_not_a_failed_generation_or_token_usage(
    pattern_session, discovery
):
    service, model, _signals = discovery
    model.side_effect = LLMProviderCooldownError("synthetic provider cooldown")
    with pytest.raises(LLMProviderCooldownError):
        await service.discover()
    pattern_session.expire_all()
    run = await pattern_session.scalar(
        select(ReasoningRun)
        .where(ReasoningRun.run_type == "pattern_discovery")
        .order_by(ReasoningRun.created_at.desc())
        .limit(1)
    )
    assert run.structured_output_json["review"]["status"] == "deferred"
    assert (
        run.structured_output_json["review"]["detail"] == "provider_admission_deferred"
    )
    assert "proposal" not in run.structured_output_json
    assert run.input_tokens is None and run.output_tokens is None


async def test_review_api_paginates_without_transferring_captured_packets(
    pattern_session, discovery
):
    service, model, _signals = discovery
    model.return_value["actionable"] = False
    first = await service.discover()
    second = await service.discover()

    async def same_session():
        yield pattern_session

    app.dependency_overrides[get_session] = same_session
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/api/reasoning/pattern-reviews?limit=1")
            assert response.status_code == 200
            page = response.json()
            assert page["items"][0]["id"] == second["reasoning_run_id"]
            assert page["total"] >= 2
            assert "structured_output_json" not in page["items"][0]
            next_page = (
                await client.get("/api/reasoning/pattern-reviews?limit=1&offset=1")
            ).json()
            assert next_page["items"][0]["id"] == first["reasoning_run_id"]
            detail = await client.get(
                f"/api/reasoning/runs/{first['reasoning_run_id']}"
            )
            assert (
                detail.json()["structured_output_json"]["review"]["status"]
                == "rejected"
            )
            assert (
                await client.get("/api/reasoning/pattern-reviews?limit=101")
            ).status_code == 422
            assert (
                await client.get("/api/reasoning/pattern-reviews?offset=-1")
            ).status_code == 422
    finally:
        app.dependency_overrides.clear()
