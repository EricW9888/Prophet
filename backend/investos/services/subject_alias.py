from __future__ import annotations

import re
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from investos.models.entity import Entity, Security
from investos.models.portfolio import Position
from investos.models.subject_alias import SubjectAlias
from investos.models.theme import Theme
from investos.schemas.graph import (
    SubjectAliasCreate,
    SubjectAliasResponse,
    SubjectAliasSubjectOption,
    SubjectAliasUpdate,
)
from investos.services.knowledge_audit import KnowledgeAuditService

ALIAS_WORD_RE = re.compile(r"[a-z0-9]+")
SHORT_SYMBOL_QUERY_RE = re.compile(r"^[A-Za-z]{1,5}(?:\.[A-Za-z]{1,4})?$")


def normalize_alias(value: str | None) -> str:
    return " ".join(ALIAS_WORD_RE.findall((value or "").casefold()))


@dataclass(frozen=True)
class SubjectAliasCandidate:
    subject_id: UUID
    subject_type: str
    subject_name: str
    score: int
    reason: str


class SubjectAliasService:
    """Resolve user terms through stored subject aliases/synonyms."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def upsert_alias(
        self,
        *,
        alias: str,
        subject_type: str,
        subject_id: UUID,
        source: str = "system",
        confidence: float = 0.8,
        reason: str | None = None,
    ) -> None:
        normalized = normalize_alias(alias)
        if not normalized:
            return
        stmt = insert(SubjectAlias).values(
            alias=alias.strip(),
            normalized_alias=normalized,
            subject_type=subject_type,
            subject_id=subject_id,
            source=source,
            confidence=confidence,
            reason=reason,
        )
        stmt = stmt.on_conflict_do_update(
            constraint="uq_subject_alias_subject",
            set_={
                "alias": alias.strip(),
                "source": source,
                "confidence": confidence,
                "reason": reason,
            },
        )
        await self.session.execute(stmt)

    async def candidates_for_message(
        self, message: str, *, limit: int = 8
    ) -> list[SubjectAliasCandidate]:
        normalized_message = normalize_alias(message)
        if not normalized_message:
            return []
        message_tokens = set(normalized_message.split())
        aliases = (
            (
                await self.session.execute(
                    select(SubjectAlias)
                    .order_by(
                        SubjectAlias.confidence.desc(), SubjectAlias.updated_at.desc()
                    )
                    .limit(2000)
                )
            )
            .scalars()
            .all()
        )
        matches: list[tuple[SubjectAlias, int]] = []
        for alias in aliases:
            alias_key = alias.normalized_alias
            if not alias_key:
                continue
            alias_tokens = alias_key.split()
            exact_token = len(alias_tokens) == 1 and alias_key in message_tokens
            phrase_match = (
                len(alias_tokens) > 1 and f" {alias_key} " in f" {normalized_message} "
            )
            if not exact_token and not phrase_match:
                continue
            score = int(50 + min(40, (alias.confidence or 0.0) * 40))
            if exact_token and len(alias_key) <= 4:
                score += 10
            if phrase_match:
                score += 15
            matches.append((alias, score))

        candidates: list[SubjectAliasCandidate] = []
        seen: set[tuple[str, UUID]] = set()
        for alias, score in sorted(matches, key=lambda item: item[1], reverse=True):
            key = (alias.subject_type, alias.subject_id)
            if key in seen:
                continue
            label = await self._subject_label(alias.subject_type, alias.subject_id)
            if label is None:
                continue
            seen.add(key)
            candidates.append(
                SubjectAliasCandidate(
                    subject_id=alias.subject_id,
                    subject_type=alias.subject_type,
                    subject_name=label,
                    score=score,
                    reason=(
                        f"Matched stored alias '{alias.alias}' for {label}."
                        + (f" {alias.reason}" if alias.reason else "")
                    ),
                )
            )
            if len(candidates) >= limit:
                break
        return candidates

    async def list_aliases(self, *, limit: int = 100) -> list[SubjectAliasResponse]:
        aliases = (
            (
                await self.session.execute(
                    select(SubjectAlias)
                    .order_by(
                        SubjectAlias.updated_at.desc(),
                        SubjectAlias.confidence.desc(),
                        SubjectAlias.alias.asc(),
                    )
                    .limit(max(1, min(limit, 500)))
                )
            )
            .scalars()
            .all()
        )
        responses: list[SubjectAliasResponse] = []
        for alias in aliases:
            response = await self._response_for_alias(alias)
            if response is not None:
                responses.append(response)
        return responses

    async def list_subject_options(
        self,
        *,
        query: str | None = None,
        limit: int = 100,
    ) -> list[SubjectAliasSubjectOption]:
        capped_limit = max(1, min(limit, 250))
        search = (query or "").strip()
        normalized_search = normalize_alias(search)
        short_symbol_search = bool(search and SHORT_SYMBOL_QUERY_RE.fullmatch(search))
        alias_subjects: set[tuple[str, UUID]] = set()
        alias_options: list[SubjectAliasSubjectOption] = []
        if normalized_search:
            aliases = (
                (
                    await self.session.execute(
                        select(SubjectAlias)
                        .where(SubjectAlias.normalized_alias == normalized_search)
                        .order_by(
                            SubjectAlias.confidence.desc(),
                            SubjectAlias.updated_at.desc(),
                        )
                        .limit(capped_limit)
                    )
                )
                .scalars()
                .all()
            )
            for alias in aliases:
                label = await self._subject_label(alias.subject_type, alias.subject_id)
                if label is None:
                    continue
                key = (alias.subject_type, alias.subject_id)
                alias_subjects.add(key)
                alias_options.append(
                    SubjectAliasSubjectOption(
                        subject_type=alias.subject_type,
                        subject_id=alias.subject_id,
                        subject_name=label,
                        subtitle=f"alias: {alias.alias}",
                        linked_symbols=await self._linked_symbols(
                            alias.subject_type, alias.subject_id
                        ),
                        is_active_holding=False,
                    )
                )

        theme_options: list[SubjectAliasSubjectOption] = []
        theme_stmt = (
            select(Theme).order_by(Theme.last_updated_at.desc()).limit(capped_limit)
        )
        if search:
            theme_filter = (
                or_(
                    Theme.name.ilike(search),
                    Theme.name.ilike(f"{search} ·%"),
                )
                if short_symbol_search
                else Theme.name.ilike(f"%{search}%")
            )
            theme_stmt = (
                select(Theme)
                .where(theme_filter)
                .order_by(Theme.last_updated_at.desc())
                .limit(capped_limit)
            )
        themes = (await self.session.execute(theme_stmt)).scalars().all()
        for theme in themes:
            theme_options.append(
                SubjectAliasSubjectOption(
                    subject_type="theme",
                    subject_id=theme.id,
                    subject_name=theme.name,
                    subtitle="theme",
                    linked_symbols=await self._linked_symbols("theme", theme.id),
                    is_active_holding=False,
                )
            )

        entity_stmt = (
            select(Entity, Security, Position)
            .join(Security, Security.entity_id == Entity.id, isouter=True)
            .join(Position, Position.security_id == Security.id, isouter=True)
            .order_by(
                (Position.list_type == "holding").desc(),
                Security.ticker.asc(),
                Entity.name.asc(),
            )
            .limit(capped_limit * 3)
        )
        if search:
            entity_filter = (
                or_(
                    Security.ticker.ilike(search),
                    Entity.name.ilike(search),
                    Entity.name.ilike(f"{search} ·%"),
                )
                if short_symbol_search
                else or_(
                    Entity.name.ilike(f"%{search}%"),
                    Security.ticker.ilike(f"%{search}%"),
                    Entity.industry.ilike(f"%{search}%"),
                    Entity.sector.ilike(f"%{search}%"),
                )
            )
            entity_stmt = (
                select(Entity, Security, Position)
                .join(Security, Security.entity_id == Entity.id, isouter=True)
                .join(Position, Position.security_id == Security.id, isouter=True)
                .where(entity_filter)
                .order_by(
                    (Position.list_type == "holding").desc(),
                    Security.ticker.asc(),
                    Entity.name.asc(),
                )
                .limit(capped_limit * 3)
            )
        entity_rows = (await self.session.execute(entity_stmt)).all()
        by_entity: dict[UUID, dict[str, object]] = {}
        for entity, security, position in entity_rows:
            bucket = by_entity.setdefault(
                entity.id,
                {
                    "entity": entity,
                    "symbols": set(),
                    "is_active_holding": False,
                },
            )
            if security is not None and security.ticker:
                bucket["symbols"].add(security.ticker)
            if position is not None and position.list_type == "holding":
                bucket["is_active_holding"] = True
        entity_options: list[SubjectAliasSubjectOption] = []
        for bucket in by_entity.values():
            entity = bucket["entity"]
            symbols = sorted(bucket["symbols"])
            ticker_prefix = f"{symbols[0]} · " if symbols else ""
            entity_options.append(
                SubjectAliasSubjectOption(
                    subject_type="entity",
                    subject_id=entity.id,
                    subject_name=f"{ticker_prefix}{entity.name}",
                    subtitle=entity.industry or entity.sector or entity.entity_type,
                    linked_symbols=symbols,
                    is_active_holding=bool(bucket["is_active_holding"]),
                )
            )
        options = [*entity_options, *theme_options, *alias_options]
        search_key = search.casefold()
        normalized_tokens = set(normalize_alias(search).split())

        def option_rank(
            option: SubjectAliasSubjectOption,
        ) -> tuple[bool, bool, bool, bool, bool, str]:
            exact_symbol = bool(search_key) and any(
                symbol.casefold() == search_key for symbol in option.linked_symbols
            )
            alias_match = (option.subject_type, option.subject_id) in alias_subjects
            if short_symbol_search:
                option_tokens = set(normalize_alias(option.subject_name).split())
                name_match = bool(normalized_tokens) and bool(
                    normalized_tokens & option_tokens
                )
            else:
                name_match = (
                    bool(search_key) and search_key in option.subject_name.casefold()
                )
            return (
                not option.is_active_holding,
                not exact_symbol,
                not alias_match,
                option.subject_type != "entity",
                not name_match,
                option.subject_name.lower(),
            )

        sorted_options = sorted(options, key=option_rank)
        deduped: list[SubjectAliasSubjectOption] = []
        seen: set[tuple[str, UUID]] = set()
        for option in sorted_options:
            key = (option.subject_type, option.subject_id)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(option)
            if len(deduped) >= capped_limit:
                break
        return deduped

    async def create_alias(self, payload: SubjectAliasCreate) -> SubjectAliasResponse:
        if not normalize_alias(payload.alias):
            raise ValueError("alias_required")
        label = await self._subject_label(payload.subject_type, payload.subject_id)
        if label is None:
            raise ValueError("subject_not_found")
        await self.upsert_alias(
            alias=payload.alias,
            subject_type=payload.subject_type,
            subject_id=payload.subject_id,
            source="user_created",
            confidence=0.98,
            reason=payload.reason or "Created by user review.",
        )
        await self.session.commit()
        alias = await self._find_alias(
            normalized_alias=normalize_alias(payload.alias),
            subject_type=payload.subject_type,
            subject_id=payload.subject_id,
        )
        if alias is None:
            raise ValueError("alias_not_created")
        response = await self._response_for_alias(alias)
        if response is None:
            raise ValueError("subject_not_found")
        return response

    async def update_alias(
        self, alias_id: UUID, payload: SubjectAliasUpdate
    ) -> SubjectAliasResponse | None:
        alias = await self.session.get(SubjectAlias, alias_id)
        if alias is None:
            return None
        target_alias = (
            payload.alias.strip() if payload.alias is not None else alias.alias
        )
        normalized = normalize_alias(target_alias)
        if not normalized:
            raise ValueError("alias_required")
        target_type = payload.subject_type or alias.subject_type
        target_id = payload.subject_id or alias.subject_id
        label = await self._subject_label(target_type, target_id)
        if label is None:
            raise ValueError("subject_not_found")
        confidence = (
            payload.confidence
            if payload.confidence is not None
            else max(float(alias.confidence or 0), 0.98)
        )
        confidence = max(0.0, min(float(confidence), 1.0))
        existing = await self._find_alias(
            normalized_alias=normalized,
            subject_type=target_type,
            subject_id=target_id,
            exclude_id=alias.id,
        )
        if existing is not None:
            existing.alias = target_alias
            existing.source = "user_updated"
            existing.confidence = confidence
            existing.reason = (
                payload.reason if payload.reason is not None else alias.reason
            )
            await KnowledgeAuditService(self.session).record_change(
                node_type="subject_alias",
                node_id=alias.id,
                change_type="merged_duplicate_alias",
                reason="User alias update matched an existing canonical alias.",
                actor="user",
                source_type="subject_alias",
                source_id=existing.id,
                subject_type=target_type,
                subject_id=target_id,
                metadata={
                    "old_alias": alias.alias,
                    "canonical_alias": existing.alias,
                    "canonical_alias_id": str(existing.id),
                },
            )
            await self.session.delete(alias)
            await self.session.commit()
            await self.session.refresh(existing)
            return await self._response_for_alias(existing)
        alias.alias = target_alias
        alias.normalized_alias = normalized
        alias.subject_type = target_type
        alias.subject_id = target_id
        alias.source = "user_updated"
        alias.confidence = confidence
        if payload.reason is not None:
            alias.reason = payload.reason or None
        await self.session.commit()
        await self.session.refresh(alias)
        return await self._response_for_alias(alias)

    async def approve_alias(self, alias_id: UUID) -> SubjectAliasResponse | None:
        alias = await self.session.get(SubjectAlias, alias_id)
        if alias is None:
            return None
        alias.source = "user_approved"
        alias.confidence = max(float(alias.confidence or 0), 0.98)
        if not alias.reason:
            alias.reason = "Approved by user review."
        await self.session.commit()
        await self.session.refresh(alias)
        return await self._response_for_alias(alias)

    async def delete_alias(self, alias_id: UUID) -> bool:
        alias = await self.session.get(SubjectAlias, alias_id)
        if alias is None:
            return False
        await KnowledgeAuditService(self.session).record_change(
            node_type="subject_alias",
            node_id=alias.id,
            change_type="deleted_alias",
            reason="User deleted a stored subject alias.",
            actor="user",
            subject_type=alias.subject_type,
            subject_id=alias.subject_id,
            metadata={"alias": alias.alias, "normalized_alias": alias.normalized_alias},
        )
        await self.session.delete(alias)
        await self.session.commit()
        return True

    async def _find_alias(
        self,
        *,
        normalized_alias: str,
        subject_type: str,
        subject_id: UUID,
        exclude_id: UUID | None = None,
    ) -> SubjectAlias | None:
        stmt = select(SubjectAlias).where(
            SubjectAlias.normalized_alias == normalized_alias,
            SubjectAlias.subject_type == subject_type,
            SubjectAlias.subject_id == subject_id,
        )
        if exclude_id is not None:
            stmt = stmt.where(SubjectAlias.id != exclude_id)
        return (await self.session.execute(stmt.limit(1))).scalar_one_or_none()

    async def _response_for_alias(
        self, alias: SubjectAlias
    ) -> SubjectAliasResponse | None:
        label = await self._subject_label(alias.subject_type, alias.subject_id)
        if label is None:
            return None
        return SubjectAliasResponse(
            id=alias.id,
            alias=alias.alias,
            normalized_alias=alias.normalized_alias,
            subject_type=alias.subject_type,
            subject_id=alias.subject_id,
            subject_name=label,
            source=alias.source,
            confidence=alias.confidence,
            reason=alias.reason,
            linked_symbols=await self._linked_symbols(
                alias.subject_type, alias.subject_id
            ),
            created_at=alias.created_at,
            updated_at=alias.updated_at,
        )

    async def _subject_label(self, subject_type: str, subject_id: UUID) -> str | None:
        if subject_type == "theme":
            theme = await self.session.get(Theme, subject_id)
            return None if theme is None else theme.name
        if subject_type == "entity":
            row = (
                await self.session.execute(
                    select(Entity, Security)
                    .join(Security, Security.entity_id == Entity.id, isouter=True)
                    .where(Entity.id == subject_id)
                    .order_by(Security.ticker.asc())
                    .limit(1)
                )
            ).first()
            if row is None:
                return None
            entity, security = row
            if security is not None and security.ticker:
                return f"{security.ticker} · {entity.name}"
            return entity.name
        if subject_type == "portfolio":
            return "Portfolio"
        return None

    async def _linked_symbols(self, subject_type: str, subject_id: UUID) -> list[str]:
        if subject_type == "theme":
            theme = await self.session.get(Theme, subject_id)
            if theme is None or not theme.tagged_security_ids:
                return []
            rows = (
                (
                    await self.session.execute(
                        select(Security.ticker)
                        .join(
                            Position, Position.security_id == Security.id, isouter=True
                        )
                        .where(Security.id.in_(theme.tagged_security_ids))
                        .order_by(
                            (Position.list_type == "holding").desc(),
                            Security.ticker.asc(),
                        )
                    )
                )
                .scalars()
                .all()
            )
            return sorted({ticker for ticker in rows if ticker})
        if subject_type == "entity":
            rows = (
                (
                    await self.session.execute(
                        select(Security.ticker)
                        .where(Security.entity_id == subject_id)
                        .order_by(Security.ticker.asc())
                    )
                )
                .scalars()
                .all()
            )
            return sorted({ticker for ticker in rows if ticker})
        return []
