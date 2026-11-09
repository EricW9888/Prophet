from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from investos.models.graph import Edge
from investos.services.knowledge_audit import KnowledgeAuditService


class GraphEdgeStateService:
    """Idempotent access layer for canonical explicit graph edges."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def retarget_node_edges(
        self,
        *,
        old_type: str,
        old_id: UUID,
        new_type: str,
        new_id: UUID,
        actor: str,
    ) -> int:
        """Move a subject's relationships without creating duplicate or self edges."""
        old_node = (old_type, old_id)
        new_node = (new_type, new_id)
        if old_node == new_node:
            return 0
        rows = (
            await self.session.scalars(
                select(Edge)
                .where(
                    or_(
                        (Edge.source_type == old_type) & (Edge.source_id == old_id),
                        (Edge.target_type == old_type) & (Edge.target_id == old_id),
                    )
                )
                .order_by(Edge.created_at, Edge.id)
                .with_for_update()
            )
        ).all()
        audit = KnowledgeAuditService(self.session)
        for edge in rows:
            edge_id = edge.id
            original_source = (edge.source_type, edge.source_id)
            original_target = (edge.target_type, edge.target_id)
            source = new_node if original_source == old_node else original_source
            target = new_node if original_target == old_node else original_target
            confidence = float(edge.confidence or 0)
            reasoning = edge.reasoning
            properties = dict(edge.properties_json or {})
            before = {
                "source_type": original_source[0],
                "source_id": str(original_source[1]),
                "target_type": original_target[0],
                "target_id": str(original_target[1]),
                "relationship_type": edge.relationship_type,
                "confidence": confidence,
                "reasoning": reasoning,
                "properties": properties,
            }
            canonical_id = None
            if source == target:
                outcome = "collapsed_self_relationship"
                await self.session.delete(edge)
            else:
                key = dict(
                    source_type=source[0],
                    source_id=source[1],
                    target_type=target[0],
                    target_id=target[1],
                    relationship_type=edge.relationship_type,
                )
                canonical = await self._get_edge(**key, for_update=True)
                if canonical is None:
                    try:
                        async with self.session.begin_nested():
                            for field, value in key.items():
                                setattr(edge, field, value)
                            await self.session.flush()
                    except IntegrityError:
                        canonical = await self._get_edge(**key, for_update=True)
                        if canonical is None:
                            raise
                if canonical is None:
                    outcome = "retargeted"
                    canonical_id = edge_id
                else:
                    outcome = "merged_relationship"
                    canonical_id = canonical.id
                    previous_confidence = float(canonical.confidence or 0)
                    if confidence > previous_confidence:
                        canonical.confidence = confidence
                    if reasoning and (
                        not canonical.reasoning or confidence > previous_confidence
                    ):
                        canonical.reasoning = reasoning
                    canonical.properties_json = {
                        **properties,
                        **(canonical.properties_json or {}),
                    } or None
                    await self.session.delete(edge)
            await audit.record_change(
                node_type="edge",
                node_id=edge_id,
                change_type=outcome,
                reason="Consolidated graph relationships after a subject identity change.",
                actor=actor,
                source_type=old_type,
                source_id=old_id,
                subject_type=new_type,
                subject_id=new_id,
                metadata={
                    "before": before,
                    "canonical_edge_id": str(canonical_id) if canonical_id else None,
                },
            )
        return len(rows)

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
        for_update: bool = False,
    ) -> Edge | None:
        statement = select(Edge).where(
            Edge.source_type == source_type,
            Edge.source_id == source_id,
            Edge.target_type == target_type,
            Edge.target_id == target_id,
            Edge.relationship_type == relationship_type,
        )
        if for_update:
            statement = statement.with_for_update().execution_options(
                populate_existing=True
            )
        return (await self.session.execute(statement)).scalar_one_or_none()

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
