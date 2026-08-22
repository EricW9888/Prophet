from __future__ import annotations

import re
from typing import Any
from uuid import UUID

from sqlalchemy import desc, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from investos.models.entity import Entity, Security
from investos.models.graph import Edge
from investos.models.knowledge import Claim, Event, Fact
from investos.models.portfolio import Position
from investos.services.graph_edge_state import GraphEdgeStateService


class PortfolioPeerContextService:
    """Find portfolio names that should be reasoned together."""

    def __init__(self, session: AsyncSession):
        self.session = session
        self.edge_state = GraphEdgeStateService(session)

    async def peer_exposures(
        self,
        *,
        subject_id: UUID | None = None,
        list_types: tuple[str, ...] = ("holding", "watchlist", "considering"),
        limit: int = 12,
    ) -> list[dict[str, object]]:
        rows = (
            await self.session.execute(
                select(Position, Security, Entity)
                .join(Security, Position.security_id == Security.id)
                .join(Entity, Security.entity_id == Entity.id)
                .where(Position.list_type.in_(list_types))
                .order_by(desc(Position.market_value), Position.added_at.desc())
            )
        ).all()
        descriptors = [
            await self._descriptor(position=position, security=security, entity=entity)
            for position, security, entity in rows
        ]
        return self.score_descriptors(descriptors, subject_id=subject_id, limit=limit)

    async def ensure_peer_edges(self, *, limit: int = 12) -> int:
        exposures = await self.peer_exposures(list_types=("holding",), limit=limit)
        links_added = 0
        for exposure in exposures:
            confidence = float(exposure.get("confidence") or 0.0)
            if confidence < 0.3:
                continue
            source_id = UUID(str(exposure["source_entity_id"]))
            target_id = UUID(str(exposure["target_entity_id"]))
            if source_id == target_id:
                continue
            if await self._has_relation(source_id, target_id):
                continue
            _, created = await self.edge_state.ensure_edge(
                source_type="entity",
                source_id=source_id,
                target_type="entity",
                target_id=target_id,
                relationship_type="contextualizes",
                confidence=confidence,
                reasoning=str(
                    exposure.get("reason")
                    or "Portfolio peer exposure discovered from holdings context."
                ),
                properties={
                    "origin": "portfolio_peer_context",
                    "reviewed_via": "relation_review",
                    "shared_terms": exposure.get("shared_terms") or [],
                },
            )
            links_added += 1 if created else 0
        return links_added

    @classmethod
    def score_descriptors(
        cls,
        descriptors: list[dict[str, object]],
        *,
        subject_id: UUID | None = None,
        limit: int = 12,
    ) -> list[dict[str, object]]:
        common_terms = cls._common_terms(descriptors)
        exposures: list[dict[str, object]] = []
        for index, left in enumerate(descriptors):
            for right in descriptors[index + 1 :]:
                scored = cls._score_pair(left, right, common_terms=common_terms)
                if scored is None:
                    continue
                if subject_id is not None and str(subject_id) not in {
                    str(scored["source_entity_id"]),
                    str(scored["target_entity_id"]),
                }:
                    continue
                exposures.append(scored)
        exposures.sort(
            key=lambda item: (
                str(subject_id)
                in {str(item["source_entity_id"]), str(item["target_entity_id"])},
                float(item.get("confidence") or 0.0),
                float(item.get("combined_weight_pct") or 0.0),
            ),
            reverse=True,
        )
        return exposures[:limit]

    @classmethod
    def _score_pair(
        cls,
        left: dict[str, object],
        right: dict[str, object],
        *,
        common_terms: set[str] | None = None,
    ) -> dict[str, object] | None:
        if str(left.get("entity_id")) == str(right.get("entity_id")):
            return None
        reasons: list[str] = []
        confidence = 0.0
        left_industry = cls._clean(left.get("industry"))
        right_industry = cls._clean(right.get("industry"))
        left_sector = cls._clean(left.get("sector"))
        right_sector = cls._clean(right.get("sector"))
        if left_industry and left_industry == right_industry:
            confidence += 0.36
            reasons.append(f"same industry: {left_industry}")
        elif left_sector and left_sector == right_sector:
            confidence += 0.24
            reasons.append(f"same sector: {left_sector}")

        left_terms = set(left.get("knowledge_terms") or [])
        right_terms = set(right.get("knowledge_terms") or [])
        name_terms = set(left.get("name_terms") or []) | set(
            right.get("name_terms") or []
        )
        shared_terms = [
            term
            for term in (left_terms & right_terms)
            if term not in name_terms
            and term not in cls._generic_terms()
            and (term not in (common_terms or set()) or term in cls._mechanism_terms())
            and not cls._is_noisy_term(term)
        ]
        strong_mechanism_terms = [
            term for term in shared_terms if term in cls._strong_mechanism_terms()
        ]
        metadata_match = bool(
            (left_industry and left_industry == right_industry)
            or (left_sector and left_sector == right_sector)
        )
        if not metadata_match:
            if (
                len(shared_terms) < 3
                or not strong_mechanism_terms
                or set(strong_mechanism_terms) == {"semiconductor"}
            ):
                shared_terms = []
        shared_terms = sorted(
            shared_terms,
            key=lambda term: (
                term not in cls._strong_mechanism_terms(),
                term not in cls._mechanism_terms(),
                term,
            ),
        )
        if shared_terms:
            confidence += min(0.42, 0.12 + 0.06 * len(shared_terms))
            reasons.append(
                "stored knowledge overlaps on " + ", ".join(shared_terms[:6])
            )

        if confidence < 0.24:
            return None
        source = left
        target = right
        if str(right.get("ticker") or "") < str(left.get("ticker") or ""):
            source = right
            target = left
        source_weight = float(source.get("weight_pct") or 0.0)
        target_weight = float(target.get("weight_pct") or 0.0)
        return {
            "source_entity_id": str(source["entity_id"]),
            "source_ticker": source.get("ticker"),
            "source_name": source.get("name"),
            "target_entity_id": str(target["entity_id"]),
            "target_ticker": target.get("ticker"),
            "target_name": target.get("name"),
            "relationship_type": "contextualizes",
            "confidence": round(min(confidence, 0.9), 2),
            "shared_terms": shared_terms[:8],
            "reason": "; ".join(reasons),
            "combined_weight_pct": round(source_weight + target_weight, 2),
        }

    async def _descriptor(
        self, *, position: Position, security: Security, entity: Entity
    ) -> dict[str, object]:
        base_text = " ".join(
            str(item)
            for item in (
                security.ticker,
                entity.name,
                entity.sector,
                entity.industry,
                entity.description,
                " ".join(entity.aliases or []),
            )
            if item
        )
        snippets = await self._knowledge_snippets(security=security, entity=entity)
        return {
            "position_id": str(position.id),
            "entity_id": str(entity.id),
            "ticker": security.ticker,
            "name": entity.name,
            "list_type": position.list_type,
            "market_value": float(position.market_value or 0.0),
            "weight_pct": float(position.weight_pct or 0.0),
            "sector": entity.sector,
            "industry": entity.industry,
            "name_terms": self._meaningful_terms(f"{security.ticker} {entity.name}"),
            "knowledge_terms": self._meaningful_terms(
                f"{base_text} {' '.join(snippets)}"
            ),
        }

    async def _knowledge_snippets(
        self, *, security: Security, entity: Entity, limit: int = 8
    ) -> list[str]:
        raw_search_terms: list[str] = []
        for term in [security.ticker, entity.name, *(entity.aliases or [])]:
            text = str(term or "").strip()
            if not text:
                continue
            raw_search_terms.append(text)
            raw_search_terms.extend(re.findall(r"[A-Za-z0-9][A-Za-z0-9+./-]{2,}", text))
        search_terms: list[str] = []
        seen_terms: set[str] = set()
        for term in raw_search_terms:
            normalized = term.strip(".,;:!?()[]{}")
            normalized_key = normalized.lower()
            if not normalized:
                continue
            if (
                len(normalized_key) <= 2
                and normalized_key not in self._allowed_short_terms()
            ):
                continue
            if normalized_key in self._generic_terms() or self._is_noisy_term(
                normalized_key
            ):
                continue
            if normalized_key in seen_terms:
                continue
            seen_terms.add(normalized_key)
            search_terms.append(normalized)
        if not search_terms:
            return []
        snippets: list[str] = []
        facts = (
            (
                await self.session.execute(
                    select(Fact)
                    .where(
                        Fact.is_deprecated.is_(False),
                        or_(
                            *[
                                Fact.statement.ilike(f"%{term}%")
                                for term in search_terms
                            ]
                        ),
                    )
                    .order_by(desc(Fact.updated_at))
                    .limit(limit * 4)
                )
            )
            .scalars()
            .all()
        )
        claims = (
            (
                await self.session.execute(
                    select(Claim)
                    .where(
                        Claim.is_deprecated.is_(False),
                        or_(
                            *[
                                Claim.statement.ilike(f"%{term}%")
                                for term in search_terms
                            ]
                        ),
                    )
                    .order_by(desc(Claim.updated_at))
                    .limit(limit * 4)
                )
            )
            .scalars()
            .all()
        )
        events = (
            (
                await self.session.execute(
                    select(Event)
                    .where(
                        Event.is_deprecated.is_(False),
                        or_(
                            *[Event.title.ilike(f"%{term}%") for term in search_terms],
                            *[
                                Event.description.ilike(f"%{term}%")
                                for term in search_terms
                            ],
                        ),
                    )
                    .order_by(desc(Event.updated_at))
                    .limit(limit * 4)
                )
            )
            .scalars()
            .all()
        )
        snippets.extend(
            fact.statement
            for fact in facts
            if self._matches_search_terms(fact.statement, search_terms)
        )
        snippets.extend(
            claim.statement
            for claim in claims
            if self._matches_search_terms(claim.statement, search_terms)
        )
        snippets.extend(
            f"{event.title} {event.description or ''}"
            for event in events
            if self._matches_search_terms(
                f"{event.title} {event.description or ''}", search_terms
            )
        )
        return snippets[: limit * 3]

    async def _has_relation(self, source_id: UUID, target_id: UUID) -> bool:
        existing = (
            await self.session.execute(
                select(Edge.id)
                .where(
                    or_(
                        (Edge.source_type == "entity")
                        & (Edge.source_id == source_id)
                        & (Edge.target_type == "entity")
                        & (Edge.target_id == target_id),
                        (Edge.source_type == "entity")
                        & (Edge.source_id == target_id)
                        & (Edge.target_type == "entity")
                        & (Edge.target_id == source_id),
                    )
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        return existing is not None

    @staticmethod
    def _clean(value: object) -> str:
        return " ".join(str(value or "").lower().split())

    @classmethod
    def _meaningful_terms(cls, text: str | None) -> list[str]:
        if not text:
            return []
        terms: list[str] = []
        for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9+./-]{2,}", text.lower()):
            token = token.strip(".,;:!?()[]{}")
            if token in cls._generic_terms():
                continue
            if cls._is_noisy_term(token):
                continue
            if token.endswith("s") and len(token) > 5:
                token = token[:-1]
            if token in cls._generic_terms():
                continue
            terms.append(token)
        seen: set[str] = set()
        deduped: list[str] = []
        for term in terms:
            if term in seen:
                continue
            seen.add(term)
            deduped.append(term)
        return deduped

    @staticmethod
    def _common_terms(descriptors: list[dict[str, object]]) -> set[str]:
        if len(descriptors) < 4:
            return set()
        counts: dict[str, int] = {}
        for descriptor in descriptors:
            for term in set(descriptor.get("knowledge_terms") or []):
                counts[term] = counts.get(term, 0) + 1
        threshold = max(3, len(descriptors) // 4)
        return {term for term, count in counts.items() if count > threshold}

    @staticmethod
    def _is_noisy_term(term: str) -> bool:
        if not term:
            return True
        if term in PortfolioPeerContextService._allowed_short_terms():
            return False
        if len(term) < 4:
            return True
        if re.fullmatch(r"20\d{2}", term):
            return True
        if re.fullmatch(r"\d+(?:\.\d+)?%?", term):
            return True
        if "." in term and term not in PortfolioPeerContextService._mechanism_terms():
            return True
        if term.endswith("."):
            return True
        return False

    @staticmethod
    def _matches_search_terms(text: str | None, search_terms: list[str]) -> bool:
        haystack = text or ""
        for term in search_terms:
            if re.search(
                rf"(?<![A-Za-z0-9]){re.escape(term)}(?![A-Za-z0-9])",
                haystack,
                re.IGNORECASE,
            ):
                return True
        return False

    @staticmethod
    def _allowed_short_terms() -> set[str]:
        return {
            "ai",
            "asic",
            "cpu",
            "ddr",
            "gpu",
            "hbm",
            "hdd",
            "nor",
            "ram",
            "ssd",
        }

    @staticmethod
    def _mechanism_terms() -> set[str]:
        return {
            "accelerator",
            "accelerators",
            "asic",
            "bandwidth",
            "capex",
            "cloud",
            "compute",
            "cycle",
            "cyclical",
            "datacenter",
            "data-center",
            "ddr",
            "dram",
            "fab",
            "flash",
            "foundry",
            "gpu",
            "hbm",
            "hdd",
            "hyperscale",
            "inference",
            "interconnect",
            "inventory",
            "latency",
            "memory",
            "nand",
            "networking",
            "nor",
            "optical",
            "packaging",
            "pricing",
            "ram",
            "semiconductor",
            "ssd",
            "storage",
            "supply",
            "training",
            "utilization",
            "wafer",
        }

    @staticmethod
    def _strong_mechanism_terms() -> set[str]:
        return {
            "bandwidth",
            "capex",
            "ddr",
            "dram",
            "fab",
            "flash",
            "foundry",
            "gpu",
            "hbm",
            "hdd",
            "interconnect",
            "inventory",
            "latency",
            "memory",
            "nand",
            "optical",
            "packaging",
            "pricing",
            "semiconductor",
            "ssd",
            "storage",
            "supply",
            "utilization",
            "wafer",
        }

    @staticmethod
    def _generic_terms() -> set[str]:
        return {
            "12-month",
            "about",
            "acceleration",
            "according",
            "acros",
            "advanced",
            "advantage",
            "agreement",
            "additional",
            "after",
            "active",
            "also",
            "amid",
            "analysis",
            "analyst",
            "announced",
            "and",
            "any",
            "april",
            "approximately",
            "are",
            "areas",
            "around",
            "artificial",
            "assign",
            "available",
            "because",
            "behavior",
            "been",
            "before",
            "benefit",
            "bullish",
            "busines",
            "business",
            "but",
            "california",
            "center",
            "change",
            "classified",
            "company",
            "comprehensive",
            "concrete",
            "consumer",
            "corp",
            "critical",
            "context",
            "contribution",
            "corporation",
            "current",
            "customer",
            "date",
            "date.",
            "develop",
            "develops/manufacture",
            "device",
            "does",
            "driver",
            "during",
            "economic",
            "edge",
            "effect",
            "electronic",
            "emerging",
            "expansion",
            "even",
            "evidence",
            "demonstrating",
            "expectation",
            "expected",
            "exploration",
            "february",
            "financial",
            "following",
            "frequent",
            "future",
            "fy26",
            "gain",
            "from",
            "global",
            "good",
            "growth",
            "have",
            "headquartered",
            "holding",
            "impact",
            "inc",
            "like",
            "increased",
            "included",
            "infrastructure",
            "investor",
            "itself",
            "its",
            "likely",
            "late",
            "leveraged",
            "market",
            "material",
            "majority",
            "manufacture",
            "milpita",
            "more",
            "near",
            "non-diversified",
            "nvidia",
            "operating",
            "outperforming",
            "over",
            "performer",
            "portfolio",
            "positioned",
            "potential",
            "price",
            "prior",
            "provide",
            "product",
            "provider",
            "premium",
            "quarter",
            "recent",
            "research",
            "representing",
            "result",
            "revenue",
            "risk",
            "scale",
            "significantly",
            "solution",
            "source",
            "specializing",
            "space",
            "stock",
            "stored",
            "subject",
            "support",
            "sustained",
            "target",
            "technology",
            "technologie",
            "technologies",
            "that",
            "the",
            "than",
            "this",
            "tracking",
            "trajectory",
            "trading",
            "through",
            "under",
            "upside",
            "upward",
            "volume",
            "weeks",
            "will",
            "with",
        }
