"""The #302 repair moves misfiled evidence and never touches operator trust."""

from __future__ import annotations

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
from investos.services.source_attribution_repair import (
    SourceAttributionRepair,
    publisher_host,
)


@pytest.fixture
async def repair_session():
    engine = create_async_engine(settings.SQLALCHEMY_DATABASE_URI, poolclass=NullPool)
    async with engine.connect() as connection:
        transaction = await connection.begin()
        async with AsyncSession(bind=connection, expire_on_commit=False) as session:
            yield session
        if transaction.is_active:
            await transaction.rollback()
    await engine.dispose()


def test_publisher_host_matches_source_identity_rule() -> None:
    assert publisher_host("https://www.sec.gov/Archives/x") == "sec.gov"
    assert publisher_host("https://user@Example.org:8443/a") == "example.org"
    assert publisher_host(None) is None


async def _evidence(session, source: Source, url: str) -> tuple[RawEvidence, Claim]:
    evidence = RawEvidence(
        source_id=source.id, source_item_type="news", title="t", url=url
    )
    session.add(evidence)
    await session.flush()
    item = SourceItem(raw_evidence_id=evidence.id, source_id=source.id)
    session.add(item)
    await session.flush()
    claim = Claim(
        statement=f"claim {uuid4().hex[:6]}",
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
    return evidence, claim


async def test_repair_moves_merges_renames_and_protects_operator_trust(
    repair_session,
) -> None:
    from datetime import UTC, datetime

    session = repair_session
    tag = uuid4().hex[:8]
    publisher = Source(
        name=f"sec-{tag}.example", source_type="official", is_trusted=False
    )
    form_source = Source(
        name=f"424B2-{tag}", source_type="news", is_trusted=True, trust_origin="learned"
    )
    single = Source(
        name=f"Company IR {tag}",
        source_type="official",
        is_trusted=True,
        trust_origin="learned",
    )
    operator = Source(
        name=f"Analyst {tag}",
        source_type="analyst",
        is_trusted=True,
        trust_origin="operator",
    )
    correct = Source(name=f"good-{tag}.example", source_type="news", is_trusted=False)
    session.add_all([publisher, form_source, single, operator, correct])
    await session.flush()

    misfiled, claim = await _evidence(
        session, form_source, f"https://www.sec-{tag}.example/a"
    )
    session.add(
        SourceClaimRecord(
            source_id=form_source.id,
            claim_id=claim.id,
            claim_time=datetime.now(UTC),
            assessment="pending",
        )
    )
    single_evidence, _ = await _evidence(
        session, single, f"https://ir-{tag}.example/q3"
    )
    operator_evidence, _ = await _evidence(
        session, operator, f"https://elsewhere-{tag}.example/b"
    )
    kept, _ = await _evidence(session, correct, f"https://good-{tag}.example/c")
    await session.flush()

    repair = SourceAttributionRepair(session)
    plan = {row["source"]: row for row in (await repair.plan(sample=5000))["sample"]}
    assert plan[form_source.name]["action"] == "move"  # its publisher already exists
    assert plan[single.name]["action"] == "rename"  # one publisher, no existing row
    assert operator.name not in plan and correct.name not in plan

    await repair.apply()
    for row in (
        misfiled,
        single_evidence,
        operator_evidence,
        kept,
        form_source,
        single,
        operator,
    ):
        await session.refresh(row)

    # Move: evidence, its processed item and claim record follow; the emptied
    # title-named row loses learned trust; the existing publisher gains no trust.
    assert misfiled.source_id == publisher.id
    item_source = (
        await session.execute(
            select(SourceItem.source_id).where(
                SourceItem.raw_evidence_id == misfiled.id
            )
        )
    ).scalar_one()
    assert item_source == publisher.id
    record_source = (
        await session.execute(
            select(SourceClaimRecord.source_id).where(
                SourceClaimRecord.claim_id == claim.id
            )
        )
    ).scalar_one()
    assert record_source == publisher.id
    assert form_source.is_trusted is False and form_source.trust_origin == "learned"
    assert publisher.is_trusted is False

    # Rename: same row, publisher name, learned trust earned from that publisher kept.
    assert single.name == f"ir-{tag}.example" and single.is_trusted is True
    assert single_evidence.source_id == single.id

    # Operator decisions and correct rows are untouched.
    assert operator_evidence.source_id == operator.id and operator.is_trusted is True
    assert kept.source_id == correct.id

    # A second run finds nothing left to repair for these rows.
    again = {row["source"] for row in (await repair.plan(sample=5000))["sample"]}
    assert not again & {form_source.name, single.name, f"ir-{tag}.example"}
