from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

SOURCE_CLAIM_REVIEW_HORIZON_DAYS = {
    "tactical": 7,
    "strategic": 30,
    "visionary": 90,
}
SOURCE_CLAIM_DEFAULT_REVIEW_DAYS = 30
SOURCE_CLAIM_MAX_OVERDUE_PRIORITY = 35.0
SOURCE_CLAIM_MAX_AGE_PRIORITY = 10.0
SOURCE_CLAIM_IMPORTANCE_PRIORITY = {
    "critical": 20.0,
    "high": 12.0,
    "medium": 6.0,
    "low": 2.0,
    "trivial": 0.0,
}


def as_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def days_between(start: datetime | None, end: datetime | None) -> float:
    a = as_utc(start)
    b = as_utc(end)
    if a is None or b is None:
        return 0.0
    return max(0.0, (b - a).total_seconds() / 86400.0)


def source_claim_due_at(record: Any, claim: Any | None = None) -> datetime | None:
    valid_until = as_utc(getattr(claim, "valid_until", None))
    if valid_until is not None:
        return valid_until

    explicit_days = getattr(record, "horizon_days", None)
    claim_time = as_utc(getattr(record, "claim_time", None))
    if explicit_days is not None and explicit_days > 0 and claim_time is not None:
        return claim_time + timedelta(days=int(explicit_days))

    stale_after = as_utc(getattr(claim, "stale_after", None))
    if stale_after is not None:
        return stale_after

    horizon = str(getattr(claim, "target_horizon", "") or "").strip().lower()
    days = SOURCE_CLAIM_REVIEW_HORIZON_DAYS.get(
        horizon, SOURCE_CLAIM_DEFAULT_REVIEW_DAYS
    )
    if claim_time is None:
        claim_time = as_utc(getattr(claim, "public_time", None))
    if claim_time is None:
        claim_time = as_utc(getattr(claim, "created_at", None))
    if claim_time is None:
        return None
    return claim_time + timedelta(days=days)


def source_claim_priority(
    record: Any,
    claim: Any | None,
    due_at: datetime | None,
    now: datetime | None = None,
    *,
    portfolio_relevant: bool = False,
    portfolio_weight_pct: float = 0.0,
) -> float:
    now = now or datetime.now(UTC)
    overdue_days = days_between(due_at, now)
    age_days = days_between(getattr(record, "claim_time", None), now)
    importance = str(getattr(claim, "importance", "") or "").strip().lower()
    importance_boost = SOURCE_CLAIM_IMPORTANCE_PRIORITY.get(importance, 4.0)
    original_boost = 5.0 if bool(getattr(claim, "is_original", False)) else 0.0
    portfolio_boost = 0.0
    if portfolio_relevant:
        portfolio_boost = max(SOURCE_CLAIM_IMPORTANCE_PRIORITY.values()) + min(
            SOURCE_CLAIM_MAX_OVERDUE_PRIORITY,
            max(0.0, abs(float(portfolio_weight_pct or 0.0))),
        )
    return (
        22.0
        + importance_boost
        + min(SOURCE_CLAIM_MAX_OVERDUE_PRIORITY, overdue_days)
        + min(SOURCE_CLAIM_MAX_AGE_PRIORITY, age_days / 14.0)
        + original_boost
        + portfolio_boost
    )
