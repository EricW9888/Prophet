from __future__ import annotations

from uuid import UUID

from investos.models.portfolio import Transaction


def transaction_source_summary(txn: Transaction) -> dict[str, object]:
    provenance = txn.provenance_json if isinstance(txn.provenance_json, dict) else {}
    source_type = _clean_optional_text(
        provenance.get("source_type") or provenance.get("source")
    )
    raw_evidence_id = provenance.get("raw_evidence_id") or provenance.get("evidence_id")
    source_evidence_id: UUID | None = None
    if raw_evidence_id:
        try:
            source_evidence_id = UUID(str(raw_evidence_id))
        except ValueError:
            source_evidence_id = None

    source_confidence: float | None = None
    confidence_raw = provenance.get("confidence")
    if confidence_raw is not None:
        try:
            source_confidence = float(confidence_raw)
        except (TypeError, ValueError):
            source_confidence = None

    notes = str(getattr(txn, "notes", "") or "").lower()
    source_label = _clean_optional_text(provenance.get("source_label"))
    if source_label is None:
        if (
            source_type in {"email_order_confirmation", "gmail"}
            or "deterministic parse" in notes
        ):
            source_label = "Broker email"
        elif source_evidence_id is not None:
            source_label = "Evidence-backed"
        else:
            source_label = "Manual/API"

    return {
        "source_type": source_type,
        "source_label": source_label,
        "source_evidence_id": source_evidence_id,
        "source_confidence": source_confidence,
        "provenance": provenance,
    }


def _clean_optional_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None
