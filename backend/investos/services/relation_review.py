from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import desc, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from investos.core.llm import call_llm_json
from investos.models.entity import Entity, Security
from investos.models.graph import Edge
from investos.models.knowledge import Claim, Event, Fact
from investos.models.portfolio import Position
from investos.models.profile import Profile
from investos.models.theme import Theme
from investos.services.agent_action_log import AgentActionLogService
from investos.services.graph_edge_state import GraphEdgeStateService
from investos.services.portfolio_peers import PortfolioPeerContextService

RELATION_REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "overall_reasoning": {"type": "string"},
        "links": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "target_key": {"type": "string"},
                    "relationship_type": {
                        "type": "string",
                        "enum": [
                            "contextualizes",
                            "affects",
                            "supports",
                            "contradicts",
                            "depends_on",
                            "benefits",
                            "pressures",
                            "competes_with",
                            "exposes",
                        ],
                    },
                    "confidence": {"type": "number"},
                    "reasoning": {"type": "string"},
                },
                "required": [
                    "target_key",
                    "relationship_type",
                    "confidence",
                    "reasoning",
                ],
                "additionalProperties": False,
            },
        },
    },
    "required": ["overall_reasoning", "links"],
    "additionalProperties": False,
}


@dataclass
class ReviewTarget:
    key: str
    subject_type: str
    subject_id: UUID
    name: str
    rationale: str


class RelationReviewService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.edge_state = GraphEdgeStateService(session)

    async def review_next_subject(self) -> dict[str, Any]:
        peer_links_added = await PortfolioPeerContextService(
            self.session
        ).ensure_peer_edges(limit=10)
        targets = await self._target_catalog()
        if not targets:
            if peer_links_added:
                await self.session.commit()
                return {
                    "status": "ok",
                    "detail": f"peer_links_added={peer_links_added}",
                    "links_added": peer_links_added,
                    "peer_links_added": peer_links_added,
                }
            return {
                "status": "idle",
                "detail": "no_relation_targets",
                "links_added": 0,
                "peer_links_added": 0,
            }

        candidate = await self._select_candidate()
        if candidate is None:
            if peer_links_added:
                await self.session.commit()
                return {
                    "status": "ok",
                    "detail": f"peer_links_added={peer_links_added}",
                    "links_added": peer_links_added,
                    "peer_links_added": peer_links_added,
                }
            return {
                "status": "idle",
                "detail": "no_relation_review_candidate",
                "links_added": 0,
                "peer_links_added": 0,
            }

        profile, subject_name = candidate
        snippets = await self._subject_evidence_snippets(
            profile.subject_type, profile.subject_id
        )
        existing = await self._existing_relation_keys(
            profile.subject_type, profile.subject_id
        )

        result = await call_llm_json(
            system_prompt=(
                "You are Prophet relation review. "
                "Your job is to reconnect stored research subjects back to the live portfolio context without inventing links. "
                "Be conservative, but do not miss meaningful second-order connections. "
                "Only propose links when there is a concrete portfolio implication, thematic dependency, competitor relation, "
                "or causal/contextual connection that would matter for later reasoning or review. "
                "Do not create vague 'AI is connected to everything' links. "
                "Use only the provided target_key values. "
                "If nothing is meaningful, return an empty links array."
            ),
            user_prompt=json.dumps(
                {
                    "subject": {
                        "subject_type": profile.subject_type,
                        "subject_id": str(profile.subject_id),
                        "name": subject_name,
                        "summary": profile.executive_summary,
                        "existing_relationships": sorted(existing),
                    },
                    "recent_evidence_snippets": snippets,
                    "portfolio_targets": [
                        {
                            "target_key": target.key,
                            "name": target.name,
                            "subject_type": target.subject_type,
                            "rationale": target.rationale,
                        }
                        for target in targets
                    ],
                },
                ensure_ascii=True,
            ),
            schema=RELATION_REVIEW_SCHEMA,
            timeout_seconds=25,
        )

        target_map = {target.key: target for target in targets}
        links_added = 0
        for payload in result.get("links", [])[:4]:
            target = target_map.get(str(payload.get("target_key") or ""))
            if target is None:
                continue
            if (
                target.subject_type == profile.subject_type
                and target.subject_id == profile.subject_id
            ):
                continue
            confidence = float(payload.get("confidence") or 0.0)
            if confidence < 0.72:
                continue
            created = await self._upsert_edge(
                source_type=profile.subject_type,
                source_id=profile.subject_id,
                target_type=target.subject_type,
                target_id=target.subject_id,
                relationship_type=str(
                    payload.get("relationship_type") or "contextualizes"
                ),
                confidence=confidence,
                reasoning=str(
                    payload.get("reasoning") or result.get("overall_reasoning") or ""
                ).strip(),
            )
            links_added += 1 if created else 0

        await self.session.commit()
        return {
            "status": "ok",
            "detail": f"reviewed={subject_name} links_added={links_added} peer_links_added={peer_links_added}",
            "subject_id": str(profile.subject_id),
            "subject_type": profile.subject_type,
            "subject_name": subject_name,
            "links_added": links_added + peer_links_added,
            "subject_links_added": links_added,
            "peer_links_added": peer_links_added,
            "overall_reasoning": result.get("overall_reasoning"),
        }

    async def _target_catalog(self) -> list[ReviewTarget]:
        targets: list[ReviewTarget] = []
        portfolio_profile = (
            await self.session.execute(
                select(Profile)
                .where(Profile.subject_type == "portfolio")
                .order_by(desc(Profile.updated_at))
                .limit(1)
            )
        ).scalar_one_or_none()
        if portfolio_profile is not None:
            targets.append(
                ReviewTarget(
                    key=f"portfolio:{portfolio_profile.subject_id}",
                    subject_type="portfolio",
                    subject_id=portfolio_profile.subject_id,
                    name="Portfolio",
                    rationale="Global portfolio context, capital allocation, and book-level risk.",
                )
            )
        rows = (
            await self.session.execute(
                select(Position, Security, Entity)
                .join(Security, Position.security_id == Security.id)
                .join(Entity, Security.entity_id == Entity.id)
                .where(Position.list_type == "holding", Position.quantity > 0)
                .order_by(desc(Position.market_value))
            )
        ).all()
        seen: set[tuple[str, UUID]] = set()
        if portfolio_profile is not None:
            seen.add(("portfolio", portfolio_profile.subject_id))
        for position, security, entity in rows:
            key = ("entity", entity.id)
            if key in seen:
                continue
            seen.add(key)
            targets.append(
                ReviewTarget(
                    key=f"entity:{entity.id}",
                    subject_type="entity",
                    subject_id=entity.id,
                    name=entity.name,
                    rationale=f"Active holding {security.ticker} with market value {float(position.market_value or 0):.2f}.",
                )
            )

        theme_rows = (
            (
                await self.session.execute(
                    select(Theme).order_by(desc(Theme.last_updated_at)).limit(8)
                )
            )
            .scalars()
            .all()
        )
        for theme in theme_rows:
            key = ("theme", theme.id)
            if key in seen:
                continue
            seen.add(key)
            targets.append(
                ReviewTarget(
                    key=f"theme:{theme.id}",
                    subject_type="theme",
                    subject_id=theme.id,
                    name=theme.name,
                    rationale=f"Active research theme currently tracked as {theme.status}.",
                )
            )
        return targets

    async def _select_candidate(self) -> tuple[Profile, str] | None:
        profiles = (
            (
                await self.session.execute(
                    select(Profile)
                    .where(Profile.subject_type.in_(["entity", "theme", "portfolio"]))
                    .order_by(desc(Profile.updated_at))
                    .limit(30)
                )
            )
            .scalars()
            .all()
        )
        for profile in profiles:
            if AgentActionLogService.has_recent_subject_attempt(
                str(profile.subject_id),
                subject_type=profile.subject_type,
                action_type="relation_review",
                within_seconds=12 * 60 * 60,
            ):
                continue
            if profile.subject_type == "entity":
                entity = (
                    await self.session.execute(
                        select(Entity).where(Entity.id == profile.subject_id)
                    )
                ).scalar_one_or_none()
                if entity is None:
                    continue
                return profile, entity.name
            if profile.subject_type == "portfolio":
                return profile, "Portfolio"
            theme = (
                await self.session.execute(
                    select(Theme).where(Theme.id == profile.subject_id)
                )
            ).scalar_one_or_none()
            if theme is None:
                continue
            return profile, theme.name
        return None

    async def _subject_evidence_snippets(
        self, subject_type: str, subject_id: UUID
    ) -> list[dict[str, str]]:
        edges = (
            (
                await self.session.execute(
                    select(Edge)
                    .where(
                        Edge.target_type == subject_type,
                        Edge.target_id == subject_id,
                        Edge.source_type.in_(["fact", "claim", "event"]),
                    )
                    .order_by(desc(Edge.created_at))
                    .limit(8)
                )
            )
            .scalars()
            .all()
        )
        snippets: list[dict[str, str]] = []
        for edge in edges:
            text = await self._source_node_text(edge.source_type, edge.source_id)
            if not text:
                continue
            snippets.append(
                {
                    "node_type": edge.source_type,
                    "relationship_type": edge.relationship_type,
                    "text": text,
                }
            )
        return snippets[:5]

    async def _source_node_text(self, node_type: str, node_id: UUID) -> str | None:
        if node_type == "fact":
            node = (
                await self.session.execute(
                    select(Fact).where(
                        Fact.id == node_id,
                        Fact.is_deprecated.is_(False),
                    )
                )
            ).scalar_one_or_none()
            return None if node is None else node.statement
        if node_type == "claim":
            node = (
                await self.session.execute(
                    select(Claim).where(
                        Claim.id == node_id,
                        Claim.is_deprecated.is_(False),
                    )
                )
            ).scalar_one_or_none()
            return None if node is None else node.statement
        if node_type == "event":
            node = (
                await self.session.execute(
                    select(Event).where(
                        Event.id == node_id,
                        Event.is_deprecated.is_(False),
                    )
                )
            ).scalar_one_or_none()
            return None if node is None else node.description or node.title
        return None

    async def _existing_relation_keys(
        self, subject_type: str, subject_id: UUID
    ) -> set[str]:
        rows = (
            (
                await self.session.execute(
                    select(Edge).where(
                        or_(
                            (Edge.source_type == subject_type)
                            & (Edge.source_id == subject_id),
                            (Edge.target_type == subject_type)
                            & (Edge.target_id == subject_id),
                        )
                    )
                )
            )
            .scalars()
            .all()
        )
        keys: set[str] = set()
        for edge in rows:
            if edge.source_type == subject_type and edge.source_id == subject_id:
                keys.add(
                    f"{edge.relationship_type}->{edge.target_type}:{edge.target_id}"
                )
            elif edge.target_type == subject_type and edge.target_id == subject_id:
                keys.add(
                    f"{edge.relationship_type}<-{edge.source_type}:{edge.source_id}"
                )
        return keys

    async def _upsert_edge(
        self,
        *,
        source_type: str,
        source_id: UUID,
        target_type: str,
        target_id: UUID,
        relationship_type: str,
        confidence: float,
        reasoning: str,
    ) -> bool:
        if source_type == "portfolio" and target_type == "portfolio":
            return False
        properties = {
            "origin": "relation_review",
            "reviewed_via": "automation",
            "confidence": confidence,
        }
        _, created = await self.edge_state.ensure_edge(
            source_type=source_type,
            source_id=source_id,
            target_type=target_type,
            target_id=target_id,
            relationship_type=relationship_type,
            confidence=confidence,
            reasoning=reasoning,
            properties=properties,
        )
        return created
