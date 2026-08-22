from __future__ import annotations

import re
import uuid
from collections import OrderedDict
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import desc, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from investos.models.catalog import (
    HistoricalEpisode,
    SourceProfile,
    SourceTrustProfile,
    SourceValueProfile,
)
from investos.models.conclusion import ConclusionState
from investos.models.coverage import CoverageMap, UnresolvedQuestion
from investos.models.entity import Entity, Security
from investos.models.evidence import RawEvidence, SourceItem
from investos.models.fundamental import FundamentalMetric
from investos.models.graph import Edge
from investos.models.knowledge import Claim, Event, Fact
from investos.models.lesson import Lesson, LessonObservation
from investos.models.market_setup import MarketSetupSignal
from investos.models.portfolio import Position
from investos.models.profile import Profile
from investos.models.review import ReviewQueueItem
from investos.models.shadow import ExperimentResult, ShadowAction, ShadowExperiment
from investos.models.source import Source
from investos.models.theme import Theme
from investos.schemas.graph import (
    GraphCitationResponse,
    GraphConnectionResponse,
    GraphNeighborhoodResponse,
    GraphNodeDetailResponse,
    GraphNodeLayoutItem,
    GraphRelationResponse,
    GraphSearchResultResponse,
    GraphWebEdgeResponse,
    GraphWebNodeResponse,
)
from investos.services.canonical_state import CanonicalStateService
from investos.services.graph_registry import durable_graph_model_map
from investos.services.source import SourceService

CONVERSATION_SOURCE_NAME = "Prophet Agent Conversation"
SYNTHETIC_PORTFOLIO_ID = UUID("00000000-0000-0000-0000-000000000000")

# Internal job/run labels that leaked into the entity table as if they were
# real subjects. They are pipeline bookkeeping, not knowledge, and should never
# surface as graph nodes.
_ARTIFACT_LABEL_RE = re.compile(
    r"^\s*(auto research|autonomous reflection|autonomous discovery)\s*:",
    re.IGNORECASE,
)
_RECLASSIFIED_THEME_DESCRIPTION = (
    "Reclassified from a generic topic label that was previously stored as an entity."
)


def _is_artifact_label(label: str | None) -> bool:
    return bool(label) and bool(_ARTIFACT_LABEL_RE.match(label or ""))


class GraphService:
    def __init__(self, session: AsyncSession):
        self.session = session
        self.canonical = CanonicalStateService(session)

    async def get_node_detail(
        self, *, node_type: str, node_id: UUID | str
    ) -> GraphNodeDetailResponse | None:
        node = await self._load_node(node_type, node_id)
        if node is None:
            return None
        citations = await self._citations_for_node(node_type, node)
        connections = await self._connections_for_node(
            node_type=node_type, node_id=node_id
        )
        attachment = await self._portfolio_attachment_summary(
            node_type=node_type,
            node_id=node_id,
            node=node,
            connections=connections,
        )
        relevance = self._calculate_relevance(node_type, node, attachment)
        return GraphNodeDetailResponse(
            id=node_id,
            node_type=node_type,
            label=self._node_label(node_type, node),
            layer=await self._node_layer(node_type, node),
            body=self._node_body(node_type, node),
            tier=getattr(node, "tier", None),
            created_at=getattr(node, "created_at", None),
            is_autonomous=await self._node_is_autonomous(node_type, node),
            properties=await self._node_properties(
                node_type, node_id, node, attachment
            ),
            relevance=relevance,
            relevance_reasoning=self._generate_relevance_reasoning(
                node_type,
                node,
                relevance,
                attachment,
                connections=connections,
                citations=citations,
            ),
            citations=citations,
            connections=connections,
        )

    async def search_nodes(
        self,
        *,
        query: str,
        limit: int = 20,
    ) -> list[GraphSearchResultResponse]:
        """Search durable graph nodes through their user-facing text fields."""
        search = " ".join((query or "").split())
        if len(search) < 2:
            return []

        capped_limit = max(1, min(limit, 50))
        pattern = f"%{search}%"
        definitions = (
            (
                "market_setup_signal",
                MarketSetupSignal,
                (
                    MarketSetupSignal.signal_name,
                    MarketSetupSignal.setup_context,
                    MarketSetupSignal.investment_relevance,
                ),
                (),
            ),
            (
                "fundamental_metric",
                FundamentalMetric,
                (
                    FundamentalMetric.metric_name,
                    FundamentalMetric.value_text,
                    FundamentalMetric.investment_relevance,
                ),
                (),
            ),
            ("fact", Fact, (Fact.statement,), (Fact.is_deprecated.is_(False),)),
            ("claim", Claim, (Claim.statement,), (Claim.is_deprecated.is_(False),)),
            (
                "event",
                Event,
                (Event.title, Event.description),
                (Event.is_deprecated.is_(False),),
            ),
            ("entity", Entity, (Entity.name, Entity.description), ()),
            ("theme", Theme, (Theme.name, Theme.description), ()),
            (
                "unresolved_question",
                UnresolvedQuestion,
                (UnresolvedQuestion.question_text,),
                (UnresolvedQuestion.status != "obsolete",),
            ),
            (
                "conclusion",
                ConclusionState,
                (ConclusionState.current_thesis_summary,),
                (),
            ),
            ("lesson", Lesson, (Lesson.title, Lesson.summary), ()),
            (
                "historical_episode",
                HistoricalEpisode,
                (
                    HistoricalEpisode.name,
                    HistoricalEpisode.description,
                    HistoricalEpisode.dominant_channel,
                    HistoricalEpisode.notes,
                ),
                (),
            ),
            ("source_item", SourceItem, (SourceItem.summary,), ()),
            ("raw_evidence", RawEvidence, (RawEvidence.title,), ()),
            ("source", Source, (Source.name,), ()),
            ("shadow_experiment", ShadowExperiment, (ShadowExperiment.name,), ()),
        )

        candidates: list[tuple[int, datetime, str, object]] = []
        normalized_search = search.casefold()
        for node_type, model, text_fields, filters in definitions:
            statement = select(model).where(
                or_(*(field.ilike(pattern) for field in text_fields)),
                *filters,
            )
            created_at = getattr(model, "created_at", None)
            if created_at is not None:
                statement = statement.order_by(desc(created_at))
            rows = (
                (await self.session.execute(statement.limit(capped_limit)))
                .scalars()
                .all()
            )
            for node in rows:
                label = self._node_label(node_type, node)
                normalized_label = " ".join(label.casefold().split())
                label_rank = (
                    0
                    if normalized_label == normalized_search
                    else (
                        1
                        if normalized_label.startswith(normalized_search)
                        else 2 if normalized_search in normalized_label else 3
                    )
                )
                candidates.append(
                    (
                        label_rank,
                        getattr(node, "created_at", None)
                        or datetime.min.replace(tzinfo=UTC),
                        node_type,
                        node,
                    )
                )

        candidates.sort(key=lambda item: (item[0], -item[1].timestamp()))
        results: list[GraphSearchResultResponse] = []
        seen: set[str] = set()
        for _rank, created_at, node_type, node in candidates:
            node_id = getattr(node, "id", None)
            key = self._node_key(node_type, node_id)
            if node_id is None or key in seen:
                continue
            seen.add(key)
            results.append(
                GraphSearchResultResponse(
                    node_type=node_type,
                    node_id=node_id,
                    label=self._node_label(node_type, node),
                    subtitle=self._node_subtitle(node_type, node),
                    layer=await self._node_layer(node_type, node),
                    created_at=created_at,
                )
            )
            if len(results) >= capped_limit:
                break
        return results

    async def get_neighborhood(
        self,
        *,
        node_type: str,
        node_id: UUID | str,
        depth: int = 3,
        limit: int = 60,
        include_system_nodes: bool = False,
    ) -> GraphNeighborhoodResponse | None:
        root = await self._load_node(node_type, node_id)
        if root is None:
            return None
        root_key = self._node_key(node_type, node_id)
        node_map: OrderedDict[str, GraphWebNodeResponse] = OrderedDict()
        node_map[root_key] = GraphWebNodeResponse(
            key=root_key,
            id=node_id,
            node_type=node_type,
            label=self._node_label(node_type, root),
            layer=await self._node_layer(node_type, root),
            subtitle=self._node_subtitle(node_type, root),
            tier=getattr(root, "tier", None),
            created_at=getattr(root, "created_at", None),
            is_root=True,
            is_autonomous=await self._node_is_autonomous(node_type, root),
        )
        edge_map: OrderedDict[UUID, GraphWebEdgeResponse] = OrderedDict()
        frontier: list[tuple[str, UUID]] = [(node_type, node_id)]
        effective_depth = max(1, min(depth, 4))
        for _level in range(effective_depth):
            next_frontier: list[tuple[str, UUID]] = []
            for current_type, current_id in frontier:
                current_node = await self._load_node(current_type, current_id)
                if current_node is None:
                    continue
                edges = await self._web_edges_touching_node(
                    node_type=current_type,
                    node_id=current_id,
                    node=current_node,
                )
                for edge in edges:
                    if edge.id not in edge_map:
                        edge_map[edge.id] = edge
                    other_type = (
                        edge.target_key.split(":", 1)[0]
                        if edge.source_key == self._node_key(current_type, current_id)
                        else edge.source_key.split(":", 1)[0]
                    )
                    try:
                        other_id = (
                            UUID(edge.target_key.split(":", 1)[1])
                            if edge.source_key
                            == self._node_key(current_type, current_id)
                            else UUID(edge.source_key.split(":", 1)[1])
                        )
                    except (ValueError, IndexError):
                        continue
                    other_key = self._node_key(other_type, other_id)
                    if other_key in node_map:
                        continue
                    if len(node_map) >= limit:
                        continue
                    other_node = await self._load_node(other_type, other_id)
                    if other_node is None:
                        continue

                    layer = await self._node_layer(other_type, other_node)
                    if not include_system_nodes and layer == "system":
                        continue

                    other_label = self._node_label(other_type, other_node)
                    if not include_system_nodes and _is_artifact_label(other_label):
                        continue

                    node_map[other_key] = GraphWebNodeResponse(
                        key=other_key,
                        id=other_id,
                        node_type=other_type,
                        label=other_label,
                        layer=layer,
                        subtitle=self._node_subtitle(other_type, other_node),
                        tier=getattr(other_node, "tier", None),
                        created_at=getattr(other_node, "created_at", None),
                        is_root=False,
                        is_autonomous=await self._node_is_autonomous(
                            other_type, other_node
                        ),
                    )
                    next_frontier.append((other_type, other_id))
            frontier = next_frontier
            if not frontier or len(node_map) >= limit:
                break

        node_keys = set(node_map.keys())
        for edge_id, edge in list(edge_map.items()):
            if edge.source_key not in node_keys or edge.target_key not in node_keys:
                edge_map.pop(edge_id, None)

        await self._attach_layouts(node_map)

        return GraphNeighborhoodResponse(
            root_key=root_key,
            depth=effective_depth,
            nodes=list(node_map.values()),
            edges=list(edge_map.values()),
        )

    async def compare_nodes(
        self,
        *,
        node_a_type: str,
        node_a_id: UUID | str,
        node_b_type: str,
        node_b_id: UUID | str,
    ) -> GraphRelationResponse | None:
        node_a = await self._load_node(node_a_type, node_a_id)
        node_b = await self._load_node(node_b_type, node_b_id)
        if node_a is None or node_b is None:
            return None

        key_a = self._node_key(node_a_type, node_a_id)
        key_b = self._node_key(node_b_type, node_b_id)
        connections_a = await self._connections_for_node(
            node_type=node_a_type, node_id=node_a_id
        )
        connections_b = await self._connections_for_node(
            node_type=node_b_type, node_id=node_b_id
        )
        neighbors_a = {
            self._node_key(item.node_type, item.node_id): item for item in connections_a
        }
        neighbors_b = {
            self._node_key(item.node_type, item.node_id): item for item in connections_b
        }
        shared_keys = [key for key in neighbors_a.keys() if key in neighbors_b.keys()][
            :8
        ]

        direct_edges = (
            (
                await self.session.execute(
                    select(Edge)
                    .where(
                        or_(
                            (
                                (Edge.source_type == node_a_type)
                                & (Edge.source_id == node_a_id)
                                & (Edge.target_type == node_b_type)
                                & (Edge.target_id == node_b_id)
                            ),
                            (
                                (Edge.source_type == node_b_type)
                                & (Edge.source_id == node_b_id)
                                & (Edge.target_type == node_a_type)
                                & (Edge.target_id == node_a_id)
                            ),
                        )
                    )
                    .order_by(desc(Edge.created_at))
                )
            )
            .scalars()
            .all()
        )
        synthetic_direct = [
            item
            for item in connections_a
            if item.node_type == node_b_type and item.node_id == node_b_id
        ]

        nodes: OrderedDict[str, GraphWebNodeResponse] = OrderedDict()
        nodes[key_a] = GraphWebNodeResponse(
            key=key_a,
            id=node_a_id,
            node_type=node_a_type,
            label=self._node_label(node_a_type, node_a),
            layer=await self._node_layer(node_a_type, node_a),
            subtitle=self._node_subtitle(node_a_type, node_a),
            tier=getattr(node_a, "tier", None),
            created_at=getattr(node_a, "created_at", None),
            is_root=True,
        )
        nodes[key_b] = GraphWebNodeResponse(
            key=key_b,
            id=node_b_id,
            node_type=node_b_type,
            label=self._node_label(node_b_type, node_b),
            layer=await self._node_layer(node_b_type, node_b),
            subtitle=self._node_subtitle(node_b_type, node_b),
            tier=getattr(node_b, "tier", None),
            created_at=getattr(node_b, "created_at", None),
            is_root=True,
        )
        for shared_key in shared_keys:
            shared = neighbors_a[shared_key]
            shared_node = await self._load_node(shared.node_type, shared.node_id)
            if shared_node is None:
                continue
            nodes[shared_key] = GraphWebNodeResponse(
                key=shared_key,
                id=shared.node_id,
                node_type=shared.node_type,
                label=self._node_label(shared.node_type, shared_node),
                layer=await self._node_layer(shared.node_type, shared_node),
                subtitle=self._node_subtitle(shared.node_type, shared_node),
                tier=getattr(shared_node, "tier", None),
                created_at=getattr(shared_node, "created_at", None),
                is_root=False,
            )

        node_keys = set(nodes.keys())
        edge_candidates = (
            (
                await self.session.execute(
                    select(Edge)
                    .where(
                        or_(
                            Edge.source_id.in_([node.id for node in nodes.values()]),
                            Edge.target_id.in_([node.id for node in nodes.values()]),
                        )
                    )
                    .order_by(desc(Edge.created_at))
                    .limit(120)
                )
            )
            .scalars()
            .all()
        )
        edge_map: OrderedDict[UUID, GraphWebEdgeResponse] = OrderedDict()
        for edge in edge_candidates:
            source_key = self._node_key(edge.source_type, edge.source_id)
            target_key = self._node_key(edge.target_type, edge.target_id)
            if source_key not in node_keys or target_key not in node_keys:
                continue
            if edge.id in edge_map:
                continue
            edge_map[edge.id] = GraphWebEdgeResponse(
                id=edge.id,
                source_key=source_key,
                target_key=target_key,
                relationship_type=edge.relationship_type,
                confidence=float(edge.confidence or 0.0),
            )
        direct_relation_names = [edge.relationship_type for edge in direct_edges]
        summary_parts: list[str] = []
        if direct_relation_names:
            summary_parts.append(
                f"Direct links: {', '.join(dict.fromkeys(direct_relation_names))}."
            )
        elif synthetic_direct:
            synthetic_names = [item.relationship_type for item in synthetic_direct]
            summary_parts.append(
                f"Direct links: {', '.join(dict.fromkeys(synthetic_names))}."
            )
        if shared_keys:
            shared_labels = [nodes[key].label for key in shared_keys if key in nodes]
            summary_parts.append(f"Shared context: {', '.join(shared_labels[:4])}.")
        if not summary_parts:
            summary_parts.append(
                "No direct edge is stored yet, but the two nodes do not currently share a visible neighborhood."
            )
        nodes_dict = OrderedDict(nodes)
        await self._attach_layouts(nodes_dict)

        return GraphRelationResponse(
            node_a_key=key_a,
            node_b_key=key_b,
            direct_relationships=[
                GraphWebEdgeResponse(
                    id=edge.id,
                    source_key=self._node_key(edge.source_type, edge.source_id),
                    target_key=self._node_key(edge.target_type, edge.target_id),
                    relationship_type=edge.relationship_type,
                    confidence=float(edge.confidence or 0.0),
                )
                for edge in direct_edges
            ],
            shared_neighbor_keys=shared_keys,
            summary=" ".join(summary_parts),
            nodes=list(nodes_dict.values()),
            edges=list(edge_map.values()),
        )

    async def sync_layout(self, layouts: list[GraphNodeLayoutItem]) -> None:
        from sqlalchemy.dialects.postgresql import insert

        from investos.models.graph import GraphNodeLayout

        if not layouts:
            return

        values = []
        for l in layouts:
            values.append(
                {
                    "node_key": l.node_key,
                    "x": float(l.x),
                    "y": float(l.y),
                    "vx": float(l.vx),
                    "vy": float(l.vy),
                }
            )

        stmt = insert(GraphNodeLayout).values(values)
        stmt = stmt.on_conflict_do_update(
            index_elements=["node_key"],
            set_={
                "x": stmt.excluded.x,
                "y": stmt.excluded.y,
                "vx": stmt.excluded.vx,
                "vy": stmt.excluded.vy,
                "updated_at": datetime.now(UTC),
            },
        )
        await self.session.execute(stmt)
        await self.session.commit()

    async def _attach_layouts(self, nodes: OrderedDict[str, GraphWebNodeResponse]):
        from investos.models.graph import GraphNodeLayout

        if not nodes:
            return
        keys = list(nodes.keys())
        layouts = (
            (
                await self.session.execute(
                    select(GraphNodeLayout).where(GraphNodeLayout.node_key.in_(keys))
                )
            )
            .scalars()
            .all()
        )
        for layout in layouts:
            if layout.node_key in nodes:
                node = nodes[layout.node_key]
                node.x = float(layout.x)
                node.y = float(layout.y)
                node.vx = float(layout.vx)
                node.vy = float(layout.vy)

    async def _load_node(self, node_type: str, node_id: UUID | str):
        if node_type == "portfolio":
            node_id = SYNTHETIC_PORTFOLIO_ID
        if isinstance(node_id, str):
            try:
                node_id = UUID(node_id)
            except ValueError:
                # Keep as string for synthetic nodes like 'ENT-ROOT'
                pass

        if node_type == "portfolio":

            class SyntheticPortfolioNode:
                id = node_id
                title = "Portfolio"
                tier = "core"
                created_at = None
                is_autonomous = False

            return SyntheticPortfolioNode()

        if node_type == "position" and isinstance(node_id, UUID):
            return (
                await self.session.execute(
                    select(Position)
                    .options(
                        selectinload(Position.security).selectinload(Security.entity)
                    )
                    .where(Position.id == node_id)
                )
            ).scalar_one_or_none()

        model_map = durable_graph_model_map()
        model = model_map.get(node_type)
        if model is None or not isinstance(node_id, UUID):
            return None
        return (
            await self.session.execute(select(model).where(model.id == node_id))
        ).scalar_one_or_none()

    async def _connections_for_node(
        self, *, node_type: str, node_id: UUID | str
    ) -> list[GraphConnectionResponse]:
        node = await self._load_node(node_type, node_id)
        if node is None:
            return []
        resolved_node_id = getattr(node, "id", node_id)
        edges = await self._edges_touching_node(node_type=node_type, node_id=node_id)

        connection_map: OrderedDict[tuple[str, UUID, str], GraphConnectionResponse] = (
            OrderedDict()
        )
        for edge in edges:
            is_outgoing = (
                edge.source_type == node_type and edge.source_id == resolved_node_id
            )
            other_type = edge.target_type if is_outgoing else edge.source_type
            other_id = edge.target_id if is_outgoing else edge.source_id
            other_node = await self._load_node(other_type, other_id)
            if other_node is None:
                continue
            key = ("out" if is_outgoing else "in", other_id, edge.relationship_type)
            if key in connection_map:
                continue
            connection_map[key] = GraphConnectionResponse(
                edge_id=edge.id,
                direction="outgoing" if is_outgoing else "incoming",
                relationship_type=edge.relationship_type,
                confidence=float(edge.confidence or 0.0),
                node_id=other_id,
                node_type=other_type,
                label=self._node_label(other_type, other_node),
                subtitle=self._node_subtitle(other_type, other_node),
                tier=getattr(other_node, "tier", None),
                created_at=getattr(other_node, "created_at", None),
            )
        for synthetic in await self._synthetic_connections(
            node_type=node_type, node_id=node_id, node=node
        ):
            key = (synthetic.direction, synthetic.node_id, synthetic.relationship_type)
            if key in connection_map:
                continue
            connection_map[key] = synthetic
        return list(connection_map.values())[:18]

    async def _edges_touching_node(
        self, *, node_type: str, node_id: UUID | str
    ) -> list[Edge]:
        real_id: UUID | None = None
        if isinstance(node_id, UUID):
            real_id = node_id
        else:
            try:
                real_id = UUID(node_id)
            except ValueError:
                pass

        if real_id is None:
            return []

        return (
            (
                await self.session.execute(
                    select(Edge)
                    .where(
                        or_(
                            (Edge.source_type == node_type)
                            & (Edge.source_id == real_id),
                            (Edge.target_type == node_type)
                            & (Edge.target_id == real_id),
                        )
                    )
                    .order_by(desc(Edge.created_at))
                    .limit(40)
                )
            )
            .scalars()
            .all()
        )

    async def _citations_for_node(
        self, node_type: str, node
    ) -> list[GraphCitationResponse]:
        source_items: list[SourceItem] = []
        if node_type in {"fact", "claim"}:
            source_item = (
                await self.session.execute(
                    select(SourceItem).where(SourceItem.id == node.source_item_id)
                )
            ).scalar_one_or_none()
            if source_item is not None:
                source_items.append(source_item)
        elif node_type == "event":
            edge_rows = (
                (
                    await self.session.execute(
                        select(Edge)
                        .where(
                            Edge.source_type == "event",
                            Edge.source_id == node.id,
                            Edge.target_type == "source_item",
                        )
                        .order_by(desc(Edge.created_at))
                    )
                )
                .scalars()
                .all()
            )
            for edge in edge_rows:
                source_item = (
                    await self.session.execute(
                        select(SourceItem).where(SourceItem.id == edge.target_id)
                    )
                ).scalar_one_or_none()
                if source_item is not None:
                    source_items.append(source_item)
        elif node_type == "source_item":
            source_items.append(node)
        elif node_type in {"fundamental_metric", "market_setup_signal"}:
            if node.source_item_id is not None:
                source_item = (
                    await self.session.execute(
                        select(SourceItem).where(SourceItem.id == node.source_item_id)
                    )
                ).scalar_one_or_none()
                if source_item is not None:
                    source_items.append(source_item)
            elif node.raw_evidence_id is not None:
                raw = (
                    await self.session.execute(
                        select(RawEvidence).where(
                            RawEvidence.id == node.raw_evidence_id
                        )
                    )
                ).scalar_one_or_none()
                return [await self._citation_from_raw(raw)] if raw is not None else []
        elif node_type == "raw_evidence":
            return await self._raw_evidence_citation(node)
        elif node_type in {"entity", "theme"}:
            edge_rows = (
                (
                    await self.session.execute(
                        select(Edge)
                        .where(
                            Edge.target_type == node_type,
                            Edge.target_id == node.id,
                            Edge.source_type.in_(["fact", "claim", "event"]),
                        )
                        .order_by(desc(Edge.created_at))
                        .limit(10)
                    )
                )
                .scalars()
                .all()
            )
            for edge in edge_rows:
                related = await self._load_node(edge.source_type, edge.source_id)
                source_items.extend(
                    await self._source_items_for_extracted_node(
                        edge.source_type, related
                    )
                )
        elif node_type == "source":
            raw_items = (
                (
                    await self.session.execute(
                        select(RawEvidence)
                        .where(RawEvidence.source_id == node.id)
                        .order_by(desc(RawEvidence.created_at))
                        .limit(6)
                    )
                )
                .scalars()
                .all()
            )
            return [
                await self._citation_from_raw(raw)
                for raw in raw_items
                if raw is not None
            ]
        elif node_type == "conclusion":
            evidence_ids = (node.key_supporting_evidence_ids or []) + (
                node.key_contradicting_evidence_ids or []
            )
            source_items.extend(
                await self._source_items_for_evidence_ids(evidence_ids[:10])
            )
        elif node_type == "unresolved_question" and node.originating_evidence_id:
            source_items.extend(
                await self._source_items_for_evidence_ids(
                    [node.originating_evidence_id]
                )
            )
        elif node_type == "review_item" and node.item_type == "conclusion":
            conclusion = await self._load_node("conclusion", node.item_id)
            if conclusion is not None:
                evidence_ids = (conclusion.key_supporting_evidence_ids or []) + (
                    conclusion.key_contradicting_evidence_ids or []
                )
                source_items.extend(
                    await self._source_items_for_evidence_ids(evidence_ids[:8])
                )
        elif node_type == "experiment_result":
            experiment = await self._load_node("shadow_experiment", node.experiment_id)
            if experiment is not None:
                context = (experiment.initial_portfolio_state_json or {}).get(
                    "experiment_context"
                ) or {}
                trigger_reason = context.get("trigger_reason")
                if trigger_reason:
                    source_items.extend(
                        await self._source_items_matching_text(trigger_reason)
                    )

        citations: OrderedDict[UUID, GraphCitationResponse] = OrderedDict()
        for source_item in source_items:
            raw = (
                await self.session.execute(
                    select(RawEvidence).where(
                        RawEvidence.id == source_item.raw_evidence_id
                    )
                )
            ).scalar_one_or_none()
            if raw is None:
                continue
            citations[raw.id] = await self._citation_from_raw(
                raw, source_item_id=source_item.id
            )
        return list(citations.values())[:6]

    async def _source_items_for_extracted_node(
        self, node_type: str, node
    ) -> list[SourceItem]:
        if node is None:
            return []
        if node_type in {"fundamental_metric", "market_setup_signal"}:
            if getattr(node, "source_item_id", None):
                source_item = (
                    await self.session.execute(
                        select(SourceItem).where(SourceItem.id == node.source_item_id)
                    )
                ).scalar_one_or_none()
                return [source_item] if source_item is not None else []
            raw_evidence_id = getattr(node, "raw_evidence_id", None)
            if raw_evidence_id:
                items = (
                    (
                        await self.session.execute(
                            select(SourceItem)
                            .where(SourceItem.raw_evidence_id == raw_evidence_id)
                            .limit(1)
                        )
                    )
                    .scalars()
                    .all()
                )
                return list(items)
            return []
        if node_type in {"fact", "claim"}:
            source_item = (
                await self.session.execute(
                    select(SourceItem).where(SourceItem.id == node.source_item_id)
                )
            ).scalar_one_or_none()
            return [source_item] if source_item is not None else []
        if node_type == "event":
            edges = (
                (
                    await self.session.execute(
                        select(Edge).where(
                            Edge.source_type == "event",
                            Edge.source_id == node.id,
                            Edge.target_type == "source_item",
                        )
                    )
                )
                .scalars()
                .all()
            )
            items: list[SourceItem] = []
            for edge in edges:
                source_item = (
                    await self.session.execute(
                        select(SourceItem).where(SourceItem.id == edge.target_id)
                    )
                ).scalar_one_or_none()
                if source_item is not None:
                    items.append(source_item)
            return items
        return []

    async def _raw_evidence_citation(
        self, raw: RawEvidence
    ) -> list[GraphCitationResponse]:
        return [await self._citation_from_raw(raw)]

    async def _citation_from_raw(
        self, raw: RawEvidence, *, source_item_id: UUID | None = None
    ) -> GraphCitationResponse:
        source = (
            await self.session.execute(select(Source).where(Source.id == raw.source_id))
        ).scalar_one()
        is_system, system_reason = self._raw_is_system(raw, source)
        origin = SourceService._evidence_origin_summary(raw, source)
        return GraphCitationResponse(
            raw_evidence_id=raw.id,
            source_item_id=source_item_id,
            source_id=source.id,
            source_name=source.name,
            source_type=source.source_type,
            source_item_type=raw.source_item_type,
            origin_kind=origin["origin_kind"],
            origin_label=origin["origin_label"],
            origin_detail=origin["origin_detail"],
            layer="system" if is_system else "knowledge",
            is_system=is_system,
            system_reason=system_reason,
            title=raw.title,
            url=raw.url,
            author=raw.author,
            created_at=raw.created_at,
        )

    def _source_is_system(self, source: Source) -> tuple[bool, str | None]:
        if source.name == CONVERSATION_SOURCE_NAME:
            return True, "internal_conversation_memory"

        # Flag internal operating memory from the platform itself
        if source.source_type in {"operating", "system"}:
            return True, f"platform_infrastructure:{source.source_type}"

        # Prefer explicit source metadata when the model supports it, but stay
        # compatible with older Source rows that do not have any metadata field.
        source_metadata = getattr(source, "metadata_json", None)
        is_research = (
            source_metadata.get("is_research_relevant")
            if isinstance(source_metadata, dict)
            else None
        )
        if is_research is False:
            return True, "classified_as_technical_infrastructure"

        # PII and Auth-source exclusion (non-research technical connections)
        if source.source_type == "integration" and not is_research:
            return True, "technical_integration_layer"

        return False, None

    def _raw_is_system(
        self, raw: RawEvidence, source: Source
    ) -> tuple[bool, str | None]:
        if raw.source_item_type == "conversation_turn":
            return True, "conversation_turn"
        source_is_system, source_reason = self._source_is_system(source)
        if source_is_system:
            return True, source_reason
        if (raw.metadata_json or {}).get("internal_only"):
            return True, "internal_only"
        return False, None

    async def _node_layer(self, node_type: str, node) -> str:
        if node_type in {
            "fact",
            "claim",
            "event",
            "entity",
            "theme",
            "fundamental_metric",
            "market_setup_signal",
            "historical_episode",
        }:
            return "knowledge"
        if node_type == "position":
            return "operating"
        if node_type in {"coverage_map", "unresolved_question", "raw_evidence"}:
            if node_type == "raw_evidence":
                source = (
                    await self.session.execute(
                        select(Source).where(Source.id == node.source_id)
                    )
                ).scalar_one_or_none()
                if source is not None and self._raw_is_system(node, source)[0]:
                    return "system"
                return "source"
            return "system"
        if node_type in {
            "conclusion",
            "review_item",
            "lesson",
            "shadow_experiment",
            "experiment_result",
        }:
            return "operating"
        if node_type == "source":
            return "system" if self._source_is_system(node)[0] else "source"
        if node_type == "source_item":
            raw = (
                await self.session.execute(
                    select(RawEvidence).where(RawEvidence.id == node.raw_evidence_id)
                )
            ).scalar_one_or_none()
            if raw is None:
                return "source"
            source = (
                await self.session.execute(
                    select(Source).where(Source.id == raw.source_id)
                )
            ).scalar_one_or_none()
            if source is not None and self._raw_is_system(raw, source)[0]:
                return "system"
            return "source"
        return "knowledge"

    async def _node_properties(
        self,
        node_type: str,
        node_id: UUID | str,
        node,
        attachment: dict[str, object] | None = None,
    ) -> dict[str, object]:
        properties: dict[str, object] = {}
        if node_type in {"fact", "claim"}:
            properties = {
                "importance": getattr(node, "importance", None),
                "confidence": float(getattr(node, "confidence", 0.0) or 0.0),
                "contradiction_role": getattr(node, "contradiction_role", None),
                "directness": getattr(node, "directness", None),
                "novelty": getattr(node, "novelty", None),
                "event_time": (
                    node.event_time.isoformat()
                    if getattr(node, "event_time", None)
                    else None
                ),
                "public_time": (
                    node.public_time.isoformat()
                    if getattr(node, "public_time", None)
                    else None
                ),
                "ingest_time": (
                    node.ingest_time.isoformat()
                    if getattr(node, "ingest_time", None)
                    else None
                ),
            }
        elif node_type == "event":
            properties = {
                "event_type": node.event_type,
                "event_time": node.event_time.isoformat() if node.event_time else None,
                "public_time": (
                    node.public_time.isoformat() if node.public_time else None
                ),
            }
        elif node_type == "entity":
            securities = (
                await self.session.execute(select(Entity).where(Entity.id == node_id))
            ).scalar_one_or_none()
            properties = {
                "entity_type": node.entity_type,
                "sector": node.sector,
                "industry": node.industry,
                "country": node.country,
                "aliases": node.aliases or [],
            }
        elif node_type == "theme":
            properties = {
                "status": node.status,
                "description": node.description,
            }
        elif node_type == "historical_episode":
            properties = {
                "episode_type": node.episode_type,
                "start_time": node.start_time.isoformat(),
                "end_time": node.end_time.isoformat() if node.end_time else None,
                "affected_sectors": node.affected_sectors or [],
                "affected_themes": node.affected_themes or [],
                "dominant_channel": node.dominant_channel,
                "notes": node.notes,
            }
        elif node_type == "position":
            properties = {
                "list_type": node.list_type,
                "direction": node.direction,
                "quantity": float(node.quantity or 0.0),
                "avg_cost_basis": float(node.avg_cost_basis or 0.0),
                "current_price": float(node.current_price or 0.0),
                "market_value": float(node.market_value or 0.0),
                "unrealized_pnl": float(node.unrealized_pnl or 0.0),
                "realized_pnl": float(getattr(node, "realized_pnl", 0.0) or 0.0),
                "conviction": node.conviction,
            }
        elif node_type == "source_item":
            properties = {
                "processing_status": node.processing_status,
                "summary": node.summary,
            }
        elif node_type == "raw_evidence":
            properties = {
                "source_item_type": node.source_item_type,
                "is_processed": node.is_processed,
                "url": node.url,
                "author": node.author,
            }
        elif node_type == "source":
            profile = (
                await self.session.execute(
                    select(SourceProfile).where(SourceProfile.source_id == node.id)
                )
            ).scalar_one_or_none()
            trust = (
                await self.session.execute(
                    select(SourceTrustProfile).where(
                        SourceTrustProfile.source_id == node.id
                    )
                )
            ).scalar_one_or_none()
            value = (
                await self.session.execute(
                    select(SourceValueProfile).where(
                        SourceValueProfile.source_id == node.id
                    )
                )
            ).scalar_one_or_none()
            properties = {
                "source_type": node.source_type,
                "is_trusted": node.is_trusted,
                "url": node.url,
                "description": node.description,
                "specialization_domains": (
                    None if profile is None else (profile.specialization_domains or [])
                ),
                "known_weaknesses": (
                    None if profile is None else (profile.known_weaknesses or [])
                ),
                "trust_trajectory": (None if trust is None else trust.trust_trajectory)
                or (None if profile is None else profile.trust_trajectory),
                "factual_reliability": (
                    None if trust is None else trust.factual_reliability
                ),
                "noise_ratio": None if trust is None else trust.noise_ratio,
                "idea_generation_value": (
                    None if value is None else value.idea_generation_value
                ),
                "timing_value": None if value is None else value.timing_value,
                "portfolio_relevance_value": (
                    None if value is None else value.portfolio_relevance_value
                ),
                "specificity": None if value is None else value.specificity,
                "originality": None if value is None else value.originality,
            }
        elif node_type == "fundamental_metric":
            properties = {
                "metric_family": node.metric_family,
                "ticker": node.ticker,
                "value_text": node.value_text,
                "numeric_value": (
                    float(node.numeric_value)
                    if node.numeric_value is not None
                    else None
                ),
                "unit": node.unit,
                "currency": node.currency,
                "period_label": node.period_label,
                "fiscal_year": node.fiscal_year,
                "fiscal_quarter": node.fiscal_quarter,
                "as_of": node.as_of.isoformat() if node.as_of else None,
                "event_time": node.event_time.isoformat() if node.event_time else None,
                "public_time": (
                    node.public_time.isoformat() if node.public_time else None
                ),
                "eligible_action_time": (
                    node.eligible_action_time.isoformat()
                    if node.eligible_action_time
                    else None
                ),
                "stale_after": (
                    node.stale_after.isoformat() if node.stale_after else None
                ),
                "direction": node.direction,
                "confidence": float(node.confidence or 0.0),
                "investment_relevance": node.investment_relevance,
                "next_test": node.next_test,
                "source_kind": node.source_kind,
                "freshness_status": node.freshness_status,
            }
        elif node_type == "market_setup_signal":
            metadata = dict(node.metadata_json or {})
            properties = {
                "signal_family": node.signal_family,
                "ticker": node.ticker,
                "setup_context": node.setup_context,
                "actual_context": node.actual_context,
                "price_reaction": node.price_reaction,
                "value_text": node.value_text,
                "numeric_value": (
                    float(node.numeric_value)
                    if node.numeric_value is not None
                    else None
                ),
                "unit": node.unit,
                "period_label": node.period_label,
                "as_of": node.as_of.isoformat() if node.as_of else None,
                "event_time": node.event_time.isoformat() if node.event_time else None,
                "public_time": (
                    node.public_time.isoformat() if node.public_time else None
                ),
                "eligible_action_time": (
                    node.eligible_action_time.isoformat()
                    if node.eligible_action_time
                    else None
                ),
                "direction": node.direction,
                "confidence": float(node.confidence or 0.0),
                "investment_relevance": node.investment_relevance,
                "next_test": node.next_test,
                "source_kind": node.source_kind,
                "outcome_status": node.outcome_status,
                "outcome_score": node.outcome_score,
                "hypothesis_status": metadata.get("hypothesis_status"),
                "pattern_type": metadata.get("pattern_type"),
                "proposed_mechanism": metadata.get("proposed_mechanism"),
                "falsifier": metadata.get("falsifier"),
                "affected_tickers": metadata.get("affected_tickers"),
                "source_lineages": metadata.get("source_lineages"),
                "derivation_policy": metadata.get("derivation_policy"),
            }
        elif node_type == "coverage_map":
            properties = {
                "subject_type": node.subject_type,
                "overall_coverage_score": float(node.overall_coverage_score or 0.0),
                "total_evidence_count": node.total_evidence_count,
                "high_tier_evidence_count": node.high_tier_evidence_count,
                "contradiction_count": node.contradiction_count,
                "unresolved_contradiction_count": node.unresolved_contradiction_count,
                "last_computed_at": (
                    node.last_computed_at.isoformat() if node.last_computed_at else None
                ),
            }
        elif node_type == "unresolved_question":
            properties = {
                "urgency": node.urgency,
                "status": node.status,
                "created_at": node.created_at.isoformat() if node.created_at else None,
            }
        elif node_type == "conclusion":
            properties = {
                "subject_type": node.subject_type,
                "current_stance": node.current_stance,
                "confidence_band": node.confidence_band,
                "update_count": node.update_count,
                "last_updated_at": (
                    node.last_updated_at.isoformat() if node.last_updated_at else None
                ),
                "last_verified_at": (
                    node.last_verified_at.isoformat() if node.last_verified_at else None
                ),
                "what_would_falsify": node.what_would_falsify or [],
                "what_would_strengthen": node.what_would_strengthen or [],
            }
        elif node_type == "review_item":
            properties = {
                "item_type": node.item_type,
                "priority_score": round(float(node.priority_score or 0.0), 2),
                "status": node.status,
                "trigger_reason": node.trigger_reason,
                "contradiction_pressure": round(
                    float(node.contradiction_pressure or 0.0), 2
                ),
                "thesis_drift": round(float(node.thesis_drift or 0.0), 2),
                "coverage_weakness": round(float(node.coverage_weakness or 0.0), 2),
            }
        elif node_type == "lesson":
            properties = {
                "lesson_type": node.lesson_type,
                "maturity_status": node.maturity_status,
                "confidence_score": round(float(node.confidence_score or 0.0), 3),
                "supporting_observations": node.supporting_observations,
                "contradicting_observations": node.contradicting_observations,
                "neutral_observations": node.neutral_observations,
                "usage_count": node.usage_count,
                "applicable_sectors": node.applicable_sectors or [],
                "applicable_regimes": node.applicable_regimes or [],
                "last_validated_at": (
                    node.last_validated_at.isoformat()
                    if node.last_validated_at
                    else None
                ),
                "stale_after": (
                    node.stale_after.isoformat() if node.stale_after else None
                ),
            }
        elif node_type == "shadow_experiment":
            summary = (node.initial_portfolio_state_json or {}).get(
                "snapshot_summary"
            ) or {}
            context = (node.initial_portfolio_state_json or {}).get(
                "experiment_context"
            ) or {}
            properties = {
                "run_status": node.run_status,
                "policy_description": node.policy_description,
                "trigger_type": context.get("trigger_type"),
                "trigger_reason": context.get("trigger_reason"),
                "initiated_by": context.get("initiated_by"),
                "holding_count": summary.get("holding_count"),
                "remaining_buying_power": summary.get("remaining_buying_power"),
                "created_at": node.created_at.isoformat() if node.created_at else None,
                "completed_at": (
                    node.completed_at.isoformat() if node.completed_at else None
                ),
            }
        elif node_type == "experiment_result":
            properties = {
                "shadow_return": round(float(node.shadow_return or 0.0), 4),
                "actual_return": round(float(node.actual_return or 0.0), 4),
                "alpha": round(float(node.alpha or 0.0), 4),
                "max_drawdown": round(float(node.max_drawdown or 0.0), 4),
                "sharpe_ratio": (
                    round(float(node.sharpe_ratio or 0.0), 4)
                    if node.sharpe_ratio is not None
                    else None
                ),
            }

        if attachment:
            linked_holdings = attachment.get("linked_holdings") or []
            linked_companies = attachment.get("linked_companies") or []
            if attachment.get("category"):
                properties["portfolio_significance"] = attachment["category"]
            if attachment.get("reason"):
                properties["why_in_graph"] = attachment["reason"]
            if linked_holdings:
                properties["linked_holdings"] = linked_holdings
            if linked_companies:
                properties["linked_companies"] = linked_companies
            if attachment.get("portfolio_mechanism"):
                properties["portfolio_mechanism"] = attachment["portfolio_mechanism"]
            if attachment.get("affected_holdings"):
                properties["affected_holdings"] = attachment["affected_holdings"]
            if attachment.get("next_test"):
                properties["next_test"] = attachment["next_test"]
            if attachment.get("direct_portfolio_link"):
                properties["direct_portfolio_link"] = True
            if attachment.get("portfolio_connection_count") is not None:
                properties["portfolio_connection_count"] = attachment[
                    "portfolio_connection_count"
                ]
            if attachment.get("graph_connection_count") is not None:
                properties["graph_connection_count"] = attachment[
                    "graph_connection_count"
                ]
            if attachment.get("nearest_connections"):
                properties["nearest_connections"] = attachment["nearest_connections"]
            if attachment.get("portfolio_route_depth") is not None:
                properties["portfolio_route_depth"] = attachment[
                    "portfolio_route_depth"
                ]

        if node_type in {"entity", "theme"}:
            profile = (
                await self.session.execute(
                    select(Profile).where(
                        Profile.subject_type == node_type, Profile.subject_id == node_id
                    )
                )
            ).scalar_one_or_none()
            conclusion = await self.canonical.get_conclusion_state(
                subject_type=node_type,
                subject_id=node_id,
            )
            profile_summary = profile.executive_summary if profile else None
            if self._is_placeholder_profile_summary(profile_summary) and attachment:
                if attachment.get("category") == "broad context":
                    profile_summary = None
                else:
                    profile_summary = self._profile_gap_summary(
                        node_type=node_type, node=node, attachment=attachment
                    )
            properties["profile_summary"] = profile_summary
            properties["current_stance"] = (
                conclusion.current_stance if conclusion else None
            )
            properties["confidence_band"] = (
                conclusion.confidence_band if conclusion else None
            )
            properties["thesis_summary"] = (
                conclusion.current_thesis_summary if conclusion else None
            )
        return {
            key: value
            for key, value in properties.items()
            if value not in (None, [], "")
        }

    def _generate_relevance_reasoning(
        self,
        node_type: str,
        node,
        relevance: float,
        attachment: dict[str, object],
        *,
        connections: list[GraphConnectionResponse] | None = None,
        citations: list[GraphCitationResponse] | None = None,
    ) -> str | None:
        """Explain why the node matters to the portfolio, not just that it exists."""
        if node_type == "portfolio":
            return "This is the live portfolio root. Everything else is judged by how it affects or informs this book."

        reason = str(attachment.get("reason") or "").strip()
        linked_holdings = attachment.get("linked_holdings") or []
        linked_companies = attachment.get("linked_companies") or []
        affected_holdings = attachment.get("affected_holdings") or []
        mechanism = str(attachment.get("portfolio_mechanism") or "").strip()
        next_test = str(attachment.get("next_test") or "").strip()
        connection_phrases = self._connection_phrases(connections or [])
        source_names = list(
            dict.fromkeys(
                citation.source_name
                for citation in (citations or [])
                if citation.source_name and not citation.is_system
            )
        )

        if mechanism:
            affected = ", ".join([str(item) for item in affected_holdings[:4]])
            prefix = f"This matters through {affected}: " if affected else ""
            if next_test:
                return f"{prefix}{mechanism} Next useful test: {next_test}"
            return f"{prefix}{mechanism}"
        if reason:
            return reason
        if linked_holdings or linked_companies:
            connected = ", ".join((linked_holdings + linked_companies)[:4])
            return f"This node is in the graph because it connects directly to tracked portfolio names: {connected}."
        if connection_phrases:
            evidence = (
                f" External source support is attached from {', '.join(source_names[:3])}."
                if source_names
                else ""
            )
            relationship_count = len(connections or [])
            return (
                f"This node is stored because it has {relationship_count} graph relationship"
                f"{'' if relationship_count == 1 else 's'}, including {', '.join(connection_phrases[:3])}."
                f"{evidence} No direct holding-level mechanism is stored yet, so it should be treated as context rather than thesis evidence."
            )
        if source_names:
            return (
                f"This node is backed by source material from {', '.join(source_names[:3])}, but it has no stored "
                "relationship to a live holding, thesis, watch item, or risk factor yet."
            )
        readable = node_type.replace("_", " ")
        if relevance >= 0.6:
            return (
                f"This {readable} is material portfolio context (relevance {relevance:.0%}) — it informs a "
                "tracked thesis even though it is not itself a position."
            )
        return (
            f"This {readable} is currently isolated in the graph: no direct holding link, no stored relationship, "
            "and no external citation is attached in this detail view."
        )

    def _is_placeholder_profile_summary(self, summary: str | None) -> bool:
        if not summary:
            return False
        normalized = " ".join(summary.lower().split())
        placeholders = (
            "prophet has some stored context relevant to this subject",
            "not enough targeted research on this specific angle yet",
            "a focused research pass would help build a stronger evidence base",
            "no accepted thesis on record",
        )
        return any(marker in normalized for marker in placeholders)

    def _profile_gap_summary(
        self, *, node_type: str, node, attachment: dict[str, object]
    ) -> str:
        label = self._node_label(node_type, node)
        mechanism = str(attachment.get("portfolio_mechanism") or "").strip()
        next_test = str(attachment.get("next_test") or "").strip()
        if mechanism:
            summary = (
                f"{label} has thin stored coverage. Current impact route: {mechanism}"
            )
            if next_test:
                summary += f" Next research step: {next_test}"
            return summary
        reason = str(attachment.get("reason") or "").strip()
        if reason:
            return f"{label} has thin stored coverage. {reason}"
        return (
            f"{label} has thin stored coverage. It should stay in the graph only if it can be tied to a live holding, "
            "accepted thesis, falsifiable watch item, or explicit risk-sizing rule."
        )

    def _calculate_relevance(
        self,
        node_type: str,
        node,
        attachment: dict[str, object],
    ) -> float:
        """Estimate how directly a node matters to the live portfolio."""
        if node_type == "portfolio":
            return 1.0

        category = attachment.get("category")
        base = {
            "direct holding": 0.97,
            "tracked company": 0.92,
            "direct portfolio context": 0.82,
            "connected portfolio context": 0.72,
            "portfolio-level context": 0.64,
            "broad context": 0.34,
        }.get(category, 0.5)

        if node_type in {"conclusion", "lesson"}:
            base += 0.05
        elif node_type in {"claim", "fact", "event"}:
            base += 0.03
        elif node_type in {"source_item", "raw_evidence"}:
            base -= 0.02

        tier = getattr(node, "tier", None)
        if tier == "critical":
            base += 0.05
        elif tier == "high":
            base += 0.03
        elif tier == "low":
            base -= 0.03
        return max(0.0, min(1.0, round(base, 2)))

    async def _portfolio_attachment_summary(
        self,
        *,
        node_type: str,
        node_id: UUID | str,
        node,
        connections: list[GraphConnectionResponse] | None = None,
    ) -> dict[str, object]:
        tracked_rows = (
            await self.session.execute(
                select(
                    Position.id,
                    Security.entity_id,
                    Security.ticker,
                    Entity.name,
                    Position.market_value,
                )
                .join(Security, Security.id == Position.security_id)
                .join(Entity, Entity.id == Security.entity_id)
                .where(Position.list_type == "holding", Position.quantity > 0)
                .order_by(desc(Position.market_value))
            )
        ).all()
        tracked_position_ids = {row[0] for row in tracked_rows}
        tracked_entity_ids = {row[1] for row in tracked_rows}
        tracked_position_labels = {
            row[0]: (row[2] or row[3] or "Holding") for row in tracked_rows
        }
        tracked_entity_labels = {
            row[1]: (row[2] or row[3] or "Company") for row in tracked_rows
        }
        all_holdings = [
            {
                "position_id": row[0],
                "entity_id": row[1],
                "ticker": row[2] or "",
                "name": row[3] or "",
                "label": row[2] or row[3] or "Holding",
                "market_value": float(row[4] or 0.0),
            }
            for row in tracked_rows
        ]

        if node_type == "portfolio":
            return {
                "category": "direct holding",
                "reason": "This is the portfolio root itself.",
                "linked_holdings": [],
                "linked_companies": [],
                "direct_portfolio_link": True,
                "portfolio_connection_count": 0,
                "graph_connection_count": 0,
                "nearest_connections": [],
            }

        if node_type == "position":
            list_type = getattr(node, "list_type", None)
            if (
                list_type == "holding"
                and float(getattr(node, "quantity", 0.0) or 0.0) > 0
            ):
                label = self._node_label(node_type, node)
                context_texts = await self._subject_mechanism_contexts(
                    node_type=node_type,
                    node_id=getattr(node, "id", node_id),
                    node=node,
                )
                mechanism = self._infer_portfolio_mechanism(
                    node_label=label,
                    node_type=node_type,
                    linked_holdings=[label],
                    linked_companies=[],
                    all_holdings=all_holdings,
                    direct_portfolio_link=True,
                    context_texts=context_texts,
                )
                return {
                    "category": "direct holding",
                    "reason": "This node is one of your current live holdings.",
                    "linked_holdings": [label],
                    "linked_companies": [],
                    "direct_portfolio_link": True,
                    "portfolio_connection_count": 1,
                    "graph_connection_count": 1,
                    "nearest_connections": [],
                    **mechanism,
                }

        if node_type == "entity" and getattr(node, "id", None) in tracked_entity_ids:
            label = self._node_label(node_type, node)
            context_texts = await self._subject_mechanism_contexts(
                node_type=node_type,
                node_id=getattr(node, "id", node_id),
                node=node,
            )
            mechanism = self._infer_portfolio_mechanism(
                node_label=label,
                node_type=node_type,
                linked_holdings=[],
                linked_companies=[label],
                all_holdings=all_holdings,
                direct_portfolio_link=True,
                context_texts=context_texts,
            )
            return {
                "category": "tracked company",
                "reason": f"{label} is directly represented in your live book.",
                "linked_holdings": [],
                "linked_companies": [label],
                "direct_portfolio_link": True,
                "portfolio_connection_count": 1,
                "graph_connection_count": 1,
                "nearest_connections": [],
                **mechanism,
            }

        direct_connections = connections or await self._connections_for_node(
            node_type=node_type, node_id=node_id
        )
        nearest_connections = self._connection_phrases(direct_connections)
        linked_holdings: list[str] = []
        linked_companies: list[str] = []
        direct_portfolio_link = False
        for connection in direct_connections:
            if connection.node_type == "portfolio":
                direct_portfolio_link = True
            elif (
                connection.node_type == "position"
                and connection.node_id in tracked_position_ids
            ):
                linked_holdings.append(
                    tracked_position_labels.get(connection.node_id, connection.label)
                )
            elif (
                connection.node_type == "entity"
                and connection.node_id in tracked_entity_ids
            ):
                linked_companies.append(
                    tracked_entity_labels.get(connection.node_id, connection.label)
                )

        linked_holdings = list(dict.fromkeys(linked_holdings))
        linked_companies = list(dict.fromkeys(linked_companies))
        connection_count = (
            len(linked_holdings)
            + len(linked_companies)
            + (1 if direct_portfolio_link else 0)
        )

        if linked_holdings or linked_companies:
            connected = ", ".join((linked_holdings + linked_companies)[:4])
            mechanism = self._infer_portfolio_mechanism(
                node_label=self._node_label(node_type, node),
                node_type=node_type,
                linked_holdings=linked_holdings,
                linked_companies=linked_companies,
                all_holdings=all_holdings,
                direct_portfolio_link=direct_portfolio_link,
                context_texts=[
                    self._node_body(node_type, node) or "",
                    *nearest_connections[:6],
                ],
            )
            return {
                "category": "direct portfolio context",
                "reason": f"This node matters because it connects directly to tracked portfolio names: {connected}.",
                "linked_holdings": linked_holdings,
                "linked_companies": linked_companies,
                "direct_portfolio_link": direct_portfolio_link,
                "portfolio_connection_count": connection_count,
                "graph_connection_count": len(direct_connections),
                "nearest_connections": nearest_connections[:6],
                **mechanism,
            }
        one_hop = (
            await self._one_hop_portfolio_connections(
                direct_connections=direct_connections,
                tracked_position_ids=tracked_position_ids,
                tracked_entity_ids=tracked_entity_ids,
                tracked_position_labels=tracked_position_labels,
                tracked_entity_labels=tracked_entity_labels,
            )
            if not direct_portfolio_link
            else self._empty_portfolio_route()
        )
        one_hop_holdings = one_hop["linked_holdings"]
        one_hop_companies = one_hop["linked_companies"]
        if one_hop_holdings or one_hop_companies or one_hop["reaches_portfolio"]:
            connected = ", ".join((one_hop_holdings + one_hop_companies)[:4])
            intermediaries = ", ".join(one_hop["intermediary_labels"][:3])
            route_description = (
                f" through {intermediaries}"
                if intermediaries
                else " through a stored relationship"
            )
            destination = (
                f"tracked names: {connected}"
                if connected
                else "the live portfolio root"
            )
            path_phrases = list(
                dict.fromkeys([*nearest_connections, *one_hop["path_phrases"]])
            )
            intermediary_contexts: list[str] = []
            intermediary_set = set(one_hop["intermediary_labels"])
            for connection in direct_connections:
                if connection.label not in intermediary_set:
                    continue
                intermediary_node = await self._load_node(
                    connection.node_type, connection.node_id
                )
                if intermediary_node is None:
                    continue
                intermediary_contexts.extend(
                    self._stored_mechanism_contexts(
                        connection.node_type, intermediary_node
                    )
                )
            mechanism = self._infer_portfolio_mechanism(
                node_label=self._node_label(node_type, node),
                node_type=node_type,
                linked_holdings=one_hop_holdings,
                linked_companies=one_hop_companies,
                all_holdings=all_holdings,
                direct_portfolio_link=False,
                context_texts=[
                    *intermediary_contexts,
                    self._node_body(node_type, node) or "",
                    *path_phrases[:8],
                ],
            )
            return {
                "category": "connected portfolio context",
                "reason": (
                    f"This node reaches {destination}{route_description}. "
                    "The relationship is indirect, so the transmission path should be tested rather than assumed."
                ),
                "linked_holdings": one_hop_holdings,
                "linked_companies": one_hop_companies,
                "direct_portfolio_link": False,
                "portfolio_connection_count": one_hop["portfolio_connection_count"],
                "graph_connection_count": len(direct_connections)
                + one_hop["traversed_connection_count"],
                "nearest_connections": path_phrases[:8],
                "portfolio_route_depth": 2,
                **mechanism,
            }
        if direct_portfolio_link:
            mechanism = self._infer_portfolio_mechanism(
                node_label=self._node_label(node_type, node),
                node_type=node_type,
                linked_holdings=[],
                linked_companies=[],
                all_holdings=all_holdings,
                direct_portfolio_link=True,
                context_texts=[
                    self._node_body(node_type, node) or "",
                    *nearest_connections[:6],
                ],
            )
            return {
                "category": "portfolio-level context",
                "reason": (
                    "This node is attached at portfolio level. Its usefulness depends on a clear impact route, "
                    "not the edge alone."
                ),
                "linked_holdings": [],
                "linked_companies": [],
                "direct_portfolio_link": True,
                "portfolio_connection_count": 1,
                "graph_connection_count": len(direct_connections),
                "nearest_connections": nearest_connections[:6],
                **mechanism,
            }
        if direct_connections:
            reason = (
                f"This node has {len(direct_connections)} stored graph relationship"
                f"{'' if len(direct_connections) == 1 else 's'}, but none currently reaches a live holding."
            )
        else:
            reason = (
                "This node is isolated: no stored relationship currently connects it to a holding, thesis, "
                "watch item, or source-backed risk factor."
            )
        return {
            "category": "broad context",
            "reason": reason,
            "linked_holdings": [],
            "linked_companies": [],
            "direct_portfolio_link": False,
            "portfolio_connection_count": 0,
            "graph_connection_count": len(direct_connections),
            "nearest_connections": nearest_connections[:6],
        }

    async def _one_hop_portfolio_connections(
        self,
        *,
        direct_connections: list[GraphConnectionResponse],
        tracked_position_ids: set[UUID],
        tracked_entity_ids: set[UUID],
        tracked_position_labels: dict[UUID, str],
        tracked_entity_labels: dict[UUID, str],
    ) -> dict[str, Any]:
        if not direct_connections:
            return self._empty_portfolio_route()

        linked_holdings: list[str] = []
        linked_companies: list[str] = []
        intermediary_labels: list[str] = []
        path_phrases: list[str] = []
        reaches_portfolio = False
        intermediary_by_key = {
            (connection.node_type, connection.node_id): connection.label
            for connection in direct_connections
        }
        intermediary_keys = list(intermediary_by_key)
        edges = (
            (
                await self.session.execute(
                    select(Edge).where(
                        or_(
                            tuple_(Edge.source_type, Edge.source_id).in_(
                                intermediary_keys
                            ),
                            tuple_(Edge.target_type, Edge.target_id).in_(
                                intermediary_keys
                            ),
                        )
                    )
                )
            )
            .scalars()
            .all()
        )
        for edge in edges:
            source_key = (edge.source_type, edge.source_id)
            target_key = (edge.target_type, edge.target_id)
            if source_key in intermediary_by_key:
                intermediary_key = source_key
                destination_type, destination_id = target_key
            elif target_key in intermediary_by_key:
                intermediary_key = target_key
                destination_type, destination_id = source_key
            else:
                continue

            destination_label = None
            if destination_type == "portfolio":
                reaches_portfolio = True
                destination_label = "Portfolio"
            elif (
                destination_type == "position"
                and destination_id in tracked_position_ids
            ):
                destination_label = tracked_position_labels.get(
                    destination_id, "Holding"
                )
                linked_holdings.append(destination_label)
            elif destination_type == "entity" and destination_id in tracked_entity_ids:
                destination_label = tracked_entity_labels.get(destination_id, "Company")
                linked_companies.append(destination_label)
            if destination_label:
                intermediary_label = intermediary_by_key[intermediary_key]
                intermediary_labels.append(intermediary_label)
                relationship = edge.relationship_type.replace("_", " ")
                path_phrases.append(
                    f"via {intermediary_label}, which {relationship} {destination_label}"
                )

        linked_holdings = list(dict.fromkeys(linked_holdings))
        linked_companies = list(dict.fromkeys(linked_companies))
        intermediary_labels = list(dict.fromkeys(intermediary_labels))
        path_phrases = list(dict.fromkeys(path_phrases))
        return {
            "linked_holdings": linked_holdings,
            "linked_companies": linked_companies,
            "intermediary_labels": intermediary_labels,
            "path_phrases": path_phrases,
            "reaches_portfolio": reaches_portfolio,
            "portfolio_connection_count": (
                len(linked_holdings)
                + len(linked_companies)
                + (1 if reaches_portfolio else 0)
            ),
            "traversed_connection_count": len(edges),
        }

    @staticmethod
    def _empty_portfolio_route() -> dict[str, Any]:
        return {
            "linked_holdings": [],
            "linked_companies": [],
            "intermediary_labels": [],
            "path_phrases": [],
            "reaches_portfolio": False,
            "portfolio_connection_count": 0,
            "traversed_connection_count": 0,
        }

    async def _subject_mechanism_contexts(
        self,
        *,
        node_type: str,
        node_id: UUID | str,
        node,
    ) -> list[str]:
        subject_pairs: list[tuple[str, UUID]] = []
        try:
            subject_pairs.append((node_type, UUID(str(node_id))))
        except (TypeError, ValueError):
            return []

        if node_type == "position":
            entity_id = (
                await self.session.execute(
                    select(Security.entity_id).where(
                        Security.id == getattr(node, "security_id", None)
                    )
                )
            ).scalar_one_or_none()
            if entity_id is not None:
                subject_pairs.append(("entity", entity_id))

        context_texts: list[str] = []
        for subject_type, subject_id in subject_pairs:
            profile = (
                await self.session.execute(
                    select(Profile)
                    .where(
                        Profile.subject_type == subject_type,
                        Profile.subject_id == subject_id,
                    )
                    .order_by(desc(Profile.updated_at))
                    .limit(1)
                )
            ).scalar_one_or_none()
            if profile is not None:
                context_texts.extend(
                    text
                    for text in (
                        profile.executive_summary,
                        profile.business_model,
                        profile.bull_case,
                        profile.bear_case,
                        profile.key_drivers,
                        profile.competitor_landscape,
                    )
                    if text and text.strip()
                )
            conclusion = (
                await self.session.execute(
                    select(ConclusionState)
                    .where(
                        ConclusionState.subject_type == subject_type,
                        ConclusionState.subject_id == subject_id,
                    )
                    .order_by(desc(ConclusionState.last_updated_at))
                    .limit(1)
                )
            ).scalar_one_or_none()
            if conclusion is not None and conclusion.current_thesis_summary.strip():
                context_texts.append(conclusion.current_thesis_summary)
        return list(dict.fromkeys(context_texts))[:8]

    @staticmethod
    def _connection_phrases(connections: list[GraphConnectionResponse]) -> list[str]:
        phrases: list[str] = []
        for connection in connections:
            relation = connection.relationship_type.replace("_", " ")
            label = connection.label.strip()
            if label:
                phrases.append(f"{relation} {label}")
        return list(dict.fromkeys(phrases))

    def _infer_portfolio_mechanism(
        self,
        *,
        node_label: str,
        node_type: str,
        linked_holdings: list[str],
        linked_companies: list[str],
        all_holdings: list[dict[str, object]],
        direct_portfolio_link: bool,
        context_texts: list[str] | None = None,
    ) -> dict[str, object]:
        top_holdings = [
            str(item.get("label") or item.get("ticker") or "")
            for item in all_holdings[:4]
        ]
        explicit_links = [
            str(item)
            for item in [*linked_holdings, *linked_companies]
            if str(item).strip()
        ]
        context_texts = [
            str(text).strip() for text in (context_texts or []) if str(text).strip()
        ]

        if explicit_links:
            channel = self._portfolio_channel_from_text(
                node_label, explicit_links, context_texts=context_texts
            )
            return {
                "portfolio_mechanism": channel["mechanism"],
                "affected_holdings": explicit_links,
                "next_test": channel["next_test"],
            }

        if direct_portfolio_link:
            channel = self._portfolio_channel_from_text(
                node_label, top_holdings, context_texts=context_texts
            )
            return {
                "portfolio_mechanism": channel["mechanism"],
                "affected_holdings": top_holdings,
                "next_test": channel["next_test"],
            }

        return {}

    def _stored_mechanism_contexts(self, node_type: str, node) -> list[str]:
        contexts: list[str] = []
        properties = getattr(node, "properties_json", None) or {}
        labeled_values = (
            ("Proposed mechanism", properties.get("proposed_mechanism")),
            ("Portfolio mechanism", properties.get("portfolio_mechanism")),
            ("Investment relevance", properties.get("investment_relevance")),
            ("Investment relevance", getattr(node, "investment_relevance", None)),
            ("Dominant channel", getattr(node, "dominant_channel", None)),
            ("Next test", properties.get("next_test")),
            ("Next test", getattr(node, "next_test", None)),
        )
        for label, value in labeled_values:
            if value and str(value).strip():
                contexts.append(f"{label}: {' '.join(str(value).split())}")
        body = self._node_body(node_type, node)
        if body:
            contexts.append(body)
        return list(dict.fromkeys(contexts))

    @staticmethod
    def _portfolio_channel_from_text(
        node_label: str,
        targets: list[str],
        *,
        context_texts: list[str] | None = None,
    ) -> dict[str, str]:
        target_text = (
            ", ".join([target for target in targets[:4] if target])
            or "the tracked portfolio"
        )
        normalized_contexts = [
            " ".join(str(text).split())
            for text in (context_texts or [])
            if str(text or "").strip()
        ]
        mechanism = GraphService._extract_labeled_context(
            normalized_contexts,
            labels=(
                "portfolio mechanism",
                "proposed mechanism",
                "investment relevance",
                "relevance",
                "dominant channel",
            ),
        )
        next_test = GraphService._extract_labeled_context(
            normalized_contexts,
            labels=("next test", "best next check"),
        )
        if mechanism:
            mechanism_text = (
                f"Stored evidence links this node to {target_text}: {mechanism}"
            )
            return {
                "portfolio_mechanism": mechanism_text,
                "mechanism": mechanism_text,
                "next_test": next_test
                or "Attach a source-backed falsifier and measurable outcome to this stored transmission route.",
            }

        relationship_context = "; ".join(normalized_contexts[:3])
        if relationship_context:
            return {
                "portfolio_mechanism": (
                    f"Stored graph evidence links this node to {target_text} through: {relationship_context}. "
                    "The precise economic channel is not explicit enough yet to treat as thesis evidence."
                ),
                "mechanism": (
                    f"Stored graph evidence links this node to {target_text} through: {relationship_context}. "
                    "The precise economic channel is not explicit enough yet to treat as thesis evidence."
                ),
                "next_test": (
                    "Convert the stored relationship into a falsifiable channel: what changes demand, supply, margins, financing, valuation, or timing for the affected holding?"
                ),
            }
        return {
            "portfolio_mechanism": (
                f"The stored relationship makes this node relevant to {target_text}, but the exact driver is not yet explicit. "
                "Treat it as a prompt for channel discovery, not as thesis evidence."
            ),
            "mechanism": (
                f"The stored relationship makes this node relevant to {target_text}, but the exact driver is not yet explicit. "
                "Treat it as a prompt for channel discovery, not as thesis evidence."
            ),
            "next_test": (
                "Name the specific channel first: demand, supply, margins, financing, regulation, valuation, or timing; then attach source-backed evidence to that channel."
            ),
        }

    @staticmethod
    def _extract_labeled_context(
        contexts: list[str], *, labels: tuple[str, ...]
    ) -> str | None:
        all_labels = (
            "portfolio mechanism",
            "proposed mechanism",
            "investment relevance",
            "relevance",
            "dominant channel",
            "next test",
            "best next check",
            "actual/result",
            "price reaction",
            "freshness",
        )
        boundary = "|".join(re.escape(label) for label in all_labels)
        for context in contexts:
            for label in labels:
                match = re.search(
                    rf"(?:^|\s){re.escape(label)}:\s*(.+?)(?=\s(?:{boundary}):|$)",
                    context,
                    flags=re.IGNORECASE,
                )
                if match:
                    return match.group(1).strip()
        return None

    def _node_label(self, node_type: str, node) -> str:
        if node_type == "fact":
            return node.statement
        if node_type == "claim":
            return node.statement
        if node_type == "event":
            return node.title
        if node_type == "fundamental_metric":
            return node.metric_name
        if node_type == "market_setup_signal":
            return node.signal_name
        if node_type == "entity":
            return node.name
        if node_type == "theme":
            return node.name
        if node_type == "historical_episode":
            return node.name
        if node_type == "position":
            security = getattr(node, "security", None)
            entity_name = getattr(getattr(security, "entity", None), "name", None)
            ticker = getattr(security, "ticker", None)
            return ticker or entity_name or "Tracked position"
        if node_type == "source_item":
            return node.summary or "Processed source item"
        if node_type == "raw_evidence":
            return node.title or "Raw evidence"
        if node_type == "source":
            return node.name
        if node_type == "coverage_map":
            return "Coverage status"
        if node_type == "unresolved_question":
            return node.question_text
        if node_type == "conclusion":
            return node.current_thesis_summary or f"{node.current_stance} view"
        if node_type == "review_item":
            return node.trigger_reason or "Review queue item"
        if node_type == "lesson":
            return node.title
        if node_type == "shadow_experiment":
            return node.name
        if node_type == "experiment_result":
            alpha = getattr(node, "alpha", 0.0) or 0.0
            return f"Shadow result {float(alpha):+.2%}"
        if node_type == "portfolio":
            return "Portfolio"
        return str(getattr(node, "id", "unknown"))

    def _node_key(self, node_type: str, node_id: UUID | str) -> str:
        if node_type == "portfolio":
            node_id = SYNTHETIC_PORTFOLIO_ID
        return f"{node_type}:{node_id}"

    async def _node_is_autonomous(self, node_type: str, node: object) -> bool:
        """Resolves if a node was autonomously discovered/proposed."""
        if hasattr(node, "is_autonomous"):
            return bool(getattr(node, "is_autonomous"))

        # If it's an entity, check its profile
        if node_type == "entity":
            from investos.models.profile import Profile

            subject_id = getattr(node, "id", None)
            if subject_id:
                profile = (
                    await self.session.execute(
                        select(Profile).where(
                            Profile.subject_id == subject_id,
                            Profile.subject_type == "entity",
                        )
                    )
                ).scalar_one_or_none()
                if profile:
                    return bool(profile.is_autonomous)
        return False

    def _fundamental_metric_body(self, node) -> str | None:
        """Render a source-dated fundamental metric: value, period, relevance, freshness."""
        period_text = node.period_label or (
            node.as_of.isoformat() if node.as_of else None
        )
        value_bits = [
            str(node.value_text) if node.value_text else None,
            (
                f"{float(node.numeric_value):g}"
                if node.numeric_value is not None
                else None
            ),
            node.unit,
            node.currency,
        ]
        value_text = " ".join(bit for bit in value_bits if bit)
        body_parts = [
            f"Value: {value_text}" if value_text else None,
            f"Period/as-of: {period_text}" if period_text else None,
            (
                f"Relevance: {node.investment_relevance}"
                if node.investment_relevance
                else None
            ),
            (f"Freshness: {node.freshness_status}" if node.freshness_status else None),
            f"Next test: {node.next_test}" if node.next_test else None,
        ]
        return self._compact_body(
            " ".join(part for part in body_parts if part), max_chars=1200
        )

    def _market_setup_signal_body(self, node) -> str | None:
        """Render a setup signal as expectation, outcome, reaction, and next test."""
        body_parts = [
            node.setup_context,
            (f"Actual/result: {node.actual_context}" if node.actual_context else None),
            (f"Price reaction: {node.price_reaction}" if node.price_reaction else None),
            (
                f"Relevance: {node.investment_relevance}"
                if node.investment_relevance
                else None
            ),
            f"Next test: {node.next_test}" if node.next_test else None,
        ]
        return self._compact_body(
            " ".join(part for part in body_parts if part), max_chars=1200
        )

    def _node_body(self, node_type: str, node) -> str | None:
        if node_type in {"fact", "claim"}:
            return self._compact_body(node.statement)
        if node_type == "event":
            return self._compact_body(node.description or node.title)
        if node_type == "fundamental_metric":
            return self._fundamental_metric_body(node)
        if node_type == "market_setup_signal":
            return self._market_setup_signal_body(node)
        if node_type == "entity":
            return self._compact_body(node.description)
        if node_type == "theme":
            if str(node.description or "").strip() == _RECLASSIFIED_THEME_DESCRIPTION:
                return None
            return self._compact_body(node.description)
        if node_type == "historical_episode":
            parts = [
                node.description,
                (
                    f"Dominant channel: {node.dominant_channel}"
                    if node.dominant_channel
                    else None
                ),
                f"Lesson notes: {node.notes}" if node.notes else None,
            ]
            return self._compact_body(
                " ".join(part for part in parts if part), max_chars=1200
            )
        if node_type == "position":
            security = getattr(node, "security", None)
            ticker = getattr(security, "ticker", None) or "This position"
            entity_name = getattr(getattr(security, "entity", None), "name", None)
            quantity = float(node.quantity or 0.0)
            market_value = float(node.market_value or 0.0)
            pnl = float(node.unrealized_pnl or 0.0)
            target = (
                f"{ticker} · {entity_name}"
                if entity_name and entity_name != ticker
                else ticker
            )
            return self._compact_body(
                f"{target} is currently tracked as a {node.list_type.replace('_', ' ')} with "
                f"{quantity:.2f} shares, ${market_value:,.2f} market value, and "
                f"{pnl:+,.2f} unrealized P&L."
            )
        if node_type == "source_item":
            return self._compact_body(
                node.summary or node.extracted_text, max_chars=1100
            )
        if node_type == "raw_evidence":
            return self._compact_body(node.title)
        if node_type == "source":
            return self._compact_body(node.description)
        if node_type == "coverage_map":
            return self._compact_body(
                f"Coverage score is {float(node.overall_coverage_score or 0.0):.1f}. "
                f"High-tier evidence count: {node.high_tier_evidence_count}. "
                f"Contradictions logged: {node.contradiction_count}. "
                f"Unresolved contradictions: {node.unresolved_contradiction_count}."
            )
        if node_type == "unresolved_question":
            return self._compact_body(node.question_text)
        if node_type == "conclusion":
            return self._compact_body(node.current_thesis_summary)
        if node_type == "review_item":
            return self._compact_body(node.trigger_reason)
        if node_type == "lesson":
            return self._compact_body(node.summary)
        if node_type == "shadow_experiment":
            report = (node.final_portfolio_state_json or {}).get("report") or {}
            return self._compact_body(
                report.get("policy_assessment")
                or ((report.get("outcome_summary") or {}).get("reasoning"))
                or node.policy_description
            )
        if node_type == "experiment_result":
            return self._compact_body(node.reasoning)
        return None

    def _node_subtitle(self, node_type: str, node) -> str | None:
        if node_type == "fact":
            return f"{node.fact_type} · {node.importance}"
        if node_type == "claim":
            return f"{node.claim_type} · {node.importance}"
        if node_type == "event":
            return node.event_type
        if node_type == "fundamental_metric":
            ticker = f"{node.ticker} · " if node.ticker else ""
            value = f" · {node.value_text}" if node.value_text else ""
            return f"{ticker}{node.metric_family}{value}"
        if node_type == "market_setup_signal":
            ticker = f"{node.ticker} · " if node.ticker else ""
            return f"{ticker}{node.signal_family}"
        if node_type == "entity":
            return node.entity_type
        if node_type == "theme":
            return node.status
        if node_type == "historical_episode":
            end = node.end_time.year if node.end_time else "present"
            return f"{node.episode_type} · {node.start_time.year}-{end}"
        if node_type == "position":
            quantity = float(node.quantity or 0.0)
            return f"{node.list_type.replace('_', ' ')} · {quantity:.2f} shares"
        if node_type == "source_item":
            return node.processing_status
        if node_type == "raw_evidence":
            return node.source_item_type
        if node_type == "source":
            return node.source_type
        if node_type == "coverage_map":
            return f"coverage {float(node.overall_coverage_score or 0.0):.1f}"
        if node_type == "unresolved_question":
            return f"urgency {node.urgency} · {node.status}"
        if node_type == "conclusion":
            return f"{node.current_stance} · {node.confidence_band}"
        if node_type == "review_item":
            priority = getattr(node, "priority_score", 0.0) or 0.0
            return f"{node.status} · priority {float(priority):.1f}"
        if node_type == "lesson":
            return node.lesson_type
        if node_type == "shadow_experiment":
            return node.run_status
        if node_type == "experiment_result":
            alpha = getattr(node, "alpha", 0.0) or 0.0
            return f"alpha {float(alpha):+.2%}"
        return None

    async def _web_edges_touching_node(
        self,
        *,
        node_type: str,
        node_id: UUID,
        node,
    ) -> list[GraphWebEdgeResponse]:
        output: OrderedDict[UUID, GraphWebEdgeResponse] = OrderedDict()
        for edge in await self._edges_touching_node(
            node_type=node_type, node_id=node_id
        ):
            output[edge.id] = GraphWebEdgeResponse(
                id=edge.id,
                source_key=self._node_key(edge.source_type, edge.source_id),
                target_key=self._node_key(edge.target_type, edge.target_id),
                relationship_type=edge.relationship_type,
                confidence=float(edge.confidence or 0.0),
            )
        for synthetic in await self._synthetic_connections(
            node_type=node_type, node_id=node_id, node=node
        ):
            edge_id = synthetic.edge_id
            if edge_id in output:
                continue
            source_key = (
                self._node_key(node_type, node_id)
                if synthetic.direction == "outgoing"
                else self._node_key(synthetic.node_type, synthetic.node_id)
            )
            target_key = (
                self._node_key(synthetic.node_type, synthetic.node_id)
                if synthetic.direction == "outgoing"
                else self._node_key(node_type, node_id)
            )
            output[edge_id] = GraphWebEdgeResponse(
                id=edge_id,
                source_key=source_key,
                target_key=target_key,
                relationship_type=synthetic.relationship_type,
                confidence=synthetic.confidence,
            )
        return list(output.values())

    async def _synthetic_connections(
        self,
        *,
        node_type: str,
        node_id: UUID,
        node,
    ) -> list[GraphConnectionResponse]:
        specs: list[tuple[str, UUID, str, float, str]] = []

        if node_type == "portfolio":
            positions = (
                (
                    await self.session.execute(
                        select(Position)
                        .where(Position.list_type == "holding", Position.quantity > 0)
                        .order_by(desc(Position.market_value))
                        .limit(10)
                    )
                )
                .scalars()
                .all()
            )
            for p in positions:
                specs.append(("position", p.id, "current_holding", 0.95, "outgoing"))

            profiles = (
                (
                    await self.session.execute(
                        select(Profile).order_by(desc(Profile.updated_at)).limit(40)
                    )
                )
                .scalars()
                .all()
            )
            for p in profiles:
                specs.append(
                    (p.subject_type, p.subject_id, "contains_profile", 0.9, "outgoing")
                )

            # Global lessons stay visible, but only after active holdings and profiles.
            lessons = (
                (
                    await self.session.execute(
                        select(Lesson).order_by(desc(Lesson.created_at)).limit(5)
                    )
                )
                .scalars()
                .all()
            )
            for l in lessons:
                specs.append(
                    ("lesson", l.id, "learned_global_lesson", 0.85, "outgoing")
                )
            metrics = (
                (
                    await self.session.execute(
                        select(FundamentalMetric)
                        .where(FundamentalMetric.subject_type == "portfolio")
                        .order_by(
                            desc(FundamentalMetric.as_of),
                            desc(FundamentalMetric.public_time),
                            desc(FundamentalMetric.created_at),
                        )
                        .limit(10)
                    )
                )
                .scalars()
                .all()
            )
            for metric in metrics:
                specs.append(
                    (
                        "fundamental_metric",
                        metric.id,
                        "has_fundamental_metric",
                        0.84,
                        "outgoing",
                    )
                )
            setup_signals = (
                (
                    await self.session.execute(
                        select(MarketSetupSignal)
                        .where(MarketSetupSignal.subject_type == "portfolio")
                        .order_by(
                            desc(MarketSetupSignal.public_time),
                            desc(MarketSetupSignal.created_at),
                        )
                        .limit(8)
                    )
                )
                .scalars()
                .all()
            )
            for signal in setup_signals:
                specs.append(
                    (
                        "market_setup_signal",
                        signal.id,
                        "has_market_setup",
                        0.82,
                        "outgoing",
                    )
                )
        elif node_type in {"entity", "theme"}:
            specs.extend(
                await self._subject_operating_specs(
                    subject_type=node_type, subject_id=node_id
                )
            )
        elif node_type == "position":
            specs.extend(await self._position_specs(node))
        elif node_type in {
            "fact",
            "claim",
            "event",
            "source_item",
            "fundamental_metric",
            "market_setup_signal",
        }:
            specs.extend(
                await self._knowledge_context_specs(node_type=node_type, node=node)
            )
        elif node_type == "conclusion":
            specs.extend(await self._conclusion_specs(node))
        elif node_type == "coverage_map":
            specs.extend(await self._coverage_specs(node))
        elif node_type == "unresolved_question":
            specs.extend(await self._question_specs(node))
        elif node_type == "review_item":
            specs.extend(await self._review_specs(node))
        elif node_type == "shadow_experiment":
            specs.extend(await self._shadow_specs(node))
        elif node_type == "experiment_result":
            specs.extend(await self._experiment_result_specs(node))
        elif node_type == "lesson":
            specs.extend(await self._lesson_specs(node))
        elif node_type == "raw_evidence":
            if getattr(node, "source_id", None):
                specs.append(("source", node.source_id, "from_source", 0.9, "incoming"))
            # Link back to items it contains
            items = (
                (
                    await self.session.execute(
                        select(SourceItem).where(SourceItem.raw_evidence_id == node_id)
                    )
                )
                .scalars()
                .all()
            )
            for item in items:
                specs.append(
                    ("source_item", item.id, "extracted_item", 0.85, "outgoing")
                )
        elif node_type == "source":
            # Global source link
            specs.append(
                (
                    "portfolio",
                    SYNTHETIC_PORTFOLIO_ID,
                    "source_for_portfolio",
                    0.7,
                    "incoming",
                )
            )

        connections: list[GraphConnectionResponse] = []
        seen: set[tuple[str, UUID, str]] = set()
        for other_type, other_id, relationship_type, confidence, direction in specs:
            other_node = await self._load_node(other_type, other_id)
            if other_node is None:
                continue
            key = (direction, other_id, relationship_type)
            if key in seen:
                continue
            seen.add(key)
            connections.append(
                GraphConnectionResponse(
                    edge_id=self._synthetic_edge_id(
                        source_type=(
                            node_type if direction == "outgoing" else other_type
                        ),
                        source_id=node_id if direction == "outgoing" else other_id,
                        target_type=(
                            other_type if direction == "outgoing" else node_type
                        ),
                        target_id=other_id if direction == "outgoing" else node_id,
                        relationship_type=relationship_type,
                    ),
                    direction=direction,
                    relationship_type=relationship_type,
                    confidence=confidence,
                    node_id=other_id,
                    node_type=other_type,
                    label=self._node_label(other_type, other_node),
                    subtitle=self._node_subtitle(other_type, other_node),
                    tier=getattr(other_node, "tier", None),
                    created_at=getattr(other_node, "created_at", None),
                )
            )
        return connections

    async def _subject_operating_specs(
        self, *, subject_type: str, subject_id: UUID
    ) -> list[tuple[str, UUID, str, float, str]]:
        specs: list[tuple[str, UUID, str, float, str]] = []
        conclusion = await self._subject_conclusion(subject_type, subject_id)
        coverage = await self._subject_coverage(subject_type, subject_id)
        if conclusion is not None:
            specs.append(
                ("conclusion", conclusion.id, "has_accepted_state", 0.95, "outgoing")
            )
        if coverage is not None:
            specs.append(
                ("coverage_map", coverage.id, "monitored_by", 0.82, "outgoing")
            )
            questions = (
                (
                    await self.session.execute(
                        select(UnresolvedQuestion)
                        .where(
                            UnresolvedQuestion.coverage_map_id == coverage.id,
                            UnresolvedQuestion.status.in_(["open", "investigating"]),
                        )
                        .order_by(
                            desc(UnresolvedQuestion.urgency),
                            desc(UnresolvedQuestion.created_at),
                        )
                        .limit(6)
                    )
                )
                .scalars()
                .all()
            )
            for question in questions:
                specs.append(
                    (
                        "unresolved_question",
                        question.id,
                        "watching_for",
                        0.86,
                        "outgoing",
                    )
                )
        review_items = await self._subject_review_items(
            subject_type,
            subject_id,
            conclusion_id=conclusion.id if conclusion else None,
        )
        for review in review_items[:6]:
            specs.append(("review_item", review.id, "needs_review", 0.88, "outgoing"))
        if subject_type == "entity":
            positions = await self._entity_positions(subject_id)
            for position in positions[:4]:
                relationship = (
                    "currently_held"
                    if position.list_type == "holding"
                    else f"tracked_as_{position.list_type}"
                )
                specs.append(("position", position.id, relationship, 0.9, "outgoing"))
            metrics = (
                (
                    await self.session.execute(
                        select(FundamentalMetric)
                        .where(
                            or_(
                                FundamentalMetric.entity_id == subject_id,
                                (
                                    (FundamentalMetric.subject_type == "entity")
                                    & (FundamentalMetric.subject_id == subject_id)
                                ),
                            )
                        )
                        .order_by(
                            desc(FundamentalMetric.as_of),
                            desc(FundamentalMetric.public_time),
                            desc(FundamentalMetric.created_at),
                        )
                        .limit(8)
                    )
                )
                .scalars()
                .all()
            )
            for metric in metrics:
                specs.append(
                    (
                        "fundamental_metric",
                        metric.id,
                        "has_fundamental_metric",
                        0.86,
                        "outgoing",
                    )
                )
            setup_signals = (
                (
                    await self.session.execute(
                        select(MarketSetupSignal)
                        .where(
                            or_(
                                MarketSetupSignal.entity_id == subject_id,
                                (
                                    (MarketSetupSignal.subject_type == "entity")
                                    & (MarketSetupSignal.subject_id == subject_id)
                                ),
                            )
                        )
                        .order_by(
                            desc(MarketSetupSignal.public_time),
                            desc(MarketSetupSignal.created_at),
                        )
                        .limit(6)
                    )
                )
                .scalars()
                .all()
            )
            for signal in setup_signals:
                specs.append(
                    (
                        "market_setup_signal",
                        signal.id,
                        "has_market_setup",
                        0.84,
                        "outgoing",
                    )
                )
            experiments = await self._entity_shadow_experiments(subject_id)
            for experiment in experiments[:4]:
                specs.append(
                    (
                        "shadow_experiment",
                        experiment.id,
                        "tested_in_shadow",
                        0.8,
                        "outgoing",
                    )
                )
        return specs

    async def _position_specs(
        self, position: Position
    ) -> list[tuple[str, UUID, str, float, str]]:
        specs: list[tuple[str, UUID, str, float, str]] = []
        security = (
            await self.session.execute(
                select(Security).where(Security.id == position.security_id)
            )
        ).scalar_one_or_none()
        if security is not None:
            specs.append(
                (
                    "entity",
                    security.entity_id,
                    "represents_exposure_to",
                    0.95,
                    "outgoing",
                )
            )
        specs.append(
            (
                "portfolio",
                SYNTHETIC_PORTFOLIO_ID,
                "belongs_to_portfolio",
                0.8,
                "incoming",
            )
        )
        return specs

    async def _knowledge_context_specs(
        self, *, node_type: str, node
    ) -> list[tuple[str, UUID, str, float, str]]:
        specs: list[tuple[str, UUID, str, float, str]] = []
        source_items = await self._source_items_for_extracted_node(node_type, node)
        seen: set[tuple[str, UUID, str]] = set()

        for source_item in source_items[:2]:
            key = ("source_item", source_item.id, "shares_source_context")
            if node_type != "source_item" and key not in seen:
                seen.add(key)
                specs.append(
                    (
                        "source_item",
                        source_item.id,
                        "shares_source_context",
                        0.84,
                        "outgoing",
                    )
                )

            sibling_specs = await self._sibling_evidence_specs(
                source_item.id, getattr(node, "id", None)
            )
            for sibling in sibling_specs:
                sibling_key = (sibling[0], sibling[1], sibling[2])
                if sibling_key in seen:
                    continue
                seen.add(sibling_key)
                specs.append(sibling)
        return specs

    async def _conclusion_specs(
        self, conclusion: ConclusionState
    ) -> list[tuple[str, UUID, str, float, str]]:
        specs: list[tuple[str, UUID, str, float, str]] = [
            (
                conclusion.subject_type,
                conclusion.subject_id,
                "describes",
                0.95,
                "outgoing",
            )
        ]
        coverage = await self._subject_coverage(
            conclusion.subject_type, conclusion.subject_id
        )
        if coverage is not None:
            specs.append(("coverage_map", coverage.id, "measured_by", 0.82, "outgoing"))
            questions = (
                (
                    await self.session.execute(
                        select(UnresolvedQuestion)
                        .where(
                            UnresolvedQuestion.coverage_map_id == coverage.id,
                            UnresolvedQuestion.status.in_(["open", "investigating"]),
                        )
                        .order_by(
                            desc(UnresolvedQuestion.urgency),
                            desc(UnresolvedQuestion.created_at),
                        )
                        .limit(6)
                    )
                )
                .scalars()
                .all()
            )
            for question in questions:
                specs.append(
                    (
                        "unresolved_question",
                        question.id,
                        "exposes_gap",
                        0.82,
                        "outgoing",
                    )
                )
        reviews = (
            (
                await self.session.execute(
                    select(ReviewQueueItem)
                    .where(
                        ReviewQueueItem.item_type == "conclusion",
                        ReviewQueueItem.item_id == conclusion.id,
                    )
                    .order_by(
                        desc(ReviewQueueItem.priority_score),
                        desc(ReviewQueueItem.created_at),
                    )
                    .limit(4)
                )
            )
            .scalars()
            .all()
        )
        for review in reviews:
            specs.append(("review_item", review.id, "needs_review", 0.9, "outgoing"))
        return specs

    async def _coverage_specs(
        self, coverage: CoverageMap
    ) -> list[tuple[str, UUID, str, float, str]]:
        specs: list[tuple[str, UUID, str, float, str]] = [
            (
                coverage.subject_type,
                coverage.subject_id,
                "about_subject",
                0.82,
                "outgoing",
            )
        ]
        conclusion = await self._subject_conclusion(
            coverage.subject_type, coverage.subject_id
        )
        if conclusion is not None:
            specs.append(("conclusion", conclusion.id, "scores", 0.8, "outgoing"))
        questions = (
            (
                await self.session.execute(
                    select(UnresolvedQuestion)
                    .where(
                        UnresolvedQuestion.coverage_map_id == coverage.id,
                        UnresolvedQuestion.status.in_(["open", "investigating"]),
                    )
                    .order_by(
                        desc(UnresolvedQuestion.urgency),
                        desc(UnresolvedQuestion.created_at),
                    )
                    .limit(8)
                )
            )
            .scalars()
            .all()
        )
        for question in questions:
            specs.append(
                ("unresolved_question", question.id, "tracks_gap", 0.86, "outgoing")
            )
        return specs

    async def _question_specs(
        self, question: UnresolvedQuestion
    ) -> list[tuple[str, UUID, str, float, str]]:
        specs: list[tuple[str, UUID, str, float, str]] = [
            ("coverage_map", question.coverage_map_id, "tracked_in", 0.86, "outgoing")
        ]
        coverage = (
            await self.session.execute(
                select(CoverageMap).where(CoverageMap.id == question.coverage_map_id)
            )
        ).scalar_one_or_none()
        if coverage is not None:
            specs.append(
                (
                    coverage.subject_type,
                    coverage.subject_id,
                    "about_subject",
                    0.84,
                    "outgoing",
                )
            )
        return specs

    async def _review_specs(
        self, review: ReviewQueueItem
    ) -> list[tuple[str, UUID, str, float, str]]:
        target_type = self._review_graph_type(review.item_type)
        if target_type is None:
            return []
        return [(target_type, review.item_id, "reviews", 0.88, "outgoing")]

    async def _shadow_specs(
        self, experiment: ShadowExperiment
    ) -> list[tuple[str, UUID, str, float, str]]:
        specs: list[tuple[str, UUID, str, float, str]] = []
        result = (
            await self.session.execute(
                select(ExperimentResult).where(
                    ExperimentResult.experiment_id == experiment.id
                )
            )
        ).scalar_one_or_none()
        if result is not None:
            specs.append(
                ("experiment_result", result.id, "produced_result", 0.94, "outgoing")
            )
            lessons = (
                (
                    await self.session.execute(
                        select(Lesson)
                        .join(
                            LessonObservation,
                            LessonObservation.lesson_id == Lesson.id,
                        )
                        .where(LessonObservation.experiment_result_id == result.id)
                        .order_by(desc(Lesson.created_at))
                        .limit(4)
                    )
                )
                .scalars()
                .all()
            )
            for lesson in lessons:
                specs.append(("lesson", lesson.id, "taught", 0.9, "outgoing"))
        for entity_id in await self._shadow_entity_ids(experiment):
            specs.append(("entity", entity_id, "simulates", 0.76, "outgoing"))
        return specs

    async def _experiment_result_specs(
        self, result: ExperimentResult
    ) -> list[tuple[str, UUID, str, float, str]]:
        specs: list[tuple[str, UUID, str, float, str]] = [
            ("shadow_experiment", result.experiment_id, "result_of", 0.94, "outgoing")
        ]
        lessons = (
            (
                await self.session.execute(
                    select(Lesson)
                    .join(
                        LessonObservation,
                        LessonObservation.lesson_id == Lesson.id,
                    )
                    .where(LessonObservation.experiment_result_id == result.id)
                    .order_by(desc(Lesson.created_at))
                    .limit(4)
                )
            )
            .scalars()
            .all()
        )
        for lesson in lessons:
            specs.append(("lesson", lesson.id, "generated_lesson", 0.9, "outgoing"))
        return specs

    async def _lesson_specs(
        self, lesson: Lesson
    ) -> list[tuple[str, UUID, str, float, str]]:
        result_ids = list(
            (
                await self.session.execute(
                    select(LessonObservation.experiment_result_id).where(
                        LessonObservation.lesson_id == lesson.id
                    )
                )
            )
            .scalars()
            .all()
        )
        if not result_ids and lesson.originating_experiment_result_id:
            result_ids = [lesson.originating_experiment_result_id]
        return [
            ("experiment_result", result_id, "learned_from", 0.9, "outgoing")
            for result_id in result_ids[:12]
        ]

    async def _subject_coverage(
        self, subject_type: str, subject_id: UUID
    ) -> CoverageMap | None:
        return await self.canonical.get_coverage_map(
            subject_type=subject_type,
            subject_id=subject_id,
        )

    async def _subject_conclusion(
        self, subject_type: str, subject_id: UUID
    ) -> ConclusionState | None:
        return await self.canonical.get_conclusion_state(
            subject_type=subject_type,
            subject_id=subject_id,
        )

    async def _subject_review_items(
        self,
        subject_type: str,
        subject_id: UUID,
        *,
        conclusion_id: UUID | None,
    ) -> list[ReviewQueueItem]:
        clauses = [
            (ReviewQueueItem.item_type == subject_type)
            & (ReviewQueueItem.item_id == subject_id)
        ]
        if conclusion_id is not None:
            clauses.append(
                (ReviewQueueItem.item_type == "conclusion")
                & (ReviewQueueItem.item_id == conclusion_id)
            )
        return list(
            (
                await self.session.execute(
                    select(ReviewQueueItem)
                    .where(or_(*clauses))
                    .order_by(
                        desc(ReviewQueueItem.priority_score),
                        desc(ReviewQueueItem.created_at),
                    )
                    .limit(8)
                )
            )
            .scalars()
            .all()
        )

    async def _entity_shadow_experiments(
        self, entity_id: UUID
    ) -> list[ShadowExperiment]:
        security_rows = (
            (
                await self.session.execute(
                    select(Security).where(Security.entity_id == entity_id)
                )
            )
            .scalars()
            .all()
        )
        security_ids = [str(item.id) for item in security_rows]
        if not security_ids:
            return []
        experiments = (
            (
                await self.session.execute(
                    select(ShadowExperiment)
                    .order_by(desc(ShadowExperiment.created_at))
                    .limit(40)
                )
            )
            .scalars()
            .all()
        )
        matched: list[ShadowExperiment] = []
        for experiment in experiments:
            positions = (experiment.initial_portfolio_state_json or {}).get(
                "positions"
            ) or []
            if any(
                (position.get("security_id") in security_ids) for position in positions
            ):
                matched.append(experiment)
        return matched[:6]

    async def _shadow_entity_ids(self, experiment: ShadowExperiment) -> list[UUID]:
        positions = (experiment.initial_portfolio_state_json or {}).get(
            "positions"
        ) or []
        security_ids: list[UUID] = []
        for position in positions:
            raw_security_id = position.get("security_id")
            if not raw_security_id:
                continue
            try:
                security_ids.append(UUID(str(raw_security_id)))
            except (ValueError, TypeError):
                continue
        if not security_ids:
            return []
        security_rows = (
            (
                await self.session.execute(
                    select(Security).where(Security.id.in_(security_ids))
                )
            )
            .scalars()
            .all()
        )
        entity_ids = OrderedDict((item.entity_id, None) for item in security_rows)
        return list(entity_ids.keys())[:8]

    async def _entity_positions(self, entity_id: UUID) -> list[Position]:
        return list(
            (
                await self.session.execute(
                    select(Position)
                    .join(Security, Position.security_id == Security.id)
                    .where(
                        Security.entity_id == entity_id,
                        Position.list_type.in_(["holding", "watchlist", "considering"]),
                    )
                    .order_by(desc(Position.market_value), desc(Position.added_at))
                    .limit(6)
                )
            )
            .scalars()
            .all()
        )

    async def _sibling_evidence_specs(
        self,
        source_item_id: UUID,
        current_node_id: UUID | None,
    ) -> list[tuple[str, UUID, str, float, str]]:
        specs: list[tuple[str, UUID, str, float, str]] = []
        for node_type, model in (("claim", Claim), ("fact", Fact)):
            rows = (
                (
                    await self.session.execute(
                        select(model)
                        .where(model.source_item_id == source_item_id)
                        .order_by(desc(model.created_at))
                        .limit(6)
                    )
                )
                .scalars()
                .all()
            )
            for row in rows:
                if current_node_id is not None and row.id == current_node_id:
                    continue
                specs.append(
                    (node_type, row.id, "shares_source_context", 0.76, "outgoing")
                )

        event_edges = (
            (
                await self.session.execute(
                    select(Edge)
                    .where(
                        Edge.source_type == "event",
                        Edge.target_type == "source_item",
                        Edge.target_id == source_item_id,
                    )
                    .order_by(desc(Edge.created_at))
                    .limit(4)
                )
            )
            .scalars()
            .all()
        )
        for edge in event_edges:
            if current_node_id is not None and edge.source_id == current_node_id:
                continue
            specs.append(
                ("event", edge.source_id, "shares_source_context", 0.76, "outgoing")
            )
        return specs[:8]

    def _review_graph_type(self, item_type: str) -> str | None:
        mapping = {
            "entity": "entity",
            "theme": "theme",
            "conclusion": "conclusion",
            "shadow_experiment": "shadow_experiment",
        }
        return mapping.get(item_type)

    def _synthetic_edge_id(
        self,
        *,
        source_type: str,
        source_id: UUID,
        target_type: str,
        target_id: UUID,
        relationship_type: str,
    ) -> UUID:
        return uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"{source_type}:{source_id}:{relationship_type}:{target_type}:{target_id}",
        )

    async def _source_items_for_evidence_ids(
        self, evidence_ids: list[UUID]
    ) -> list[SourceItem]:
        output: list[SourceItem] = []
        for evidence_id in evidence_ids:
            for node_type in ("fact", "claim", "event"):
                node = await self._load_node(node_type, evidence_id)
                if node is None:
                    continue
                output.extend(
                    await self._source_items_for_extracted_node(node_type, node)
                )
                break
            else:
                raw = await self._load_node("raw_evidence", evidence_id)
                if raw is not None:
                    source_item = (
                        (
                            await self.session.execute(
                                select(SourceItem)
                                .where(SourceItem.raw_evidence_id == raw.id)
                                .order_by(desc(SourceItem.created_at))
                            )
                        )
                        .scalars()
                        .first()
                    )
                    if source_item is not None:
                        output.append(source_item)
        return output

    async def _source_items_matching_text(self, text: str) -> list[SourceItem]:
        terms = [
            token.strip() for token in re.split(r"\W+", text) if len(token.strip()) >= 4
        ][:4]
        if not terms:
            return []
        clauses = [SourceItem.summary.ilike(f"%{term}%") for term in terms]
        return list(
            (
                await self.session.execute(
                    select(SourceItem)
                    .where(or_(*clauses))
                    .order_by(desc(SourceItem.created_at))
                    .limit(4)
                )
            )
            .scalars()
            .all()
        )

    def _compact_body(self, text: str | None, *, max_chars: int = 900) -> str | None:
        if not text:
            return None
        cleaned = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1", text)
        cleaned = re.sub(r"https?://\S+", "", cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        if len(cleaned) <= max_chars:
            return cleaned
        return cleaned[: max_chars - 1].rstrip() + "…"
