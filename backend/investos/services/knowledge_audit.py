from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from investos.models.knowledge_mutation import KnowledgeMutation


class KnowledgeAuditService:
    """Append-only lifecycle events for knowledge and graph nodes.

    Facts, claims, events, edges, and canonical state rows are the user's
    durable research memory. When the graph grows or shrinks, this service
    records the why/when/source instead of making the UI infer history from the
    current row state.
    """

    def __init__(self, session: AsyncSession):
        self.session = session

    async def record_change(
        self,
        *,
        node_type: str,
        node_id: UUID,
        change_type: str,
        reason: str | None = None,
        actor: str = "system",
        source_type: str | None = None,
        source_id: UUID | None = None,
        subject_type: str | None = None,
        subject_id: UUID | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> KnowledgeMutation:
        event = KnowledgeMutation(
            node_type=node_type,
            node_id=node_id,
            change_type=change_type,
            reason=reason,
            actor=actor,
            source_type=source_type,
            source_id=source_id,
            subject_type=subject_type,
            subject_id=subject_id,
            metadata_json=metadata or None,
        )
        self.session.add(event)
        await self.session.flush()
        return event
