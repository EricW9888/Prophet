import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import ARRAY, DateTime, Float, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, utcnow


class GraphNodeLayout(Base):
    """Cached force-directed layout coordinates for graph nodes."""

    __tablename__ = "graph_node_layouts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    node_key: Mapped[str] = mapped_column(String, unique=True, index=True)
    x: Mapped[float] = mapped_column(Float)
    y: Mapped[float] = mapped_column(Float)
    vx: Mapped[float] = mapped_column(Float, default=0.0)
    vy: Mapped[float] = mapped_column(Float, default=0.0)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class Edge(Base):
    """Explicit relationship between exactly two nodes in the system."""

    __tablename__ = "edges"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    source_type: Mapped[str] = mapped_column(
        String, index=True
    )  # fact|claim|event|entity|security|theme
    source_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)

    target_type: Mapped[str] = mapped_column(String, index=True)
    target_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)

    relationship_type: Mapped[str] = mapped_column(String, index=True)
    # Types like: supports|contradicts|updates|causes|implies|mentions|competes_with|depends_on

    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    reasoning: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    properties_json: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class GraphTraversalSet(Base):
    """Cached result of an expensive graph query, used for layered retrieval."""

    __tablename__ = "graph_traversal_sets"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    root_node_type: Mapped[str] = mapped_column(String)
    root_node_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))

    traversal_depth: Mapped[int] = mapped_column(Integer)
    filter_params_json: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)

    node_ids: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(UUID(as_uuid=True)))
    edge_ids: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(UUID(as_uuid=True)))

    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    expires_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
