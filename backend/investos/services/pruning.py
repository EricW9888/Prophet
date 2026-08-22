import logging
from typing import Any
from uuid import UUID

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from investos.core.llm import call_llm_json
from investos.models.graph import Edge
from investos.models.knowledge import Claim, Event, Fact
from investos.services.knowledge_audit import KnowledgeAuditService
from investos.services.operating_state import OperatingStateService
from investos.workers.coverage import CoverageWorker

logger = logging.getLogger(__name__)

PRUNING_EVALUATION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "pruned_nodes": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "node_id": {"type": "string"},
                    "node_type": {"type": "string", "enum": ["fact", "claim", "event"]},
                    "category": {
                        "type": "string",
                        "enum": [
                            "duplicate",
                            "internal_telemetry",
                            "superseded",
                            "proven_false",
                            "trivial",
                        ],
                    },
                    "confidence": {"type": "number"},
                    "reason": {"type": "string"},
                },
                "required": [
                    "node_id",
                    "node_type",
                    "category",
                    "confidence",
                    "reason",
                ],
            },
        },
        "summary": {"type": "string"},
    },
    "required": ["pruned_nodes", "summary"],
}

SAFE_PRUNE_CATEGORIES = {
    "duplicate",
    "internal_telemetry",
    "superseded",
    "proven_false",
    "trivial",
}
AUTO_PRUNE_CONFIDENCE_FLOOR = 0.86
AUTO_PRUNE_MAX_COUNT = 6
AUTO_PRUNE_MAX_RATIO = 0.12
KNOWLEDGE_NODE_MODELS = {
    "fact": Fact,
    "claim": Claim,
    "event": Event,
}


class PruningService:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def restore_knowledge_node(
        self,
        node_type: str,
        node_id: UUID,
        *,
        reason: str | None = None,
        actor: str = "user",
    ) -> dict[str, Any]:
        model = self._knowledge_model_for_type(node_type)
        if model is None:
            raise ValueError("node_type must be fact, claim, or event")
        node = await self.session.get(model, node_id)
        if node is None:
            return {"restored": False, "reason": "not_found"}
        if not bool(getattr(node, "is_deprecated", False)):
            return {"restored": False, "reason": "already_active"}

        previous_reason = getattr(node, "deprecated_reason", None)
        node.is_deprecated = False
        node.deprecated_reason = None
        if hasattr(node, "mark_updated"):
            node.mark_updated()

        clean_node_type = node_type.strip().lower()
        subject_type, subject_id = await self._first_subject_for_node(
            clean_node_type, node_id
        )
        restore_reason = reason or f"Restored previously deprecated {clean_node_type}."
        await KnowledgeAuditService(self.session).record_change(
            node_type=clean_node_type,
            node_id=node_id,
            change_type="restored",
            reason=restore_reason,
            actor=actor,
            subject_type=subject_type,
            subject_id=subject_id,
            metadata={
                "previous_deprecated_reason": previous_reason,
                "label": self._node_label(node),
            },
        )
        await self.session.flush()
        if subject_type in {"entity", "theme"} and subject_id is not None:
            subject_name = await OperatingStateService(self.session).subject_name(
                subject_id, subject_type
            )
            await CoverageWorker(self.session).audit_subject_coverage(
                subject_id=subject_id,
                subject_type=subject_type,
                subject_name=subject_name,
            )
        await self.session.commit()
        return {
            "restored": True,
            "node_type": clean_node_type,
            "node_id": str(node_id),
            "previous_deprecated_reason": previous_reason,
            "reason": restore_reason,
        }

    @staticmethod
    def _knowledge_model_for_type(node_type: str):
        return KNOWLEDGE_NODE_MODELS.get(str(node_type or "").strip().lower())

    @staticmethod
    def _node_label(node) -> str:
        return str(
            getattr(node, "statement", None)
            or getattr(node, "title", None)
            or getattr(node, "id", "")
        )[:500]

    async def _first_subject_for_node(
        self, node_type: str, node_id: UUID
    ) -> tuple[str | None, UUID | None]:
        edge = (
            await self.session.execute(
                select(Edge)
                .where(
                    Edge.source_type == node_type,
                    Edge.source_id == node_id,
                    Edge.target_type.in_(["entity", "theme"]),
                )
                .order_by(desc(Edge.confidence))
                .limit(1)
            )
        ).scalar_one_or_none()
        if edge is None:
            return None, None
        return edge.target_type, edge.target_id

    async def prune_stale_knowledge(
        self,
        subject_id: UUID,
        subject_type: str,
    ) -> dict[str, Any]:
        """
        Identify and deprecate stale or irrelevant knowledge nodes for a subject.
        Returns pruning details metrics.
        """
        facts = (
            (
                await self.session.execute(
                    select(Fact)
                    .join(Edge, Edge.source_id == Fact.id)
                    .where(
                        Edge.target_id == subject_id,
                        Edge.target_type == subject_type,
                        Fact.is_deprecated.is_(False),
                    )
                )
            )
            .scalars()
            .all()
        )

        claims = (
            (
                await self.session.execute(
                    select(Claim)
                    .join(Edge, Edge.source_id == Claim.id)
                    .where(
                        Edge.target_id == subject_id,
                        Edge.target_type == subject_type,
                        Claim.is_deprecated.is_(False),
                    )
                )
            )
            .scalars()
            .all()
        )

        events = (
            (
                await self.session.execute(
                    select(Event)
                    .join(Edge, Edge.source_id == Event.id)
                    .where(
                        Edge.target_id == subject_id,
                        Edge.target_type == subject_type,
                        Event.is_deprecated.is_(False),
                    )
                )
            )
            .scalars()
            .all()
        )

        if not facts and not claims and not events:
            return {
                "pruned_count": 0,
                "detail": "No active knowledge nodes found for subject",
            }

        subject_name = await OperatingStateService(self.session).subject_name(
            subject_id, subject_type
        )

        # Identify nodes with contradiction roles
        contradiction_roots = [
            c
            for c in claims
            if c.contradiction_role in ("contradicts_consensus", "ambiguous")
        ]
        contradiction_roots.extend(
            [
                f
                for f in facts
                if f.contradiction_role in ("contradicts_consensus", "ambiguous")
            ]
        )

        nodes_payload = []
        for f in facts:
            is_root = f in contradiction_roots
            nodes_payload.append(
                {
                    "id": str(f.id),
                    "type": "fact",
                    "text": f.statement,
                    "tier": f.tier,
                    "created_at": f.created_at.isoformat(),
                    "contradiction_role": f.contradiction_role,
                    "is_contradiction_focus": is_root,
                }
            )
        for c in claims:
            is_root = c in contradiction_roots
            nodes_payload.append(
                {
                    "id": str(c.id),
                    "type": "claim",
                    "text": c.statement,
                    "tier": c.tier,
                    "created_at": c.created_at.isoformat(),
                    "contradiction_role": c.contradiction_role,
                    "is_contradiction_focus": is_root,
                }
            )
        for e in events:
            nodes_payload.append(
                {
                    "id": str(e.id),
                    "type": "event",
                    "text": e.title,
                    "event_type": e.event_type,
                    "created_at": e.created_at.isoformat(),
                    "contradiction_role": "neutral",
                    "is_contradiction_focus": False,
                }
            )

        # Cap length to bound LLM context. This is only the candidate review set;
        # pruning itself is guarded below and must remain conservative.
        nodes_payload = nodes_payload[:150]

        system_prompt = (
            "Evaluate whether any active knowledge nodes for the subject should be softly deprecated.\n"
            "Retain by default: the graph is allowed to keep useful history, unresolved contradictions, thin-coverage markers, "
            "and imperfect but source-backed research context.\n"
            "Propose deprecation only when the case is specific and high-confidence under one category: duplicate, "
            "internal_telemetry, superseded, proven_false, or trivial.\n"
            "Do not deprecate a node merely because it is old, broad, incomplete, bearish, bullish, or inconvenient to the current thesis.\n"
            "Do not use pruning to make the graph smaller. Events should be deprecated only when duplicated, trivial, or proven false.\n"
            "Return only nodes that clear this conservative bar, with a short user-facing reason and confidence from 0 to 1."
        )

        user_prompt = f"Subject: {subject_name}\nNodes:\n" + "\n".join(
            [f"- [{n['type']} {n['id']}] {n['text']}" for n in nodes_payload]
        )

        try:
            result = await call_llm_json(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                schema=PRUNING_EVALUATION_SCHEMA,
            )
        except Exception as e:
            logger.error(f"Error during pruning evaluation: {e}")
            return {"pruned_count": 0, "detail": f"LLM iteration failed: {e}"}

        pruned = result.get("pruned_nodes", [])
        if not pruned:
            return {"pruned_count": 0, "detail": "All nodes retained."}

        approved, rejected_count, review_required, guardrail_detail = (
            self._screen_prune_candidates(
                pruned,
                active_node_count=len(nodes_payload),
            )
        )
        if review_required:
            return {
                "pruned_count": 0,
                "proposed_count": len(pruned),
                "rejected_count": rejected_count,
                "review_required": True,
                "detail": guardrail_detail,
            }
        if not approved:
            return {
                "pruned_count": 0,
                "proposed_count": len(pruned),
                "rejected_count": rejected_count,
                "review_required": False,
                "detail": guardrail_detail
                or "No high-confidence prune candidates passed guardrails.",
            }

        pruned_count = 0
        fact_dict = {str(f.id): f for f in facts}
        claim_dict = {str(c.id): c for c in claims}
        event_dict = {str(e.id): e for e in events}
        audit = KnowledgeAuditService(self.session)

        for item in approved:
            nid = item.get("node_id")
            rtype = item.get("node_type")
            reason = f"{item.get('category')}: {item.get('reason', 'stale')}"
            metadata = {
                "category": item.get("category"),
                "confidence": item.get("confidence"),
                "subject_name": subject_name,
            }

            if rtype == "fact" and nid in fact_dict:
                fact_dict[nid].is_deprecated = True
                fact_dict[nid].deprecated_reason = reason
                await audit.record_change(
                    node_type="fact",
                    node_id=fact_dict[nid].id,
                    change_type="deprecated",
                    reason=reason,
                    actor="pruning_service",
                    subject_type=subject_type,
                    subject_id=subject_id,
                    metadata=metadata,
                )
                pruned_count += 1
            elif rtype == "claim" and nid in claim_dict:
                claim_dict[nid].is_deprecated = True
                claim_dict[nid].deprecated_reason = reason
                await audit.record_change(
                    node_type="claim",
                    node_id=claim_dict[nid].id,
                    change_type="deprecated",
                    reason=reason,
                    actor="pruning_service",
                    subject_type=subject_type,
                    subject_id=subject_id,
                    metadata=metadata,
                )
                pruned_count += 1
            elif rtype == "event" and nid in event_dict:
                event_dict[nid].is_deprecated = True
                event_dict[nid].deprecated_reason = reason
                await audit.record_change(
                    node_type="event",
                    node_id=event_dict[nid].id,
                    change_type="deprecated",
                    reason=reason,
                    actor="pruning_service",
                    subject_type=subject_type,
                    subject_id=subject_id,
                    metadata=metadata,
                )
                pruned_count += 1

        if pruned_count > 0:
            await self.session.flush()
            # Update the subject coverage constraints
            await CoverageWorker(self.session).audit_subject_coverage(
                subject_id=subject_id,
                subject_type=subject_type,
                subject_name=subject_name,
            )
            await self.session.commit()

        detail = str(result.get("summary", "") or "").strip()
        if rejected_count:
            suffix = f"{rejected_count} proposed prune candidate(s) were rejected by guardrails."
            detail = f"{detail} {suffix}".strip()
        return {
            "pruned_count": pruned_count,
            "proposed_count": len(pruned),
            "rejected_count": rejected_count,
            "review_required": False,
            "detail": detail,
        }

    @classmethod
    def _screen_prune_candidates(
        cls,
        pruned_nodes: list[dict[str, Any]],
        *,
        active_node_count: int,
    ) -> tuple[list[dict[str, Any]], int, bool, str]:
        approved: list[dict[str, Any]] = []
        rejected_count = 0
        for item in pruned_nodes:
            category = str(item.get("category") or "").strip()
            try:
                confidence = float(item.get("confidence") or 0.0)
            except (TypeError, ValueError):
                confidence = 0.0
            if (
                category not in SAFE_PRUNE_CATEGORIES
                or confidence < AUTO_PRUNE_CONFIDENCE_FLOOR
            ):
                rejected_count += 1
                continue
            approved.append(item)

        max_allowed = max(
            1,
            min(
                AUTO_PRUNE_MAX_COUNT, int(active_node_count * AUTO_PRUNE_MAX_RATIO) or 1
            ),
        )
        if len(approved) > max_allowed:
            return (
                [],
                len(pruned_nodes),
                True,
                (
                    f"Pruning proposed {len(approved)} high-confidence changes, above the automatic limit "
                    f"of {max_allowed}; review required before soft-deprecating knowledge."
                ),
            )
        if not approved:
            return (
                [],
                rejected_count,
                False,
                "No high-confidence prune candidates passed guardrails.",
            )
        return approved, rejected_count, False, ""
