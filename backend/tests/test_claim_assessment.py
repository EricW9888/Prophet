"""Recording a claim verdict keeps what the claim already knew."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from investos.config import settings
from investos.models.catalog import SourceClaimRecord
from investos.models.evidence import RawEvidence, SourceItem
from investos.models.knowledge import Claim
from investos.models.source import Source
from investos.services.source import SourceService


@pytest.fixture
async def claim_session():
    engine = create_async_engine(settings.SQLALCHEMY_DATABASE_URI, poolclass=NullPool)
    async with engine.connect() as connection:
        transaction = await connection.begin()
        async with AsyncSession(bind=connection, expire_on_commit=False) as session:
            yield session
        if transaction.is_active:
            await transaction.rollback()
    await engine.dispose()


async def _pending_record(
    session: AsyncSession, horizon_days: int
) -> SourceClaimRecord:
    tag = uuid4().hex[:8]
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
    claim = Claim(
        statement=f"claim {tag}",
        claim_type="forecast",
        confidence=0.5,
        source_item_id=item.id,
        tier="secondary",
        importance="medium",
        directness="direct",
        novelty="new",
        contradiction_role="none",
    )
    session.add(claim)
    await session.flush()
    record = SourceClaimRecord(
        source_id=source.id,
        claim_id=claim.id,
        claim_time=datetime.now(UTC) - timedelta(days=60),
        assessment="pending",
        horizon_days=horizon_days,
    )
    session.add(record)
    await session.flush()
    return record


async def test_a_verdict_without_a_horizon_keeps_the_claims_horizon(
    claim_session,
) -> None:
    record = await _pending_record(claim_session, horizon_days=30)

    result = await SourceService(claim_session).update_claim_assessment(
        claim_record_id=record.id, assessment="correct", notes="owner verdict"
    )

    assert result is not None
    assert result["assessment"] == "correct"
    assert result["horizon_days"] == 30
    assert result["assessment_time"] is not None


async def test_an_explicit_horizon_still_replaces_it(claim_session) -> None:
    record = await _pending_record(claim_session, horizon_days=30)

    result = await SourceService(claim_session).update_claim_assessment(
        claim_record_id=record.id, assessment="incorrect", horizon_days=90
    )

    assert result is not None
    assert result["horizon_days"] == 90
