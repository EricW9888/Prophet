"""Bind derived assertions to inspectable text, without claiming entailment."""

import re
from hashlib import sha256
from types import SimpleNamespace
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from investos.models.evidence import SourceItem
from investos.models.knowledge_mutation import KnowledgeMutation


def text_digest(text: str) -> str:
    return sha256(text.encode("utf-8")).hexdigest()


def bind_source_passage(
    payload: dict,
    statement: str,
    text: str,
    *,
    source_item: SourceItem,
    window: dict | None = None,
    document_hash: str | None = None,
) -> dict:
    quote = payload.get("supporting_quote")
    base = {
        "version": 1,
        "status": "unbound",
        "assertion_hash": text_digest(statement),
        "source_item_id": str(source_item.id),
        "raw_evidence_id": str(source_item.raw_evidence_id),
        "document_hash": document_hash or text_digest(text),
        "coordinate_unit": "unicode_codepoints",
        "semantic_verification": False,
        "proposed_quote": quote if isinstance(quote, str) else None,
    }
    if not isinstance(quote, str) or not quote.strip():
        return {**base, "reason": "No supporting quotation was supplied."}
    start = (window or {}).get("input_start", 0)
    end = (window or {}).get("end", len(text))
    # Only whitespace may differ. Restore the exact stored spelling and offsets.
    pattern = r"\s+".join(re.escape(word) for word in quote.split())
    matches = re.finditer(pattern, text[start:end])
    match = next(matches, None)
    if match is None or next(matches, None) is not None:
        return {
            **base,
            "reason": "The passage could not be located uniquely in the submitted text.",
        }
    absolute_start, absolute_end = start + match.start(), start + match.end()
    return {
        **base,
        "status": "bound",
        "exact": text[absolute_start:absolute_end],
        "start": absolute_start,
        "end": absolute_end,
        "source_window": {"start": start, "end": end},
    }


def project_source_passage(
    binding: dict | None,
    statement: str,
    item: SourceItem | None,
    *,
    context_chars: int = 320,
    document_hash: str | None = None,
) -> dict:
    if not binding:
        return {
            "status": "unknown",
            "reason": "No source passage was retained for this record.",
        }
    if binding.get("version") != 1:
        return {
            "status": "unbound",
            "reason": "The source locator version is unsupported.",
        }
    if binding.get("status") != "bound":
        return {"status": "unbound", "reason": binding.get("reason")}
    if binding.get("assertion_hash") != text_digest(statement):
        return {
            "status": "assertion_changed",
            "reason": "The assertion changed after its quotation was recorded.",
        }
    text = (item.extracted_text or "") if item else ""
    start, end = binding.get("start"), binding.get("end")
    if (
        item is None
        or str(item.id) != binding.get("source_item_id")
        or str(item.raw_evidence_id) != binding.get("raw_evidence_id")
        or (document_hash or text_digest(text)) != binding.get("document_hash")
        or isinstance(start, bool)
        or isinstance(end, bool)
        or not isinstance(start, int)
        or not isinstance(end, int)
        or not 0 <= start < end <= len(text)
        or text[start:end] != binding.get("exact")
    ):
        return {
            "status": "source_changed",
            "reason": "The retained source no longer matches the recorded quotation.",
        }
    context_start = max(0, start - context_chars)
    context_end = min(len(text), end + context_chars)
    return {
        "status": "bound",
        "source_item_id": str(item.id),
        "raw_evidence_id": str(item.raw_evidence_id),
        "document_hash": binding["document_hash"],
        "assertion_hash": binding["assertion_hash"],
        "coordinate_unit": "unicode_codepoints",
        "quote_selector": {
            "type": "TextQuoteSelector",
            "exact": text[start:end],
            "prefix": text[context_start:start],
            "suffix": text[end:context_end],
        },
        "position_selector": {
            "type": "TextPositionSelector",
            "start": start,
            "end": end,
        },
        "context_start": context_start,
        "context_end": context_end,
        "document_chars": len(text),
        "context_is_excerpt": context_start > 0 or context_end < len(text),
        "semantic_verification": False,
    }


class SourcePassageService:
    def __init__(self, session: AsyncSession):
        self.session = session

    @staticmethod
    def _item_id(binding: dict | None) -> UUID | None:
        try:
            return UUID(str((binding or {}).get("source_item_id")))
        except (ValueError, TypeError, AttributeError):
            return None

    async def creation_bindings(self, node_ids: list[UUID]) -> dict:
        creations = await self.session.execute(
            select(
                KnowledgeMutation.node_type,
                KnowledgeMutation.node_id,
                KnowledgeMutation.metadata_json["source_passage"],
            )
            .where(
                KnowledgeMutation.node_id.in_(node_ids),
                KnowledgeMutation.change_type == "created",
            )
            .order_by(KnowledgeMutation.created_at.desc(), KnowledgeMutation.id.desc())
        )
        bindings = {}
        for kind, node_id, passage in creations:
            bindings.setdefault((kind, str(node_id)), passage)
        return bindings

    async def attach(
        self, nodes: list[dict], *, items: dict[UUID, SourceItem] | None = None
    ) -> None:
        eligible = [
            node for node in nodes if node.get("type") in {"fact", "claim", "event"}
        ]
        if not eligible:
            return
        bindings = await self.creation_bindings(
            [UUID(str(node["id"])) for node in eligible]
        )
        retained = dict(items or {})
        needed = {
            self._item_id(binding)
            for binding in bindings.values()
            if isinstance(binding, dict)
            and binding.get("status") == "bound"
            and self._item_id(binding) is not None
        } - retained.keys()
        if needed:
            retained.update(
                {
                    item.id: item
                    for item in await self.session.scalars(
                        select(SourceItem).where(SourceItem.id.in_(needed))
                    )
                }
            )
        digests = {
            item_id: text_digest(item.extracted_text or "")
            for item_id, item in retained.items()
        }
        # Historical repair may have inspected the original after a legacy prefix
        # was stored in SourceItem. Reuse the original-text reader and hash check.
        from investos.services.translation import TranslationService

        for item_id, item in list(retained.items()):
            expected = {
                binding.get("document_hash")
                for binding in bindings.values()
                if isinstance(binding, dict)
                and binding.get("source_item_id") == str(item_id)
                and binding.get("status") == "bound"
            }
            if expected and digests[item_id] not in expected:
                try:
                    evidence, content, digest = await TranslationService(
                        self.session
                    ).original(item.raw_evidence_id)
                except (LookupError, ValueError):
                    continue
                if digest in expected:
                    retained[item_id] = SimpleNamespace(
                        id=item.id, raw_evidence_id=evidence.id, extracted_text=content
                    )
                    digests[item_id] = digest
        for node in eligible:
            binding = bindings.get((node["type"], str(node["id"])))
            item_id = self._item_id(binding)
            source_ids = {
                str(source.get("source_item_id"))
                for source in [node.get("source"), *(node.get("sources") or [])]
                if isinstance(source, dict)
            }
            if binding and item_id is not None and str(item_id) not in source_ids:
                node["source_passage"] = {
                    "status": "source_changed",
                    "reason": "The assertion's source association changed.",
                }
            else:
                node["source_passage"] = project_source_passage(
                    binding,
                    str(node.get("text") or ""),
                    retained.get(item_id),
                    document_hash=digests.get(item_id),
                )
