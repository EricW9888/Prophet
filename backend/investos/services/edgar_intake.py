import re

from sqlalchemy import and_, func, or_, select
from sqlalchemy.dialects.postgresql import array

# EDGAR form families whose documents are fund administration, structured-note
# terms, asset-backed servicing data, or exempt-offering notices. They arrive
# in the largest volumes from the all-issuer feeds, and each one rarely changes
# an investment view. They stay eligible for extraction; the queue only lets
# substantive work captured shortly after them go first.
ROUTINE_FILING_FORMS = (
    "D",
    "D/A",
    "FWP",
    "10-D",
    "10-D/A",
    "24F-2NT",
    "40-17G",
    "424B2",
    "CERT",
    "EFFECT",
    "MA-I",
    "MA-I/A",
    "S-6",
    "X-17A-5",
    "25-NSE",
)
ROUTINE_FILING_FORM_PREFIXES = ("NPORT-", "N-", "485", "497", "ABS-")


def _form(title):
    """The form type EDGAR puts first in its feed titles ("8-K - Issuer ...")."""
    return func.split_part(title, " ", 1)


def is_edgar_current_report(url, title):
    """Identify current-report entries from EDGAR's own feed titles."""
    return and_(
        url.regexp_match(
            r"^https?://(?:www\.)?sec\.gov/Archives/edgar/data/[0-9]+/[0-9]+/[0-9-]+-index\.html?(?:[?#].*)?$",
            flags="i",
        ),
        _form(title).in_(("8-K", "8-K/A", "6-K", "6-K/A")),
    )


def is_edgar_routine_filing(source_item_type, title):
    """Identify routine filing forms (see ROUTINE_FILING_FORMS) by title."""
    form = _form(title)
    return and_(
        source_item_type == "filing",
        or_(
            form.in_(ROUTINE_FILING_FORMS),
            *(form.startswith(prefix) for prefix in ROUTINE_FILING_FORM_PREFIXES),
        ),
    )


# 8-K item codes. A material item is read ahead of same-age work; an 8-K that
# carries only administrative items ages like a routine form. Everything else
# (for example 8.01 "other events", which can be material) keeps its place.
MATERIAL_8K_ITEMS = (
    "1.01",
    "1.02",
    "1.03",
    "1.04",
    "1.05",
    "2.01",
    "2.02",
    "2.03",
    "2.04",
    "2.05",
    "2.06",
    "3.01",
    "3.03",
    "4.01",
    "4.02",
    "5.01",
    "5.02",
)
ADMINISTRATIVE_8K_ITEMS = ("5.07", "7.01", "9.01")
_ITEM = re.compile(r"\bItem\s+(\d\.\d\d)\b", re.IGNORECASE)


def edgar_items(text: str | None) -> list[str]:
    """The 8-K item codes a filing lists ("Item 2.02: Results of ...")."""
    return sorted(set(_ITEM.findall(text or "")))


def edgar_items_material(metadata):
    """SQL: the stored item codes include a material item."""
    return metadata["edgar_items"].has_any(array(list(MATERIAL_8K_ITEMS)))


def edgar_items_administrative(metadata):
    """SQL: the stored item codes are all administrative (and there is one)."""
    return and_(
        func.jsonb_array_length(metadata["edgar_items"]) > 0,
        metadata["edgar_items"].contained_by(list(ADMINISTRATIVE_8K_ITEMS)),
    )


async def record_edgar_items(session, *, limit: int = 300) -> int:
    """Record item codes for unread 8-Ks that have none yet, from stored text.

    Bounded per call; new captures are picked up on the next call, and the
    existing backlog is worked through in batches. Metadata only.
    """
    from investos.config import settings
    from investos.core.storage import LocalStorage
    from investos.models.evidence import RawEvidence, SourceItem

    rows = (
        (
            await session.execute(
                select(RawEvidence)
                .where(
                    RawEvidence.is_processed.is_(False),
                    RawEvidence.source_item_type == "filing",
                    _form(RawEvidence.title).in_(("8-K", "8-K/A")),
                    or_(
                        RawEvidence.metadata_json.is_(None),
                        ~RawEvidence.metadata_json.has_key("edgar_items"),
                    ),
                )
                .order_by(RawEvidence.created_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return 0
    texts = dict(
        (
            await session.execute(
                select(SourceItem.raw_evidence_id, SourceItem.extracted_text).where(
                    SourceItem.raw_evidence_id.in_([row.id for row in rows])
                )
            )
        ).all()
    )
    storage = LocalStorage(settings.STORAGE_DIR)
    recorded = 0
    for row in rows:
        text = texts.get(row.id) or ""
        if not text and row.raw_content_ref:
            try:
                text = (await storage.get_object(row.raw_content_ref)).decode(
                    "utf-8", errors="replace"
                )
            except Exception:
                text = ""
        row.metadata_json = {
            **(row.metadata_json or {}),
            "edgar_items": edgar_items(text),
        }
        recorded += 1
    await session.commit()
    return recorded
