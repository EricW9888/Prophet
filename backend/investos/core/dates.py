from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

MONTHS = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}


def parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def parse_explicit_calendar_datetime(
    text: str | None,
    *,
    reference_time: datetime,
    earliest: datetime | None = None,
    latest: datetime | None = None,
    default_hour: int = 20,
) -> datetime | None:
    """Parse explicit dated calendar text without guessing yearless dates."""

    if not text:
        return None
    iso = parse_iso_datetime(str(text).strip())
    if iso is not None and _within_bounds(iso, earliest=earliest, latest=latest):
        return iso

    tzinfo = reference_time.tzinfo or UTC
    candidates: list[datetime] = []
    compact = " ".join(str(text).split())
    for match in re.finditer(r"\b(20\d{2})[-/](\d{1,2})[-/](\d{1,2})\b", compact):
        candidate = _date_candidate(
            int(match.group(1)),
            int(match.group(2)),
            int(match.group(3)),
            default_hour=default_hour,
            tzinfo=tzinfo,
        )
        if candidate is not None:
            candidates.append(candidate)
    month_pattern = "|".join(sorted(MONTHS, key=len, reverse=True))
    for match in re.finditer(
        rf"\b({month_pattern})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,)?\s+(20\d{{2}})\b",
        compact,
        flags=re.I,
    ):
        month = MONTHS[match.group(1).lower().rstrip(".")]
        candidate = _date_candidate(
            int(match.group(3)),
            month,
            int(match.group(2)),
            default_hour=default_hour,
            tzinfo=tzinfo,
        )
        if candidate is not None:
            candidates.append(candidate)
    for match in re.finditer(
        rf"\b(\d{{1,2}})\s+({month_pattern})\.?\s+(20\d{{2}})\b",
        compact,
        flags=re.I,
    ):
        month = MONTHS[match.group(2).lower().rstrip(".")]
        candidate = _date_candidate(
            int(match.group(3)),
            month,
            int(match.group(1)),
            default_hour=default_hour,
            tzinfo=tzinfo,
        )
        if candidate is not None:
            candidates.append(candidate)

    for candidate in sorted(candidates):
        if _within_bounds(candidate, earliest=earliest, latest=latest):
            return candidate
    return None


def lookahead_calendar_datetime(
    text: str | None, *, now: datetime, horizon: datetime
) -> datetime | None:
    return parse_explicit_calendar_datetime(
        text,
        reference_time=now,
        earliest=now - timedelta(hours=12),
        latest=horizon,
    )


def _date_candidate(
    year: int,
    month: int,
    day: int,
    *,
    default_hour: int,
    tzinfo,
) -> datetime | None:
    try:
        return datetime(year, month, day, default_hour, 0, tzinfo=tzinfo)
    except ValueError:
        return None


def _within_bounds(
    value: datetime,
    *,
    earliest: datetime | None,
    latest: datetime | None,
) -> bool:
    if earliest is not None and value < earliest:
        return False
    if latest is not None and value > latest:
        return False
    return True
