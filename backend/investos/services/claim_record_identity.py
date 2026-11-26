"""One source repeating one statement is one claim to assess and to score.

Extraction can record the same statement from the same source several times:
the page is read again, or one document repeats a sentence. Each copy got its
own tracking record, so the assessor re-checked the same claim and the
source's accuracy counted it once per copy. These helpers pick one record to
stand for each (source, statement) pair. Every copy is kept.
"""

from __future__ import annotations

from sqlalchemy import case, exists, func, literal, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from investos.models.catalog import SourceClaimRecord
from investos.models.knowledge import Claim

SCORED = ("correct", "incorrect", "partially_correct")


def statement_key(column):
    """Case and whitespace do not make a statement different."""
    return func.regexp_replace(func.lower(func.btrim(column)), r"\s+", " ", "g")


def _rank(record):
    # A scored copy stands first, then any other finished assessment, then
    # copies still waiting.
    return case(
        (record.assessment.in_(SCORED), literal(0)),
        (record.assessment.is_not(None) & (record.assessment != "pending"), literal(1)),
        else_=literal(2),
    )


def representative_claim_record(record=SourceClaimRecord, claim=Claim):
    """True for the one record that stands for its (source, statement) pair.

    The query must join ``claim`` to ``record``. Within a pair the
    representative is the best-ranked copy, then the earliest claim time,
    then the lowest id.
    """
    other = aliased(SourceClaimRecord)
    other_claim = aliased(Claim)
    return ~exists(
        select(other.id)
        .join(other_claim, other_claim.id == other.claim_id)
        .where(
            other.source_id == record.source_id,
            other.id != record.id,
            statement_key(other_claim.statement) == statement_key(claim.statement),
            tuple_(_rank(other), other.claim_time, other.id)
            < tuple_(_rank(record), record.claim_time, record.id),
        )
    )


async def source_already_claimed(session: AsyncSession, source_id, statement) -> bool:
    """Whether this source already has a tracking record for this statement."""
    if not statement or not str(statement).strip():
        return False
    return bool(
        await session.scalar(
            select(SourceClaimRecord.id)
            .join(Claim, Claim.id == SourceClaimRecord.claim_id)
            .where(
                SourceClaimRecord.source_id == source_id,
                statement_key(Claim.statement)
                == statement_key(literal(str(statement))),
                or_(Claim.is_deprecated.is_(False), Claim.is_deprecated.is_(None)),
            )
            .limit(1)
        )
    )
