"""Keep documented failures out of source assessment reads."""

from sqlalchemy import or_, select

from investos.models.catalog import SourceProfile
from investos.models.source import SourceQualitySegment

# Older versions wrote these notes along with fallback scores. The literal
# identifies a recorded assessment outcome, independently of its ratings.
LEGACY_UNAVAILABLE_ASSESSMENT = "Source has been observed, but there is not enough evidence yet to promote it confidently."
_LEGACY_UNAVAILABLE_WITH_FEEDBACK = (
    LEGACY_UNAVAILABLE_ASSESSMENT + " User feedback observed:"
)


def documented_unavailable_assessment(notes: str | None) -> bool:
    return notes == LEGACY_UNAVAILABLE_ASSESSMENT or bool(
        notes and notes.startswith(_LEGACY_UNAVAILABLE_WITH_FEEDBACK)
    )


def assessment_note_available(notes):
    return or_(
        notes == LEGACY_UNAVAILABLE_ASSESSMENT,
        notes.startswith(_LEGACY_UNAVAILABLE_WITH_FEEDBACK),
    ).is_not(True)


def trust_assessment_available(source_id):
    return (
        ~select(SourceProfile.source_id)
        .where(
            SourceProfile.source_id == source_id,
            assessment_note_available(SourceProfile.notes).is_(False),
        )
        .exists()
    )


def value_assessment_available(source_id):
    latest_assessment = (
        select(assessment_note_available(SourceQualitySegment.notes))
        .where(SourceQualitySegment.source_id == source_id)
        .order_by(
            SourceQualitySegment.last_evaluated.desc().nullslast(),
            SourceQualitySegment.id,
        )
        .limit(1)
        .scalar_subquery()
    )
    return latest_assessment.is_not(False)
