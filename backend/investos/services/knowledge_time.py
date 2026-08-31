from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime

YEAR_RE = re.compile(r"\b(20\d{2})\b")
FORECAST_RE = re.compile(
    r"\b(will|would|could|expect(?:s|ed)?|forecast(?:s|ed)?|project(?:s|ed)?|target(?:s|ed)?|guidance)\b",
    re.IGNORECASE,
)

BREAKING_WINDOW_DAYS = 3
CURRENT_WINDOW_DAYS = 7


@dataclass(frozen=True)
class KnowledgeTemporalContext:
    status: str
    novelty: str
    referenced_years: tuple[int, ...]
    explanation: str


def as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def referenced_years(text: str | None) -> tuple[int, ...]:
    return tuple(sorted({int(value) for value in YEAR_RE.findall(text or "")}))


def is_legacy_synthetic_event_time(
    event_time: datetime | None,
    *,
    public_time: datetime | None,
    ingest_time: datetime | None,
) -> bool:
    """Recognize the former fact/claim fallback that copied source time into event time."""

    event_time = as_utc(event_time)
    if event_time is None:
        return False
    for source_time in (public_time, ingest_time):
        source_time = as_utc(source_time)
        if (
            source_time is not None
            and abs((event_time - source_time).total_seconds()) < 1
        ):
            return True
    return False


def infer_expired_forecast_time(
    text: str | None,
    *,
    reference_time: datetime,
) -> datetime | None:
    """Return the end of an explicitly past forecast year, without dating history by guess."""

    if not FORECAST_RE.search(text or ""):
        return None
    years = [year for year in referenced_years(text) if year < reference_time.year]
    if not years:
        return None
    return datetime(max(years), 12, 31, 23, 59, 59, tzinfo=reference_time.tzinfo or UTC)


def assess_knowledge_time(
    text: str | None,
    *,
    event_time: datetime | None,
    public_time: datetime | None,
    ingest_time: datetime | None,
    valid_until: datetime | None = None,
    item_type: str = "fact",
    now: datetime | None = None,
) -> KnowledgeTemporalContext:
    """Classify temporal use without treating ingestion as when a claim happened."""

    now = as_utc(now) or datetime.now(UTC)
    event_time = as_utc(event_time)
    public_time = as_utc(public_time)
    ingest_time = as_utc(ingest_time)
    valid_until = as_utc(valid_until)
    reference_time = public_time or ingest_time or now
    years = referenced_years(text)

    expired_target = valid_until is not None and valid_until < now
    if not expired_target and item_type == "claim":
        expired_target = (
            infer_expired_forecast_time(
                text,
                reference_time=reference_time,
            )
            is not None
        )
    if expired_target:
        return KnowledgeTemporalContext(
            status="outcome_due",
            novelty="historical",
            referenced_years=years,
            explanation="The forecast target is in the past; later outcome evidence should now be checked.",
        )

    if event_time is not None and event_time > now:
        return KnowledgeTemporalContext(
            status="scheduled",
            novelty="breaking",
            referenced_years=years,
            explanation="This is a future-dated event or target, not an observed outcome.",
        )

    if event_time is not None:
        age_days = max(0.0, (now - event_time).total_seconds() / 86400)
        source_lag_days = max(
            0.0,
            (reference_time - event_time).total_seconds() / 86400,
        )
        if age_days > CURRENT_WINDOW_DAYS:
            return KnowledgeTemporalContext(
                status="historical",
                novelty="historical",
                referenced_years=years,
                explanation="The underlying event is older than the current-attention window and remains available as historical context.",
            )
        return KnowledgeTemporalContext(
            status="current",
            novelty=(
                "breaking"
                if age_days <= BREAKING_WINDOW_DAYS
                and source_lag_days <= BREAKING_WINDOW_DAYS
                else "confirming"
            ),
            referenced_years=years,
            explanation="The source provides an explicit event date close to its publication or ingestion time.",
        )

    past_years = [year for year in years if year < reference_time.year]
    if past_years:
        return KnowledgeTemporalContext(
            status="historical",
            novelty="historical",
            referenced_years=years,
            explanation=(
                "The statement refers to "
                + ", ".join(str(year) for year in past_years)
                + "; no exact event date was established."
            ),
        )

    if public_time is None:
        return KnowledgeTemporalContext(
            status="undated",
            novelty="confirming",
            referenced_years=years,
            explanation="The source publication time is unknown, so ingestion time is not treated as event time.",
        )

    publication_age_days = max(0.0, (now - public_time).total_seconds() / 86400)
    if publication_age_days > CURRENT_WINDOW_DAYS:
        return KnowledgeTemporalContext(
            status="historical",
            novelty="historical",
            referenced_years=years,
            explanation="The source itself predates the current-attention window; it remains available as historical context.",
        )

    return KnowledgeTemporalContext(
        status="current",
        novelty=(
            "breaking" if publication_age_days <= BREAKING_WINDOW_DAYS else "confirming"
        ),
        referenced_years=years,
        explanation="The item is source-dated and no older underlying period was identified.",
    )
