"""The Decisions page's outcome summary counts what the learning loop scored."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from investos.config import settings
from investos.models.catalog import SourceClaimRecord
from investos.models.evidence import RawEvidence, SourceItem
from investos.models.knowledge import Claim
from investos.models.market_setup import MarketSetupSignal
from investos.models.source import Source
from investos.services.outcome_summary import OutcomeSummaryService, _rate


@pytest.fixture
async def outcome_session():
    engine = create_async_engine(settings.SQLALCHEMY_DATABASE_URI, poolclass=NullPool)
    async with engine.connect() as connection:
        transaction = await connection.begin()
        async with AsyncSession(bind=connection, expire_on_commit=False) as session:
            yield session
        if transaction.is_active:
            await transaction.rollback()
    await engine.dispose()


def test_rates_ignore_undecided_outcomes() -> None:
    assert _rate(3, 1) == 0.75
    assert _rate(0, 0) is None


async def test_summary_counts_scored_outcomes_and_latest_results(
    outcome_session,
) -> None:
    session = outcome_session
    service = OutcomeSummaryService(session)
    before = await service.summary()
    now = datetime.now(UTC)
    tag = uuid4().hex[:8]

    for status in (
        "validated",
        "validated",
        "invalidated",
        "indeterminate",
        "unscored",
    ):
        session.add(
            MarketSetupSignal(
                ticker="SYN",
                signal_name=f"setup-{tag}-{status}",
                signal_family="synthetic",
                outcome_status=status,
                outcome_notes=f"note {status}",
            )
        )
    session.add(
        MarketSetupSignal(
            ticker="SYN",
            signal_name=f"setup-{tag}-deprecated",
            signal_family="synthetic",
            outcome_status="validated",
            is_deprecated=True,
        )
    )

    source = Source(
        name=f"synthetic-{tag}.example", source_type="news", is_trusted=False
    )
    session.add(source)
    await session.flush()
    evidence = RawEvidence(
        source_id=source.id, source_item_type="news", title="Synthetic"
    )
    session.add(evidence)
    await session.flush()
    item = SourceItem(raw_evidence_id=evidence.id, source_id=source.id)
    session.add(item)
    await session.flush()

    def claim(statement: str) -> Claim:
        value = Claim(
            statement=statement,
            claim_type="forecast",
            confidence=0.5,
            source_item_id=item.id,
            tier="secondary",
            importance="medium",
            directness="direct",
            novelty="new",
            contradiction_role="none",
        )
        session.add(value)
        return value

    claims = [claim(f"claim {tag} {n}") for n in range(4)]
    await session.flush()
    for value, assessment, due in zip(
        claims,
        ("correct", "incorrect", "pending", "pending"),
        (None, None, now - timedelta(days=1), now + timedelta(days=1)),
    ):
        session.add(
            SourceClaimRecord(
                source_id=source.id,
                claim_id=value.id,
                claim_time=now - timedelta(days=30),
                assessment=assessment,
                assessment_time=now if assessment not in ("pending",) else None,
                next_assessment_at=due,
            )
        )
    await session.flush()

    after = await service.summary()
    setups, claims_after = after["market_setups"], after["source_claims"]
    assert (
        setups["scored"] - before["market_setups"]["scored"] == 4
    )  # deprecated excluded
    assert setups["awaiting"] - before["market_setups"]["awaiting"] == 1
    assert (
        setups["counts"]["validated"]
        - before["market_setups"]["counts"].get("validated", 0)
        == 2
    )
    assert claims_after["scored"] - before["source_claims"]["scored"] == 2
    assert claims_after["awaiting"] - before["source_claims"]["awaiting"] == 2
    assert claims_after["overdue"] - before["source_claims"]["overdue"] == 1
    assert any(
        row["label"] == f"claim {tag} 0" and row["source"] == source.name
        for row in claims_after["recent"]
    )
    assert any(row["label"].startswith("SYN · setup-") for row in setups["recent"])


async def test_outcomes_route_is_read_only_and_reachable(
    outcome_session, monkeypatch
) -> None:
    from investos.db import get_session
    from investos.main import app

    async def override():
        yield outcome_session

    app.dependency_overrides[get_session] = override
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://127.0.0.1:8000"
        ) as client:
            response = await client.get("/api/decisions/outcomes")
        assert response.status_code == 200
        body = response.json()
        assert {"market_setups", "source_claims", "shadow_experiments"} <= set(body)
    finally:
        app.dependency_overrides.pop(get_session, None)
