from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime

from sqlalchemy import delete, func, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from investos.config import settings
from investos.models.conclusion import ConclusionRevision, ConclusionState
from investos.models.coverage import (
    CoverageMap,
    MissingEvidenceClass,
    Resolution,
    UnresolvedQuestion,
)
from investos.models.decision import DecisionJournal
from investos.models.entity import Entity, Security
from investos.models.fundamental import FundamentalMetric
from investos.models.graph import Edge, GraphNodeLayout, GraphTraversalSet
from investos.models.market_setup import MarketSetupSignal
from investos.models.portfolio import Position
from investos.models.profile import Profile
from investos.models.subject_alias import SubjectAlias
from investos.models.theme import Theme
from investos.models.thesis import Thesis
from investos.models.verification import VerificationRun
from investos.models.watcher import ActiveWatcher
from investos.services.artifact_hygiene import (
    ARTIFACT_PREFIX_RE,
    MONEY_OR_POSITION_FRAGMENT_RE,
    QUESTION_SUBJECT_RE,
    compact_key,
    is_artifact_subject_name,
    is_placeholder_profile_text,
    is_topic_subject_name,
    is_unusable_subject,
    label_from_profile_texts,
    normalize_subject_name,
)
from investos.services.knowledge_audit import KnowledgeAuditService


class EntityHygieneService:
    """Transparent, auditable cleanup of junk entity nodes.

    The extractor creates a Profile + CoverageMap (+ edges) for every subject,
    so junk entities are never truly orphaned — they drag a tail of
    auto-generated satellites. This service removes a junk entity together with
    those satellites, but only when the entity has **no real portfolio
    linkage** (no security, watcher, or thesis). Anything portfolio-linked is
    reported, never deleted.

    Two tiers of junk:
      * ``artifact`` — internal run labels (``Auto research:``, ``Autonomous
        reflection:``, ``Research on:`` …). Unambiguous; cleaned by default.
      * ``unusable`` — numbers, money phrases, questions, lowercase fragments.
        Borderline; only cleaned in ``aggressive`` mode, otherwise flagged.

    Every run writes a JSON audit with the full record of each deleted node and
    its satellite counts, so a deletion is recoverable and never "mysterious".
    """

    def __init__(self, session: AsyncSession):
        self.session = session

    _PROFILE_TEXT_FIELDS = (
        "executive_summary",
        "business_model",
        "bull_case",
        "bear_case",
        "key_drivers",
        "competitor_landscape",
        "strategist_reasoning",
        "source_rationale",
    )

    @staticmethod
    def _classify(name: str | None) -> str | None:
        if (
            ARTIFACT_PREFIX_RE.match(name or "")
            or is_artifact_subject_name(name)
            or EntityHygieneService._is_structural_artifact(name)
        ):
            return "artifact"
        if is_unusable_subject(name):
            return "unusable"
        return None

    @staticmethod
    def _is_structural_artifact(name: str | None) -> bool:
        """Entity-layer labels that are structurally not durable subjects."""
        s = (name or "").strip()
        if not s:
            return False
        if "?" in s or QUESTION_SUBJECT_RE.match(s):
            return True
        if MONEY_OR_POSITION_FRAGMENT_RE.search(s):
            return True
        if re.match(r"^\d+(?:\.\d+)?%\s+", s):
            return True
        if re.match(r"^\d+(?:\s+|[/-])", s):
            return True
        if "/" in s and s.casefold() == s:
            return True
        if len(s) <= 3 and s.casefold() == s and s.isalpha():
            return True
        return False

    @staticmethod
    def _is_product_like_label(name: str | None) -> bool:
        tokens = re.findall(r"[A-Za-z][A-Za-z0-9-]{2,}", name or "")
        return any(
            token[0].islower() and any(ch.isupper() for ch in token[1:])
            for token in tokens
        )

    @classmethod
    def _profile_values(cls, profiles: list[Profile]) -> list[str]:
        return [
            getattr(profile, field, None) or ""
            for profile in profiles
            for field in cls._PROFILE_TEXT_FIELDS
        ]

    @classmethod
    def _profiles_have_substantive_text(cls, profiles: list[Profile]) -> bool:
        return any(
            value.strip() and not is_placeholder_profile_text(value)
            for value in cls._profile_values(profiles)
        )

    @classmethod
    def _theme_name_from_profiles(
        cls, name: str | None, profiles: list[Profile]
    ) -> str:
        label = label_from_profile_texts(cls._profile_values(profiles))
        return label or normalize_subject_name(name)

    @classmethod
    def _should_reclassify_unusable_entity(
        cls, name: str | None, profiles: list[Profile]
    ) -> bool:
        s = (name or "").strip()
        if not s or cls._is_product_like_label(s):
            return False
        if not cls._profiles_have_substantive_text(profiles):
            return False
        key = compact_key(s)
        if is_topic_subject_name(s):
            return True
        if len(s) > 80:
            return True
        if "/" in s:
            return True
        if key in {"industry", "implementation"}:
            return True
        tokens = re.findall(r"[A-Za-z][A-Za-z0-9-]{2,}", s)
        return bool(tokens and s.casefold() == s)

    @classmethod
    def _placeholder_only_entity(cls, profiles: list[Profile]) -> bool:
        values = [value for value in cls._profile_values(profiles) if value.strip()]
        return bool(values) and all(
            is_placeholder_profile_text(value) for value in values
        )

    @staticmethod
    def _is_migratable_topic_label(name: str | None) -> bool:
        return is_topic_subject_name(name)

    _ENTITY_NAME_STOPWORDS = {
        "adr",
        "class",
        "co",
        "common",
        "company",
        "corp",
        "corporation",
        "inc",
        "incorporated",
        "ltd",
        "ordinary",
        "plc",
        "share",
        "shares",
        "stock",
        "the",
        "us",
    }

    @staticmethod
    def _explicit_ticker_mentions(
        name: str | None, known_tickers: set[str]
    ) -> set[str]:
        """Return explicit uppercase ticker mentions; titlecase product names do not count."""
        tickers = {ticker.upper() for ticker in known_tickers if ticker}
        mentions: set[str] = set()
        for token in re.findall(r"\b[A-Z]{1,8}(?:\.[A-Z]{1,4})?\b", name or ""):
            root = token.split(".", 1)[0]
            if root in tickers:
                mentions.add(root)
        return mentions

    @classmethod
    def _entity_name_key(
        cls, name: str | None, *, known_tickers: set[str] | None = None
    ) -> str:
        known = {ticker.casefold() for ticker in known_tickers or set()}
        without_parentheticals = re.sub(r"\([^)]*\)", " ", name or "")
        words = re.findall(r"[a-z0-9]+", without_parentheticals.casefold())
        useful = [
            word
            for word in words
            if word
            and word not in known
            and word not in cls._ENTITY_NAME_STOPWORDS
            and not word.isdigit()
        ]
        return " ".join(useful)

    @classmethod
    def _duplicate_entity_target_name(
        cls,
        candidate_name: str | None,
        canonical_entries: list[dict],
    ) -> dict | None:
        """Choose a canonical security-backed entity only when the match is conservative."""
        known_tickers = {
            str(entry.get("ticker") or "").upper()
            for entry in canonical_entries
            if entry.get("ticker")
        }
        explicit_tickers = cls._explicit_ticker_mentions(candidate_name, known_tickers)
        if len(explicit_tickers) > 1:
            return None

        candidate_key = cls._entity_name_key(
            candidate_name, known_tickers=known_tickers
        )
        scored: list[tuple[int, int, str, dict]] = []
        for entry in canonical_entries:
            ticker = str(entry.get("ticker") or "").upper()
            canonical_key = cls._entity_name_key(
                str(entry.get("name") or ""), known_tickers=known_tickers
            )
            if not canonical_key:
                continue
            score = 0
            reason = ""
            if explicit_tickers and ticker in explicit_tickers:
                score = 100
                reason = f"explicit ticker {ticker}"
            elif candidate_key and candidate_key == canonical_key:
                score = 90
                reason = "normalized name match"
            if score < 90:
                continue
            holding_rank = 1 if entry.get("is_active_holding") else 0
            scored.append((score, holding_rank, reason, entry))

        if not scored:
            return None
        scored.sort(
            key=lambda item: (item[0], item[1], str(item[3].get("name") or "")),
            reverse=True,
        )
        best = scored[0]
        if len(scored) > 1 and scored[1][0] == best[0] and scored[1][1] == best[1]:
            return None
        result = dict(best[3])
        result["match_reason"] = best[2]
        result["match_score"] = best[0]
        return result

    async def _count(self, stmt) -> int:
        return int((await self.session.execute(stmt)).scalar() or 0)

    async def _portfolio_linkage(self, entity_id) -> dict[str, int]:
        """References that make an entity 'real' — must never be auto-deleted."""
        return {
            "securities": await self._count(
                select(func.count())
                .select_from(Security)
                .where(Security.entity_id == entity_id)
            ),
            "watchers": await self._count(
                select(func.count())
                .select_from(ActiveWatcher)
                .where(ActiveWatcher.entity_id == entity_id)
            ),
            "theses": await self._count(
                select(func.count())
                .select_from(Thesis)
                .where(Thesis.entity_id == entity_id)
            ),
        }

    async def _satellite_counts(self, entity_id) -> dict[str, int]:
        return {
            "profiles": await self._count(
                select(func.count())
                .select_from(Profile)
                .where(
                    Profile.subject_type == "entity", Profile.subject_id == entity_id
                )
            ),
            "coverage": await self._count(
                select(func.count())
                .select_from(CoverageMap)
                .where(
                    CoverageMap.subject_type == "entity",
                    CoverageMap.subject_id == entity_id,
                )
            ),
            "conclusions": await self._count(
                select(func.count())
                .select_from(ConclusionState)
                .where(
                    ConclusionState.subject_type == "entity",
                    ConclusionState.subject_id == entity_id,
                )
            ),
            "fundamental_metrics": await self._count(
                select(func.count())
                .select_from(FundamentalMetric)
                .where(
                    or_(
                        FundamentalMetric.entity_id == entity_id,
                        (FundamentalMetric.subject_type == "entity")
                        & (FundamentalMetric.subject_id == entity_id),
                    )
                )
            ),
            "market_setup_signals": await self._count(
                select(func.count())
                .select_from(MarketSetupSignal)
                .where(
                    or_(
                        MarketSetupSignal.entity_id == entity_id,
                        (MarketSetupSignal.subject_type == "entity")
                        & (MarketSetupSignal.subject_id == entity_id),
                    )
                )
            ),
            "edges": await self._count(
                select(func.count())
                .select_from(Edge)
                .where(
                    or_(
                        (Edge.source_type == "entity") & (Edge.source_id == entity_id),
                        (Edge.target_type == "entity") & (Edge.target_id == entity_id),
                    )
                )
            ),
        }

    async def _retarget_investment_objects_to_entity(
        self, source_entity_id, target_entity_id
    ) -> dict[str, int]:
        moved: dict[str, int] = {}
        for label, model in (
            ("fundamental_metrics", FundamentalMetric),
            ("market_setup_signals", MarketSetupSignal),
        ):
            subject_result = await self.session.execute(
                update(model)
                .where(
                    model.subject_type == "entity",
                    model.subject_id == source_entity_id,
                )
                .values(subject_id=target_entity_id, entity_id=target_entity_id)
            )
            entity_result = await self.session.execute(
                update(model)
                .where(model.entity_id == source_entity_id)
                .values(entity_id=target_entity_id)
            )
            moved[label] = int(subject_result.rowcount or 0) + int(
                entity_result.rowcount or 0
            )
        return moved

    async def _retarget_investment_objects_to_theme(
        self, entity_id, theme_id
    ) -> dict[str, int]:
        moved: dict[str, int] = {}
        for label, model in (
            ("fundamental_metrics", FundamentalMetric),
            ("market_setup_signals", MarketSetupSignal),
        ):
            subject_result = await self.session.execute(
                update(model)
                .where(
                    model.subject_type == "entity",
                    model.subject_id == entity_id,
                )
                .values(
                    subject_type="theme",
                    subject_id=theme_id,
                    entity_id=None,
                    security_id=None,
                    ticker=None,
                )
            )
            entity_result = await self.session.execute(
                update(model).where(model.entity_id == entity_id).values(entity_id=None)
            )
            moved[label] = int(subject_result.rowcount or 0) + int(
                entity_result.rowcount or 0
            )
        return moved

    async def _delete_investment_objects_for_entity(self, entity_id) -> dict[str, int]:
        removed: dict[str, int] = {}
        for label, node_type, model in (
            ("fundamental_metrics", "fundamental_metric", FundamentalMetric),
            ("market_setup_signals", "market_setup_signal", MarketSetupSignal),
        ):
            object_ids = (
                (
                    await self.session.execute(
                        select(model.id).where(
                            or_(
                                model.entity_id == entity_id,
                                (model.subject_type == "entity")
                                & (model.subject_id == entity_id),
                            )
                        )
                    )
                )
                .scalars()
                .all()
            )
            if object_ids:
                await self.session.execute(
                    delete(Edge).where(
                        or_(
                            (Edge.source_type == node_type)
                            & (Edge.source_id.in_(object_ids)),
                            (Edge.target_type == node_type)
                            & (Edge.target_id.in_(object_ids)),
                        )
                    )
                )
                result = await self.session.execute(
                    delete(model).where(model.id.in_(object_ids))
                )
                removed[label] = int(result.rowcount or 0)
            else:
                removed[label] = 0
        return removed

    async def _cascade_delete(self, entity_id) -> None:
        """Delete an entity and its auto-generated satellites in FK order."""
        await self._delete_investment_objects_for_entity(entity_id)

        # Coverage maps -> resolutions -> questions/classes -> maps
        cov_ids = (
            (
                await self.session.execute(
                    select(CoverageMap.id).where(
                        CoverageMap.subject_type == "entity",
                        CoverageMap.subject_id == entity_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        if cov_ids:
            q_ids = (
                (
                    await self.session.execute(
                        select(UnresolvedQuestion.id).where(
                            UnresolvedQuestion.coverage_map_id.in_(cov_ids)
                        )
                    )
                )
                .scalars()
                .all()
            )
            c_ids = (
                (
                    await self.session.execute(
                        select(MissingEvidenceClass.id).where(
                            MissingEvidenceClass.coverage_map_id.in_(cov_ids)
                        )
                    )
                )
                .scalars()
                .all()
            )
            if q_ids or c_ids:
                await self.session.execute(
                    delete(Resolution).where(
                        or_(
                            Resolution.unresolved_question_id.in_(q_ids or [None]),
                            Resolution.missing_evidence_class_id.in_(c_ids or [None]),
                        )
                    )
                )
            await self.session.execute(
                delete(UnresolvedQuestion).where(
                    UnresolvedQuestion.coverage_map_id.in_(cov_ids)
                )
            )
            await self.session.execute(
                delete(MissingEvidenceClass).where(
                    MissingEvidenceClass.coverage_map_id.in_(cov_ids)
                )
            )
            await self.session.execute(
                delete(CoverageMap).where(CoverageMap.id.in_(cov_ids))
            )

        # Profiles (table 'dossiers') -> snapshots/deltas -> profiles
        prof_ids = (
            (
                await self.session.execute(
                    select(Profile.id).where(
                        Profile.subject_type == "entity",
                        Profile.subject_id == entity_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        if prof_ids:
            # ProfileSnapshot/ProfileDelta map the FK to the real column 'dossier_id'.
            await self.session.execute(
                text("DELETE FROM dossier_snapshots WHERE dossier_id = ANY(:ids)"),
                {"ids": prof_ids},
            )
            await self.session.execute(
                text("DELETE FROM dossier_deltas WHERE dossier_id = ANY(:ids)"),
                {"ids": prof_ids},
            )
            await self.session.execute(delete(Profile).where(Profile.id.in_(prof_ids)))

        # Conclusions -> revisions -> states
        conc_ids = (
            (
                await self.session.execute(
                    select(ConclusionState.id).where(
                        ConclusionState.subject_type == "entity",
                        ConclusionState.subject_id == entity_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        if conc_ids:
            await self.session.execute(
                delete(VerificationRun).where(
                    VerificationRun.conclusion_state_id.in_(conc_ids)
                )
            )
            await self.session.execute(
                update(DecisionJournal)
                .where(DecisionJournal.conclusion_state_id.in_(conc_ids))
                .values(conclusion_state_id=None)
            )
            await self.session.execute(
                update(Thesis)
                .where(Thesis.conclusion_state_id.in_(conc_ids))
                .values(conclusion_state_id=None)
            )
            await self.session.execute(
                delete(ConclusionRevision).where(
                    ConclusionRevision.conclusion_state_id.in_(conc_ids)
                )
            )
            await self.session.execute(
                delete(ConclusionState).where(ConclusionState.id.in_(conc_ids))
            )

        # Graph edges touching this entity
        await self.session.execute(
            delete(Edge).where(
                or_(
                    (Edge.source_type == "entity") & (Edge.source_id == entity_id),
                    (Edge.target_type == "entity") & (Edge.target_id == entity_id),
                )
            )
        )

        await self.session.execute(delete(Entity).where(Entity.id == entity_id))

    async def _get_or_create_theme(self, name: str) -> Theme:
        normalized = normalize_subject_name(name)
        existing = (
            await self.session.execute(
                select(Theme)
                .where(func.lower(Theme.name) == normalized.lower())
                .limit(1)
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing
        theme = Theme(
            name=normalized,
            description=None,
            status="active",
        )
        self.session.add(theme)
        await self.session.flush()
        return theme

    async def _merge_profiles_into_theme(self, entity_id, theme_id) -> int:
        old_profiles = (
            (
                await self.session.execute(
                    select(Profile).where(
                        Profile.subject_type == "entity",
                        Profile.subject_id == entity_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        if not old_profiles:
            return 0
        target = (
            await self.session.execute(
                select(Profile)
                .where(Profile.subject_type == "theme", Profile.subject_id == theme_id)
                .limit(1)
            )
        ).scalar_one_or_none()
        moved = 0
        for profile in old_profiles:
            if target is None:
                profile.subject_type = "theme"
                profile.subject_id = theme_id
                target = profile
                moved += 1
                continue
            if profile.id == target.id:
                continue
            for field in (
                "executive_summary",
                "business_model",
                "bull_case",
                "bear_case",
                "key_drivers",
                "competitor_landscape",
                "strategist_reasoning",
                "source_rationale",
            ):
                if not getattr(target, field) and getattr(profile, field):
                    setattr(target, field, getattr(profile, field))
            if profile.active_contradictions:
                target.active_contradictions = sorted(
                    {
                        *((target.active_contradictions or [])),
                        *profile.active_contradictions,
                    }
                )
            target.version = max(target.version or 1, profile.version or 1)
            await self.session.execute(
                text(
                    "UPDATE dossier_snapshots SET dossier_id = :target WHERE dossier_id = :old"
                ),
                {"target": target.id, "old": profile.id},
            )
            await self.session.execute(
                text(
                    "UPDATE dossier_deltas SET dossier_id = :target WHERE dossier_id = :old"
                ),
                {"target": target.id, "old": profile.id},
            )
            await self.session.execute(delete(Profile).where(Profile.id == profile.id))
            moved += 1
        return moved

    async def _merge_coverage_into_theme(self, entity_id, theme_id) -> int:
        old_maps = (
            (
                await self.session.execute(
                    select(CoverageMap).where(
                        CoverageMap.subject_type == "entity",
                        CoverageMap.subject_id == entity_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        if not old_maps:
            return 0
        target = (
            await self.session.execute(
                select(CoverageMap)
                .where(
                    CoverageMap.subject_type == "theme",
                    CoverageMap.subject_id == theme_id,
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        moved = 0
        for coverage in old_maps:
            if target is None:
                coverage.subject_type = "theme"
                coverage.subject_id = theme_id
                target = coverage
                moved += 1
                continue
            if coverage.id == target.id:
                continue
            target.total_evidence_count = max(
                target.total_evidence_count or 0, coverage.total_evidence_count or 0
            )
            target.high_tier_evidence_count = max(
                target.high_tier_evidence_count or 0,
                coverage.high_tier_evidence_count or 0,
            )
            target.contradiction_count = max(
                target.contradiction_count or 0, coverage.contradiction_count or 0
            )
            target.unresolved_contradiction_count = max(
                target.unresolved_contradiction_count or 0,
                coverage.unresolved_contradiction_count or 0,
            )
            target.overall_coverage_score = max(
                float(target.overall_coverage_score or 0.0),
                float(coverage.overall_coverage_score or 0.0),
            )
            target.evidence_class_coverage_json = {
                **(coverage.evidence_class_coverage_json or {}),
                **(target.evidence_class_coverage_json or {}),
            }
            await self.session.execute(
                update(UnresolvedQuestion)
                .where(UnresolvedQuestion.coverage_map_id == coverage.id)
                .values(coverage_map_id=target.id)
            )
            await self.session.execute(
                update(MissingEvidenceClass)
                .where(MissingEvidenceClass.coverage_map_id == coverage.id)
                .values(coverage_map_id=target.id)
            )
            await self.session.execute(
                delete(CoverageMap).where(CoverageMap.id == coverage.id)
            )
            moved += 1
        return moved

    async def _merge_conclusions_into_theme(self, entity_id, theme_id) -> int:
        old_states = (
            (
                await self.session.execute(
                    select(ConclusionState).where(
                        ConclusionState.subject_type == "entity",
                        ConclusionState.subject_id == entity_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        if not old_states:
            return 0
        target = (
            await self.session.execute(
                select(ConclusionState)
                .where(
                    ConclusionState.subject_type == "theme",
                    ConclusionState.subject_id == theme_id,
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        moved = 0
        for state in old_states:
            if target is None:
                state.subject_type = "theme"
                state.subject_id = theme_id
                target = state
                moved += 1
                continue
            if state.id == target.id:
                continue
            if target.current_stance == "no_view" and state.current_stance != "no_view":
                target.current_thesis_summary = state.current_thesis_summary
                target.current_stance = state.current_stance
                target.confidence_band = state.confidence_band
                target.dominant_channel_id = state.dominant_channel_id
                target.key_supporting_evidence_ids = state.key_supporting_evidence_ids
                target.key_contradicting_evidence_ids = (
                    state.key_contradicting_evidence_ids
                )
                target.what_would_falsify = state.what_would_falsify
                target.what_would_strengthen = state.what_would_strengthen
                target.reasoning_run_id = state.reasoning_run_id
            target.update_count = max(target.update_count or 0, state.update_count or 0)
            await self._retarget_conclusion_dependents(state.id, target.id)
            await self.session.execute(
                update(ConclusionRevision)
                .where(ConclusionRevision.conclusion_state_id == state.id)
                .values(conclusion_state_id=target.id)
            )
            await self.session.execute(
                delete(ConclusionState).where(ConclusionState.id == state.id)
            )
            moved += 1
        return moved

    async def _retarget_edges_to_theme(self, entity_id, theme_id) -> int:
        rows = (
            (
                await self.session.execute(
                    select(Edge).where(
                        or_(
                            (Edge.source_type == "entity")
                            & (Edge.source_id == entity_id),
                            (Edge.target_type == "entity")
                            & (Edge.target_id == entity_id),
                        )
                    )
                )
            )
            .scalars()
            .all()
        )
        for edge in rows:
            if edge.source_type == "entity" and edge.source_id == entity_id:
                edge.source_type = "theme"
                edge.source_id = theme_id
            if edge.target_type == "entity" and edge.target_id == entity_id:
                edge.target_type = "theme"
                edge.target_id = theme_id
        return len(rows)

    async def _clear_graph_cache_for_entity(self, entity_id) -> int:
        layout_result = await self.session.execute(
            delete(GraphNodeLayout).where(
                GraphNodeLayout.node_key == f"entity:{entity_id}"
            )
        )
        traversal_result = await self.session.execute(
            delete(GraphTraversalSet).where(
                or_(
                    (GraphTraversalSet.root_node_type == "entity")
                    & (GraphTraversalSet.root_node_id == entity_id),
                    GraphTraversalSet.node_ids.any(entity_id),
                )
            )
        )
        return int(layout_result.rowcount or 0) + int(traversal_result.rowcount or 0)

    async def _canonical_security_entities(self) -> list[dict]:
        rows = (
            await self.session.execute(
                select(Entity, Security, Position)
                .join(Security, Security.entity_id == Entity.id)
                .join(Position, Position.security_id == Security.id, isouter=True)
            )
        ).all()
        by_key: dict[tuple[str, str], dict] = {}
        for entity, security, position in rows:
            if not security.ticker:
                continue
            ticker = security.ticker.upper()
            key = (str(entity.id), ticker)
            entry = by_key.setdefault(
                key,
                {
                    "entity": entity,
                    "entity_id": entity.id,
                    "ticker": ticker,
                    "name": entity.name,
                    "is_active_holding": False,
                    "security_count": 0,
                },
            )
            entry["security_count"] += 1
            if position is not None and position.list_type == "holding":
                entry["is_active_holding"] = True
        return sorted(
            by_key.values(),
            key=lambda item: (
                not item["is_active_holding"],
                str(item["ticker"]),
                str(item["name"]),
            ),
        )

    async def _merge_profiles_into_entity(
        self, source_entity_id, target_entity_id
    ) -> int:
        old_profiles = (
            (
                await self.session.execute(
                    select(Profile).where(
                        Profile.subject_type == "entity",
                        Profile.subject_id == source_entity_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        if not old_profiles:
            return 0
        target = (
            await self.session.execute(
                select(Profile)
                .where(
                    Profile.subject_type == "entity",
                    Profile.subject_id == target_entity_id,
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        moved = 0
        for profile in old_profiles:
            if target is None:
                profile.subject_id = target_entity_id
                target = profile
                moved += 1
                continue
            if profile.id == target.id:
                continue
            for field in (
                "executive_summary",
                "business_model",
                "bull_case",
                "bear_case",
                "key_drivers",
                "competitor_landscape",
                "strategist_reasoning",
                "source_rationale",
            ):
                if not getattr(target, field) and getattr(profile, field):
                    setattr(target, field, getattr(profile, field))
            if profile.active_contradictions:
                target.active_contradictions = sorted(
                    {
                        *((target.active_contradictions or [])),
                        *profile.active_contradictions,
                    }
                )
            target.version = max(target.version or 1, profile.version or 1)
            await self.session.execute(
                text(
                    "UPDATE dossier_snapshots SET dossier_id = :target WHERE dossier_id = :old"
                ),
                {"target": target.id, "old": profile.id},
            )
            await self.session.execute(
                text(
                    "UPDATE dossier_deltas SET dossier_id = :target WHERE dossier_id = :old"
                ),
                {"target": target.id, "old": profile.id},
            )
            await self.session.execute(delete(Profile).where(Profile.id == profile.id))
            moved += 1
        return moved

    async def _merge_coverage_into_entity(
        self, source_entity_id, target_entity_id
    ) -> int:
        old_maps = (
            (
                await self.session.execute(
                    select(CoverageMap).where(
                        CoverageMap.subject_type == "entity",
                        CoverageMap.subject_id == source_entity_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        if not old_maps:
            return 0
        target = (
            await self.session.execute(
                select(CoverageMap)
                .where(
                    CoverageMap.subject_type == "entity",
                    CoverageMap.subject_id == target_entity_id,
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        moved = 0
        for coverage in old_maps:
            if target is None:
                coverage.subject_id = target_entity_id
                target = coverage
                moved += 1
                continue
            if coverage.id == target.id:
                continue
            target.total_evidence_count = max(
                target.total_evidence_count or 0, coverage.total_evidence_count or 0
            )
            target.high_tier_evidence_count = max(
                target.high_tier_evidence_count or 0,
                coverage.high_tier_evidence_count or 0,
            )
            target.contradiction_count = max(
                target.contradiction_count or 0, coverage.contradiction_count or 0
            )
            target.unresolved_contradiction_count = max(
                target.unresolved_contradiction_count or 0,
                coverage.unresolved_contradiction_count or 0,
            )
            target.overall_coverage_score = max(
                float(target.overall_coverage_score or 0.0),
                float(coverage.overall_coverage_score or 0.0),
            )
            target.evidence_class_coverage_json = {
                **(coverage.evidence_class_coverage_json or {}),
                **(target.evidence_class_coverage_json or {}),
            }
            await self.session.execute(
                update(UnresolvedQuestion)
                .where(UnresolvedQuestion.coverage_map_id == coverage.id)
                .values(coverage_map_id=target.id)
            )
            await self.session.execute(
                update(MissingEvidenceClass)
                .where(MissingEvidenceClass.coverage_map_id == coverage.id)
                .values(coverage_map_id=target.id)
            )
            await self.session.execute(
                delete(CoverageMap).where(CoverageMap.id == coverage.id)
            )
            moved += 1
        return moved

    async def _merge_conclusions_into_entity(
        self, source_entity_id, target_entity_id
    ) -> int:
        old_states = (
            (
                await self.session.execute(
                    select(ConclusionState).where(
                        ConclusionState.subject_type == "entity",
                        ConclusionState.subject_id == source_entity_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        if not old_states:
            return 0
        target = (
            await self.session.execute(
                select(ConclusionState)
                .where(
                    ConclusionState.subject_type == "entity",
                    ConclusionState.subject_id == target_entity_id,
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        moved = 0
        for state in old_states:
            if target is None:
                state.subject_id = target_entity_id
                target = state
                moved += 1
                continue
            if state.id == target.id:
                continue
            if target.current_stance == "no_view" and state.current_stance != "no_view":
                target.current_thesis_summary = state.current_thesis_summary
                target.current_stance = state.current_stance
                target.confidence_band = state.confidence_band
                target.dominant_channel_id = state.dominant_channel_id
                target.key_supporting_evidence_ids = state.key_supporting_evidence_ids
                target.key_contradicting_evidence_ids = (
                    state.key_contradicting_evidence_ids
                )
                target.what_would_falsify = state.what_would_falsify
                target.what_would_strengthen = state.what_would_strengthen
                target.reasoning_run_id = state.reasoning_run_id
            target.update_count = max(target.update_count or 0, state.update_count or 0)
            await self._retarget_conclusion_dependents(state.id, target.id)
            await self.session.execute(
                update(ConclusionRevision)
                .where(ConclusionRevision.conclusion_state_id == state.id)
                .values(conclusion_state_id=target.id)
            )
            await self.session.execute(
                delete(ConclusionState).where(ConclusionState.id == state.id)
            )
            moved += 1
        return moved

    async def _retarget_conclusion_dependents(
        self, old_state_id, target_state_id
    ) -> int:
        """Preserve historical rows that point at a conclusion state during merges."""
        counts = 0
        for model in (VerificationRun, DecisionJournal, Thesis):
            result = await self.session.execute(
                update(model)
                .where(model.conclusion_state_id == old_state_id)
                .values(conclusion_state_id=target_state_id)
            )
            counts += int(result.rowcount or 0)
        return counts

    async def _retarget_edges_to_entity(
        self, source_entity_id, target_entity_id
    ) -> int:
        rows = (
            (
                await self.session.execute(
                    select(Edge).where(
                        or_(
                            (Edge.source_type == "entity")
                            & (Edge.source_id == source_entity_id),
                            (Edge.target_type == "entity")
                            & (Edge.target_id == source_entity_id),
                        )
                    )
                )
            )
            .scalars()
            .all()
        )
        for edge in rows:
            if edge.source_type == "entity" and edge.source_id == source_entity_id:
                edge.source_id = target_entity_id
            if edge.target_type == "entity" and edge.target_id == source_entity_id:
                edge.target_id = target_entity_id
        return len(rows)

    async def _retarget_subject_aliases_to_entity(
        self, source_entity_id, target_entity_id
    ) -> int:
        aliases = (
            (
                await self.session.execute(
                    select(SubjectAlias).where(
                        SubjectAlias.subject_type == "entity",
                        SubjectAlias.subject_id == source_entity_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        moved = 0
        for alias in aliases:
            existing = (
                await self.session.execute(
                    select(SubjectAlias)
                    .where(
                        SubjectAlias.normalized_alias == alias.normalized_alias,
                        SubjectAlias.subject_type == "entity",
                        SubjectAlias.subject_id == target_entity_id,
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            if existing is not None:
                existing.confidence = max(
                    float(existing.confidence or 0.0), float(alias.confidence or 0.0)
                )
                if not existing.reason and alias.reason:
                    existing.reason = alias.reason
                await self.session.execute(
                    delete(SubjectAlias).where(SubjectAlias.id == alias.id)
                )
            else:
                alias.subject_id = target_entity_id
            moved += 1
        return moved

    @staticmethod
    def _merge_entity_fields(source: Entity, target: Entity) -> dict[str, object]:
        changed: dict[str, object] = {}
        source_aliases = [alias for alias in (source.aliases or []) if alias]
        if source.name:
            source_aliases.append(source.name)
        if source_aliases:
            target_aliases = list(target.aliases or [])
            merged_aliases = sorted({*target_aliases, *source_aliases})
            if merged_aliases != target_aliases:
                target.aliases = merged_aliases
                changed["aliases_added"] = sorted(
                    set(merged_aliases) - set(target_aliases)
                )
        for field in ("sector", "industry", "country", "description"):
            source_value = getattr(source, field)
            if not getattr(target, field) and source_value:
                setattr(target, field, source_value)
                changed[field] = source_value
        return changed

    async def _merge_duplicate_entity(
        self, source: Entity, target: Entity, record: dict
    ) -> dict:
        investment_objects = await self._retarget_investment_objects_to_entity(
            source.id, target.id
        )
        moved = {
            "profiles": await self._merge_profiles_into_entity(source.id, target.id),
            "coverage": await self._merge_coverage_into_entity(source.id, target.id),
            "conclusions": await self._merge_conclusions_into_entity(
                source.id, target.id
            ),
            "edges": await self._retarget_edges_to_entity(source.id, target.id),
            "subject_aliases": await self._retarget_subject_aliases_to_entity(
                source.id, target.id
            ),
            "entity_fields": self._merge_entity_fields(source, target),
            "graph_cache_rows": await self._clear_graph_cache_for_entity(source.id),
            **investment_objects,
        }
        await self._record_duplicate_entity_merge(source, target, record, moved)
        await self.session.execute(delete(Entity).where(Entity.id == source.id))
        return {"target_id": str(target.id), "target_name": target.name, "moved": moved}

    async def _merge_duplicate_entities(self, *, dry_run: bool) -> list[dict]:
        canonical_entries = await self._canonical_security_entities()
        if not canonical_entries:
            return []
        securityless_entities = (
            (
                await self.session.execute(
                    select(Entity)
                    .join(Security, Security.entity_id == Entity.id, isouter=True)
                    .where(Security.id.is_(None))
                    .order_by(Entity.name.asc())
                )
            )
            .scalars()
            .all()
        )
        merged: list[dict] = []
        for entity in securityless_entities:
            if not entity.name or self._classify(entity.name) == "artifact":
                continue
            target_record = self._duplicate_entity_target_name(
                entity.name, canonical_entries
            )
            if target_record is None:
                continue
            target = target_record["entity"]
            if target.id == entity.id:
                continue
            linkage = await self._portfolio_linkage(entity.id)
            if sum(linkage.values()) != 0:
                continue
            satellites = await self._satellite_counts(entity.id)
            record = {
                "id": str(entity.id),
                "name": entity.name,
                "entity_type": entity.entity_type,
                "normalized": normalize_subject_name(entity.name),
                "linkage": linkage,
                "satellites": satellites,
                "target_id": str(target.id),
                "target_name": target.name,
                "target_ticker": target_record.get("ticker"),
                "match_reason": target_record.get("match_reason"),
                "match_score": target_record.get("match_score"),
            }
            if not dry_run:
                record["merge"] = await self._merge_duplicate_entity(
                    entity, target, record
                )
            merged.append(record)
        return merged

    async def _reclassify_entity_as_theme(
        self, entity: Entity, record: dict, theme_name: str | None = None
    ) -> dict:
        theme = await self._get_or_create_theme(theme_name or entity.name)
        investment_objects = await self._retarget_investment_objects_to_theme(
            entity.id, theme.id
        )
        moved = {
            "profiles": await self._merge_profiles_into_theme(entity.id, theme.id),
            "coverage": await self._merge_coverage_into_theme(entity.id, theme.id),
            "conclusions": await self._merge_conclusions_into_theme(
                entity.id, theme.id
            ),
            "edges": await self._retarget_edges_to_theme(entity.id, theme.id),
            "graph_cache_rows": await self._clear_graph_cache_for_entity(entity.id),
            **investment_objects,
        }
        await self._record_entity_reclassification(entity, theme, record, moved)
        await self.session.execute(delete(Entity).where(Entity.id == entity.id))
        return {"theme_id": str(theme.id), "theme_name": theme.name, "moved": moved}

    async def run(
        self,
        *,
        dry_run: bool = False,
        mode: str = "conservative",
        reclassify_topics: bool = True,
        merge_duplicate_entities: bool = True,
    ) -> dict:
        """mode: 'conservative' deletes only artifact-class junk; 'aggressive'
        also deletes unusable-class junk. Portfolio-linked nodes are never
        deleted in either mode."""
        duplicate_merges = (
            await self._merge_duplicate_entities(dry_run=dry_run)
            if merge_duplicate_entities
            else []
        )
        entities = (await self.session.execute(select(Entity))).scalars().all()
        deleted: list[dict] = []
        reclassified: list[dict] = []
        flagged: list[dict] = []

        for e in entities:
            kind = self._classify(e.name)
            if kind is None:
                continue
            linkage = await self._portfolio_linkage(e.id)
            satellites = await self._satellite_counts(e.id)
            profiles = (
                (
                    await self.session.execute(
                        select(Profile).where(
                            Profile.subject_type == "entity", Profile.subject_id == e.id
                        )
                    )
                )
                .scalars()
                .all()
            )
            record = {
                "id": str(e.id),
                "name": e.name,
                "entity_type": e.entity_type,
                "kind": kind,
                "normalized": normalize_subject_name(e.name),
                "linkage": linkage,
                "satellites": satellites,
            }
            placeholder_only = self._placeholder_only_entity(profiles)
            deletable = sum(linkage.values()) == 0 and (
                kind == "artifact"
                or (kind == "unusable" and mode == "aggressive")
                or (
                    kind == "unusable"
                    and placeholder_only
                    and satellites["edges"] == 0
                    and satellites["conclusions"] == 0
                )
            )
            reclassifiable = (
                reclassify_topics
                and kind == "unusable"
                and sum(linkage.values()) == 0
                and not placeholder_only
                and (
                    self._is_migratable_topic_label(e.name)
                    or self._should_reclassify_unusable_entity(e.name, profiles)
                )
            )
            if reclassifiable:
                record["target_theme_name"] = self._theme_name_from_profiles(
                    e.name, profiles
                )
                if not dry_run:
                    record["reclassification"] = await self._reclassify_entity_as_theme(
                        e,
                        record,
                        record["target_theme_name"],
                    )
                reclassified.append(record)
            elif deletable:
                if not dry_run:
                    await self._record_entity_deletion(e, record)
                    await self._cascade_delete(e.id)
                deleted.append(record)
            else:
                record["kept_reason"] = (
                    "portfolio-linked"
                    if sum(linkage.values())
                    else f"{kind} (needs {mode}=aggressive)"
                )
                flagged.append(record)

        if not dry_run:
            await self.session.commit()

        summary = {
            "ran_at": datetime.now(UTC).isoformat(),
            "dry_run": dry_run,
            "mode": mode,
            "reclassify_topics": reclassify_topics,
            "merge_duplicate_entities": merge_duplicate_entities,
            "scanned": len(entities),
            "duplicate_merge_count": len(duplicate_merges),
            "deleted_count": len(deleted),
            "reclassified_count": len(reclassified),
            "flagged_count": len(flagged),
            "duplicate_merges": duplicate_merges,
            "deleted": deleted,
            "reclassified": reclassified,
            "flagged": flagged,
        }
        self._write_audit(summary)
        return summary

    @staticmethod
    def _audit_dir() -> str:
        return os.path.join(
            os.path.dirname(settings.STORAGE_DIR), "maintenance", "entity_hygiene"
        )

    def _write_audit(self, summary: dict) -> None:
        base = self._audit_dir()
        os.makedirs(base, exist_ok=True)
        ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        with open(os.path.join(base, f"{ts}.json"), "w") as f:
            json.dump(summary, f, indent=2)
        with open(os.path.join(base, "index.log"), "a") as f:
            f.write(
                f"{summary['ran_at']} mode={summary['mode']} scanned={summary['scanned']} "
                f"merged={summary.get('duplicate_merge_count', 0)} "
                f"deleted={summary['deleted_count']} reclassified={summary.get('reclassified_count', 0)} "
                f"flagged={summary['flagged_count']} "
                f"dry_run={summary['dry_run']}\n"
            )

    def latest_audit(self) -> dict | None:
        base = self._audit_dir()
        if not os.path.isdir(base):
            return None
        files = sorted(f for f in os.listdir(base) if f.endswith(".json"))
        if not files:
            return None
        with open(os.path.join(base, files[-1])) as f:
            return json.load(f)

    async def _record_entity_deletion(self, entity: Entity, record: dict) -> None:
        await KnowledgeAuditService(self.session).record_change(
            node_type="entity",
            node_id=entity.id,
            change_type=f"deleted_{record['kind']}",
            reason=(
                "Entity hygiene removed a non-portfolio-linked junk entity and its auto-generated satellites."
            ),
            actor="entity_hygiene",
            subject_type="entity",
            subject_id=entity.id,
            metadata={
                "label": entity.name,
                "entity_type": entity.entity_type,
                "classification": record["kind"],
                "normalized": record["normalized"],
                "linkage": record["linkage"],
                "satellites": record["satellites"],
            },
        )

    async def _record_entity_reclassification(
        self,
        entity: Entity,
        theme: Theme,
        record: dict,
        moved: dict,
    ) -> None:
        await KnowledgeAuditService(self.session).record_change(
            node_type="theme",
            node_id=theme.id,
            change_type="reclassified_topic_entity",
            reason=(
                "Entity hygiene moved a non-portfolio generic topic out of the entity layer "
                "and into the theme layer."
            ),
            actor="entity_hygiene",
            source_type="entity",
            source_id=entity.id,
            subject_type="theme",
            subject_id=theme.id,
            metadata={
                "old_entity_id": str(entity.id),
                "old_label": entity.name,
                "old_entity_type": entity.entity_type,
                "new_theme_id": str(theme.id),
                "new_theme_name": theme.name,
                "classification": record["kind"],
                "normalized": record["normalized"],
                "linkage": record["linkage"],
                "satellites_before": record["satellites"],
                "moved": moved,
            },
        )

    async def _record_duplicate_entity_merge(
        self,
        source: Entity,
        target: Entity,
        record: dict,
        moved: dict,
    ) -> None:
        await KnowledgeAuditService(self.session).record_change(
            node_type="entity",
            node_id=target.id,
            change_type="merged_duplicate_entity",
            reason=(
                "Entity hygiene merged a security-less duplicate entity into the canonical "
                "security-backed entity."
            ),
            actor="entity_hygiene",
            source_type="entity",
            source_id=source.id,
            subject_type="entity",
            subject_id=target.id,
            metadata={
                "old_entity_id": str(source.id),
                "old_label": source.name,
                "old_entity_type": source.entity_type,
                "canonical_entity_id": str(target.id),
                "canonical_label": target.name,
                "target_ticker": record.get("target_ticker"),
                "match_reason": record.get("match_reason"),
                "match_score": record.get("match_score"),
                "normalized": record.get("normalized"),
                "linkage": record.get("linkage"),
                "satellites_before": record.get("satellites"),
                "moved": moved,
            },
        )
