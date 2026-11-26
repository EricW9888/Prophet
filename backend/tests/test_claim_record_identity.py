"""A statement one source repeats is one claim to assess and one result to count."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from investos.config import settings
from investos.models.catalog import SourceClaimRecord
from investos.models.evidence import RawEvidence, SourceItem
from investos.models.knowledge import Claim
from investos.models.source import Source
from investos.services.claim_record_identity import (
    representative_claim_record,
    source_already_claimed,
)
from investos.services.outcome_summary import OutcomeSummaryService


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


async def _source(session, tag):
    source = Source(name=f"synthetic-{tag}.example", source_type="news")
    session.add(source)
    await session.flush()
    raw = RawEvidence(source_id=source.id, source_item_type="news", title="Synthetic")
    session.add(raw)
    await session.flush()
    item = SourceItem(raw_evidence_id=raw.id, source_id=source.id)
    session.add(item)
    await session.flush()
    return source, item


async def _record(session, source, item, statement, assessment, minutes):
    claim = Claim(
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
    session.add(claim)
    await session.flush()
    record = SourceClaimRecord(
        source_id=source.id,
        claim_id=claim.id,
        claim_time=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=minutes),
        assessment=assessment,
        assessment_time=None if assessment == "pending" else datetime.now(UTC),
    )
    session.add(record)
    await session.flush()
    return record


async def test_one_record_stands_for_each_repeated_statement(claim_session):
    session = claim_session
    tag = uuid4().hex[:8]
    summary_before = await OutcomeSummaryService(session).summary()
    source, item = await _source(session, tag)
    other_source, other_item = await _source(session, tag + "b")
    rise = f"Revenue will rise {tag}."

    # The scored copy stands for its statement even though it is not the earliest.
    early_pending = await _record(session, source, item, rise, "pending", 1)
    later_pending = await _record(
        session, source, item, f"  revenue   WILL rise {tag}.", "pending", 2
    )
    scored = await _record(session, source, item, rise, "correct", 3)
    # With nothing scored, the earliest waiting copy stands for the statement.
    first_waiting = await _record(
        session, source, item, f"Margins narrow {tag}.", "pending", 1
    )
    second_waiting = await _record(
        session, source, item, f"Margins narrow {tag}.", "pending", 2
    )
    # Another source making the same statement is a separate claim.
    other = await _record(session, other_source, other_item, rise, "pending", 1)

    ids = {
        early_pending.id,
        later_pending.id,
        scored.id,
        first_waiting.id,
        second_waiting.id,
        other.id,
    }
    representatives = set(
        (
            await session.scalars(
                select(SourceClaimRecord.id)
                .join(Claim, Claim.id == SourceClaimRecord.claim_id)
                .where(SourceClaimRecord.id.in_(ids), representative_claim_record())
            )
        ).all()
    )
    assert representatives == {scored.id, first_waiting.id, other.id}

    summary_after = await OutcomeSummaryService(session).summary()
    claims_before = summary_before["source_claims"]
    claims_after = summary_after["source_claims"]
    assert claims_after["scored"] - claims_before["scored"] == 1
    assert claims_after["awaiting"] - claims_before["awaiting"] == 2


async def test_a_source_already_has_a_record_for_a_repeated_statement(claim_session):
    session = claim_session
    tag = uuid4().hex[:8]
    source, item = await _source(session, tag)
    await _record(session, source, item, f"Demand recovers {tag}.", "pending", 1)

    assert await source_already_claimed(
        session, source.id, f" demand  RECOVERS {tag}. "
    )
    assert not await source_already_claimed(session, source.id, f"Demand falls {tag}.")
    assert not await source_already_claimed(session, source.id, "   ")
