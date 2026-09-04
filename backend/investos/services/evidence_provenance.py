from __future__ import annotations

from uuid import UUID

from investos.models.evidence import RawEvidence
from investos.models.source import Source
from investos.schemas.provenance import EvidenceSourceReferenceResponse
from investos.services.source import SourceService


def build_evidence_source_reference(
    *,
    source_item_id: UUID | None,
    raw_evidence: RawEvidence,
    source: Source,
) -> EvidenceSourceReferenceResponse:
    """Build the user-visible reference for one durable evidence receipt."""

    item_url = str(raw_evidence.url or "").strip()
    source_url = str(source.url or "").strip()
    if item_url:
        url = item_url
        url_kind = "evidence_item"
    elif source_url:
        url = source_url
        url_kind = "source_home"
    else:
        url = None
        url_kind = "unavailable"

    origin = SourceService._evidence_origin_summary(raw_evidence, source)
    return EvidenceSourceReferenceResponse(
        raw_evidence_id=raw_evidence.id,
        source_item_id=source_item_id,
        source_id=source.id,
        source_name=source.name,
        source_type=source.source_type,
        source_item_type=raw_evidence.source_item_type,
        origin_kind=origin["origin_kind"],
        origin_label=origin["origin_label"],
        origin_detail=origin["origin_detail"],
        title=raw_evidence.title,
        url=url,
        url_kind=url_kind,
        author=raw_evidence.author,
        created_at=raw_evidence.created_at,
    )
