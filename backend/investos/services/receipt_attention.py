"""Order identity holds by attributable ledger relevance, never resolve them."""

from uuid import UUID

from sqlalchemy import select

from investos.models.evidence import RawEvidence
from investos.models.portfolio import Transaction
from investos.models.review import ReviewQueueItem

FINANCIAL_DOCUMENT_TYPES = frozenset(
    {
        "order_confirmation",
        "dividend_notice",
        "cash_activity",
        "corporate_action",
        "account_transfer",
    }
)
HOLD_REASON = "Identity remains unverified; compare original receipts before posting."


def attention(evidence_rows, linked_ids, *, missing_candidate=False):
    """Classification is provisional attention evidence, not identity proof."""
    if linked_ids:
        return 100.0, "Current or candidate receipt is linked to ledger activity."
    classifications = []
    truncated = False
    for evidence in evidence_rows:
        metadata = evidence.metadata_json or {}
        truncated = truncated or bool(
            (metadata.get("attention_classification") or {}).get("input_truncated")
        )
        # Both keys have been used by the normal mailbox classifier.
        values = [
            metadata[key]
            for key in ("mailbox_classification", "classification")
            if isinstance(metadata.get(key), dict)
        ]
        classifications.extend(values or [{}])
    kinds = {row.get("document_type") for row in classifications}
    if kinds & FINANCIAL_DOCUMENT_TYPES:
        return (
            95.0,
            "Stored current/candidate classification indicates financial activity (provisional).",
        )
    # A contradictory newsletter extraction must never be demoted.
    if any(
        row.get("ticker")
        or row.get("action")
        or row.get("quantity")
        or row.get("price")
        for row in classifications
    ):
        return (
            95.0,
            "Classification includes possible financial fields; inspect the uncertainty.",
        )
    if (
        classifications
        and kinds == {"newsletter"}
        and not missing_candidate
        and not truncated
    ):
        # The existing confidence measures transaction confidence: a newsletter
        # correctly has confidence zero. It is not a document-confidence score.
        return (
            30.0,
            "Stored current/candidate classifications indicate correspondence (provisional).",
        )
    return (
        80.0,
        "Document relevance is uncertain: classification or candidate context is missing, ambiguous or incomplete.",
    )


class ReceiptAttentionService:
    def __init__(self, session):
        self.session = session

    async def refresh(self, *, evidence_id: UUID | None = None):
        conditions = [
            ReviewQueueItem.item_type == "mailbox_identity",
            ReviewQueueItem.status.in_(["pending", "in_review"]),
        ]
        if evidence_id is not None:
            conditions.append(ReviewQueueItem.item_id == evidence_id)
        items = list(
            (
                await self.session.scalars(select(ReviewQueueItem).where(*conditions))
            ).all()
        )
        if not items:
            return
        # Batch the current receipts, their candidate receipts, and ledger links.
        current_ids = {item.evidence_id or item.item_id for item in items}
        current = await self._evidence(current_ids)
        candidate_ids = {}
        malformed = set()
        for key, row in current.items():
            candidate_ids[key] = set()
            for value in (
                (row.metadata_json or {})
                .get("identity_review", {})
                .get("candidate_evidence_ids", [])
            ):
                try:
                    candidate_ids[key].add(UUID(str(value)))
                except (ValueError, TypeError, AttributeError):
                    malformed.add(key)
        all_ids = current_ids | {
            key for values in candidate_ids.values() for key in values
        }
        rows = {**current, **await self._evidence(all_ids - current_ids)}
        linked = set()
        values = [str(key) for key in all_ids]
        for offset in range(0, len(values), 1000):
            linked.update(
                (
                    await self.session.scalars(
                        select(Transaction.provenance_json["raw_evidence_id"].astext)
                        .where(
                            Transaction.provenance_json["raw_evidence_id"].astext.in_(
                                values[offset : offset + 1000]
                            )
                        )
                        .distinct()
                    )
                ).all()
            )
        for item in items:
            key = item.evidence_id or item.item_id
            current_row = current.get(key)
            if (
                current_row is None
                or (current_row.metadata_json or {})
                .get("identity_review", {})
                .get("status")
                != "pending"
            ):
                # An orphan/legacy concern is still retained, but cannot be
                # rewritten as if a current pending identity receipt was found.
                continue
            ids = {key} | candidate_ids.get(key, set())
            score, reason = attention(
                [rows[value] for value in ids if value in rows],
                {str(value) for value in ids} & linked,
                missing_candidate=bool(ids - rows.keys()) or key in malformed,
            )
            item.priority_score = score
            item.trigger_reason = f"{reason} {HOLD_REASON}"
        await self.session.flush()

    async def _evidence(self, ids):
        result = {}
        values = list(ids)
        for offset in range(0, len(values), 1000):
            result.update(
                {
                    row.id: row
                    for row in (
                        await self.session.scalars(
                            select(RawEvidence).where(
                                RawEvidence.id.in_(values[offset : offset + 1000])
                            )
                        )
                    ).all()
                }
            )
        return result
