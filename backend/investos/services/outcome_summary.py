"""Read-only summary of what the learning loop has scored so far.

Market setups and source claims are scored against later evidence by their own
jobs. This module only counts and lists those recorded outcomes for the
Decisions page; it never scores, rescores, or changes anything.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from investos.models.catalog import SourceClaimRecord
from investos.models.knowledge import Claim
from investos.models.market_setup import MarketSetupSignal
from investos.models.shadow import ShadowExperiment
from investos.models.source import Source

SETUP_HELD = "validated"
SETUP_FAILED = "invalidated"
CLAIM_CORRECT = "correct"
CLAIM_INCORRECT = "incorrect"


def _rate(hits: int, misses: int) -> float | None:
    """Share of decided outcomes that held; undecided outcomes are not counted."""
    decided = hits + misses
    return round(hits / decided, 4) if decided else None


class OutcomeSummaryService:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def summary(self, *, recent_limit: int = 8) -> dict[str, Any]:
        now = datetime.now(UTC)

        setup_counts = dict(
            (
                await self.session.execute(
                    select(MarketSetupSignal.outcome_status, func.count())
                    .where(MarketSetupSignal.is_deprecated.is_(False))
                    .group_by(MarketSetupSignal.outcome_status)
                )
            ).all()
        )
        recent_setups = (
            (
                await self.session.execute(
                    select(MarketSetupSignal)
                    .where(MarketSetupSignal.is_deprecated.is_(False))
                    .where(MarketSetupSignal.outcome_status != "unscored")
                    .order_by(MarketSetupSignal.updated_at.desc())
                    .limit(recent_limit)
                )
            )
            .scalars()
            .all()
        )

        claim_counts: dict[str, int] = {}
        for assessment, count in (
            await self.session.execute(
                select(SourceClaimRecord.assessment, func.count()).group_by(
                    SourceClaimRecord.assessment
                )
            )
        ).all():
            key = assessment or "pending"
            claim_counts[key] = claim_counts.get(key, 0) + int(count)
        overdue_claims = (
            await self.session.execute(
                select(func.count())
                .select_from(SourceClaimRecord)
                .where(
                    or_(
                        SourceClaimRecord.assessment.is_(None),
                        SourceClaimRecord.assessment == "pending",
                    )
                )
                .where(SourceClaimRecord.next_assessment_at < now)
            )
        ).scalar_one()
        recent_claims = (
            await self.session.execute(
                select(SourceClaimRecord, Claim.statement, Source.name)
                .join(Claim, Claim.id == SourceClaimRecord.claim_id, isouter=True)
                .join(Source, Source.id == SourceClaimRecord.source_id, isouter=True)
                .where(SourceClaimRecord.assessment.is_not(None))
                .where(SourceClaimRecord.assessment != "pending")
                .order_by(SourceClaimRecord.assessment_time.desc().nulls_last())
                .limit(recent_limit)
            )
        ).all()

        shadow_counts = dict(
            (
                await self.session.execute(
                    select(ShadowExperiment.run_status, func.count()).group_by(
                        ShadowExperiment.run_status
                    )
                )
            ).all()
        )

        held = int(setup_counts.get(SETUP_HELD, 0))
        failed = int(setup_counts.get(SETUP_FAILED, 0))
        correct = int(claim_counts.get(CLAIM_CORRECT, 0))
        incorrect = int(claim_counts.get(CLAIM_INCORRECT, 0))
        return {
            "generated_at": now,
            "market_setups": {
                "counts": {str(k): int(v) for k, v in setup_counts.items()},
                "scored": sum(
                    int(v) for k, v in setup_counts.items() if k != "unscored"
                ),
                "awaiting": int(setup_counts.get("unscored", 0)),
                "held_rate": _rate(held, failed),
                "recent": [
                    {
                        "id": item.id,
                        "label": " · ".join(
                            part for part in (item.ticker, item.signal_name) if part
                        )
                        or "Market setup",
                        "direction": item.direction,
                        "outcome": item.outcome_status,
                        "notes": item.outcome_notes,
                        "as_of": item.as_of,
                    }
                    for item in recent_setups
                ],
            },
            "source_claims": {
                "counts": {str(k): int(v) for k, v in claim_counts.items()},
                "scored": sum(
                    int(v) for k, v in claim_counts.items() if k != "pending"
                ),
                "awaiting": int(claim_counts.get("pending", 0)),
                "overdue": int(overdue_claims),
                "correct_rate": _rate(correct, incorrect),
                "recent": [
                    {
                        "id": record.id,
                        "label": statement or "Source claim",
                        "source": source_name,
                        "ticker": record.ticker,
                        "outcome": record.assessment,
                        "notes": record.notes,
                        "as_of": record.assessment_time,
                    }
                    for record, statement, source_name in recent_claims
                ],
            },
            "shadow_experiments": {str(k): int(v) for k, v in shadow_counts.items()},
        }
