"""Contiguous, attributable source-text windows for restartable extraction."""

import json
from copy import deepcopy
from datetime import UTC, datetime
from hashlib import sha256


class SourceTextChanged(ValueError):
    pass


def prepare_source_text(
    text: str,
    saved: dict | None,
    *,
    intent: dict | None = None,
    representation: str = "stored_utf8_text",
) -> dict:
    digest = sha256(text.encode("utf-8")).hexdigest()
    intent_hash = sha256(json.dumps(intent or {}, sort_keys=True).encode()).hexdigest()
    if saved:
        if (
            saved.get("version") != 1
            or saved.get("document_hash") != digest
            or saved.get("intent_hash") != intent_hash
            or saved.get("representation") != representation
        ):
            raise SourceTextChanged(
                "The stored source representation changed. Restore it or ingest a new evidence version before continuing."
            )
        return deepcopy(saved)
    return {
        "version": 1,
        "representation": representation,
        "document_hash": digest,
        "intent_hash": intent_hash,
        "document_chars": len(text),
        "next_offset": 0,
        "windows": [],
        "seen": {},
        "summaries": [],
        "assessments": [],
    }


def next_source_window(text: str, progress: dict, *, size: int, overlap: int) -> dict:
    start = int(progress["next_offset"])
    if not 0 <= start <= len(text):
        raise SourceTextChanged("The saved source-text offset is invalid.")
    if start == len(text):
        raise SourceTextChanged("No unseen source text remains in this representation.")
    if not 0 <= overlap < size:
        raise ValueError("Extraction overlap must be smaller than the source window.")
    input_start = max(0, start - overlap)
    end = min(len(text), input_start + size)
    if end < len(text):
        paragraph = text.rfind("\n\n", start + (end - start) // 2, end)
        if paragraph >= start:
            end = paragraph + 2
    if end <= start and start < len(text):
        raise ValueError("The source-text window must advance.")
    return {"start": start, "input_start": input_start, "end": end}


def retain_new_outputs(extracted: dict, progress: dict) -> None:
    """Suppress exact repeated outputs from overlapping text, preserving conflicts."""
    for field in (
        "events",
        "facts",
        "claims",
        "relationships",
        "fundamental_metrics",
        "market_setup_signals",
    ):
        seen = set(progress["seen"].get(field) or [])
        retained = []
        for item in extracted.get(field) or []:
            digest = sha256(
                json.dumps(item, sort_keys=True, ensure_ascii=False).encode()
            ).hexdigest()
            if digest not in seen:
                retained.append(item)
                seen.add(digest)
        extracted[field] = retained
        progress["seen"][field] = sorted(seen)


def complete_source_window(progress: dict, window: dict, extracted: dict) -> None:
    progress["windows"].append(
        {**window, "submitted_at": datetime.now(UTC).isoformat()}
    )
    progress["next_offset"] = window["end"]
    progress["summaries"].append(str(extracted.get("summary") or ""))
    progress["assessments"].append(extracted.get("relevance_assessment") or {})
    if "primary_subject" not in progress and (
        extracted.get("relevance_assessment") or {}
    ).get("status") in {"relevant", "adjacent"}:
        progress["primary_subject"] = {
            key: extracted.get(key)
            for key in ("primary_subject", "subject_type", "entity_type")
        }


def source_text_coverage(progress: dict | None) -> dict:
    if not progress:
        return {"status": "unknown", "representation": "stored_utf8_text"}
    covered = int(progress.get("next_offset") or 0)
    total = int(progress.get("document_chars") or 0)
    return {
        "status": "all_text_submitted" if covered >= total else "partial",
        "representation": progress.get("representation", "stored_utf8_text"),
        "coordinate_unit": "unicode_codepoints",
        "document_hash": progress["document_hash"],
        "document_chars": total,
        "submitted_chars": covered,
        "remaining_chars": max(0, total - covered),
        "windows": list(progress.get("windows") or []),
        "meaning": "Text submitted to structured extraction; not proof of complete fact recall or whole-document reconciliation.",
    }
