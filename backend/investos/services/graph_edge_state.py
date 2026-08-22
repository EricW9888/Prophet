from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from investos.models.graph import Edge
from investos.services.knowledge_audit import KnowledgeAuditService


class GraphEdgeStateService:
    """Idempotent access layer for canonical explicit graph edges."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def ensure_edge(
        self,
        *,
        source_type: str,
        source_id: UUID,
        target_type: str,
        target_id: UUID,
        relationship_type: str,
        confidence: float = 1.0,
        reasoning: str | None = None,
        properties: dict[str, Any] | None = None,
    ) -> tuple[Edge, bool]:
        origin = str((properties or {}).get("origin") or "graph_edge_state")
        existing = await self._get_edge(
            source_type=source_type,
            source_id=source_id,
            target_type=target_type,
            target_id=target_id,
            relationship_type=relationship_type,
        )
        merged_properties = dict(properties or {})
        if existing is not None:
            current_confidence = float(existing.confidence or 0.0)
            changed = False
            if confidence > current_confidence:
                existing.confidence = confidence
                changed = True
            if reasoning and (
                not existing.reasoning or confidence >= current_confidence
            ):
                changed = changed or existing.reasoning != reasoning
                existing.reasoning = reasoning
            if merged_properties:
                next_properties = {
                    **(existing.properties_json or {}),
                    **merged_properties,
                }
                changed = changed or next_properties != (existing.properties_json or {})
                existing.properties_json = next_properties
            if changed:
                await self._record_edge_change(
                    existing,
                    change_type="updated",
                    actor=origin,
                    reason=reasoning or "Explicit graph edge updated.",
                )
            return existing, False

        edge = Edge(
            source_type=source_type,
            source_id=source_id,
            target_type=target_type,
            target_id=target_id,
            relationship_type=relationship_type,
            confidence=confidence,
            reasoning=reasoning,
            properties_json=merged_properties or None,
        )
        try:
            async with self.session.begin_nested():
                self.session.add(edge)
                await self.session.flush()
                await self._record_edge_change(
                    edge,
                    change_type="created",
                    actor=origin,
                    reason=reasoning or "Explicit graph edge created.",
                )
            return edge, True
        except IntegrityError:
            existing = await self._get_edge(
                source_type=source_type,
                source_id=source_id,
                target_type=target_type,
                target_id=target_id,
                relationship_type=relationship_type,
            )
            if existing is None:
                raise
            current_confidence = float(existing.confidence or 0.0)
            changed = False
            if confidence > current_confidence:
                existing.confidence = confidence
                changed = True
            if reasoning and (
                not existing.reasoning or confidence >= current_confidence
            ):
                changed = changed or existing.reasoning != reasoning
                existing.reasoning = reasoning
            if merged_properties:
                next_properties = {
                    **(existing.properties_json or {}),
                    **merged_properties,
                }
                changed = changed or next_properties != (existing.properties_json or {})
                existing.properties_json = next_properties
            await self.session.flush()
            if changed:
                await self._record_edge_change(
                    existing,
                    change_type="updated",
                    actor=origin,
                    reason=reasoning
                    or "Explicit graph edge updated after concurrent insert.",
                )
            return existing, False

    async def _get_edge(
        self,
        *,
        source_type: str,
        source_id: UUID,
        target_type: str,
        target_id: UUID,
        relationship_type: str,
    ) -> Edge | None:
        return (
            await self.session.execute(
                select(Edge).where(
                    Edge.source_type == source_type,
                    Edge.source_id == source_id,
                    Edge.target_type == target_type,
                    Edge.target_id == target_id,
                    Edge.relationship_type == relationship_type,
                )
            )
        ).scalar_one_or_none()

    async def _record_edge_change(
        self,
        edge: Edge,
        *,
        change_type: str,
        actor: str,
        reason: str | None,
    ) -> None:
        await KnowledgeAuditService(self.session).record_change(
            node_type="edge",
            node_id=edge.id,
            change_type=change_type,
            reason=reason,
            actor=actor,
            subject_type=edge.source_type,
            subject_id=edge.source_id,
            metadata={
                "source_type": edge.source_type,
                "source_id": str(edge.source_id),
                "target_type": edge.target_type,
                "target_id": str(edge.target_id),
                "relationship_type": edge.relationship_type,
                "confidence": float(edge.confidence or 0.0),
                "properties": edge.properties_json or {},
            },
        )
