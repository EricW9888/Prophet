from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime
from typing import Any

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
from investos.models.graph import Edge, GraphNodeLayout, GraphTraversalSet
from investos.models.profile import Profile
from investos.models.subject_alias import SubjectAlias
from investos.models.theme import Theme
from investos.models.thesis import Thesis
from investos.models.verification import VerificationRun
from investos.services.artifact_hygiene import (
    is_artifact_research_query,
    is_artifact_subject_name,
    is_placeholder_profile_text,
    label_from_profile_texts,
    normalize_subject_name,
    strip_research_wrappers,
)
from investos.services.knowledge_audit import KnowledgeAuditService


class ThemeHygieneService:
    """Auditable cleanup for theme-layer wrapper artifacts.

    Themes are supposed to be durable narratives. Auto-generated labels such as
    ``Research on: ...`` or ``Autonomous reflection: ...`` sometimes leak into
    that layer with only placeholder profile/coverage rows. This service removes
    those wrappers only when they have no substantive thesis/profile/conclusion
    content and no explicit portfolio tag.
    """

    _TICKER_PREFIX_RE = re.compile(r"^([A-Z][A-Z0-9.]{0,5})\s*:\s*(.{12,})$")
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
    _MAX_CLEAN_LABEL_LENGTH = 140

    def __init__(self, session: AsyncSession):
        self.session = session

    @classmethod
    def _is_artifact_theme_name(cls, name: str | None) -> bool:
        return bool(is_artifact_subject_name(name) or is_artifact_research_query(name))

    @classmethod
    def _is_placeholder_profile_text(cls, value: str | None) -> bool:
        return is_placeholder_profile_text(value)

    @classmethod
    def _profile_has_substantive_text(cls, profile: Profile) -> bool:
        for field in cls._PROFILE_TEXT_FIELDS:
            value = (getattr(profile, field, None) or "").strip()
            if value and not cls._is_placeholder_profile_text(value):
                return True
        return bool(profile.active_contradictions or [])

    @staticmethod
    def _conclusion_has_substantive_state(conclusion: ConclusionState) -> bool:
        if (conclusion.current_thesis_summary or "").strip():
            return True
        if conclusion.current_stance != "no_view":
            return True
        if conclusion.confidence_band != "very_low":
            return True
        return bool(
            (conclusion.key_supporting_evidence_ids or [])
            or (conclusion.key_contradicting_evidence_ids or [])
            or (conclusion.what_would_falsify or [])
            or (conclusion.what_would_strengthen or [])
        )

    async def _count(self, stmt) -> int:
        return int((await self.session.execute(stmt)).scalar() or 0)

    async def _satellite_counts(self, theme_id) -> dict[str, int]:
        return {
            "profiles": await self._count(
                select(func.count())
                .select_from(Profile)
                .where(Profile.subject_type == "theme", Profile.subject_id == theme_id)
            ),
            "coverage": await self._count(
                select(func.count())
                .select_from(CoverageMap)
                .where(
                    CoverageMap.subject_type == "theme",
                    CoverageMap.subject_id == theme_id,
                )
            ),
            "conclusions": await self._count(
                select(func.count())
                .select_from(ConclusionState)
                .where(
                    ConclusionState.subject_type == "theme",
                    ConclusionState.subject_id == theme_id,
                )
            ),
            "edges": await self._count(
                select(func.count())
                .select_from(Edge)
                .where(
                    or_(
                        (Edge.source_type == "theme") & (Edge.source_id == theme_id),
                        (Edge.target_type == "theme") & (Edge.target_id == theme_id),
                    )
                )
            ),
            "theses": await self._count(
                select(func.count())
                .select_from(Thesis)
                .where(Thesis.theme_id == theme_id)
            ),
            "aliases": await self._count(
                select(func.count())
                .select_from(SubjectAlias)
                .where(
                    SubjectAlias.subject_type == "theme",
                    SubjectAlias.subject_id == theme_id,
                )
            ),
        }

    async def _theme_payload(self, theme: Theme) -> dict[str, Any]:
        profiles = (
            (
                await self.session.execute(
                    select(Profile).where(
                        Profile.subject_type == "theme", Profile.subject_id == theme.id
                    )
                )
            )
            .scalars()
            .all()
        )
        conclusions = (
            (
                await self.session.execute(
                    select(ConclusionState).where(
                        ConclusionState.subject_type == "theme",
                        ConclusionState.subject_id == theme.id,
                    )
                )
            )
            .scalars()
            .all()
        )
        return {
            "profiles": profiles,
            "conclusions": conclusions,
            "has_substantive_profile": any(
                self._profile_has_substantive_text(item) for item in profiles
            ),
            "has_substantive_conclusion": any(
                self._conclusion_has_substantive_state(item) for item in conclusions
            ),
        }

    async def _record_theme_deletion(
        self, theme: Theme, record: dict[str, Any]
    ) -> None:
        await KnowledgeAuditService(self.session).record_change(
            node_type="theme",
            node_id=theme.id,
            change_type="deleted_artifact_theme",
            reason=(
                "Theme hygiene removed a non-portfolio wrapper theme with only placeholder "
                "profile/coverage state."
            ),
            actor="theme_hygiene",
            subject_type="theme",
            subject_id=theme.id,
            metadata={
                "label": theme.name,
                "normalized": record["normalized"],
                "satellites": record["satellites"],
            },
        )

    async def _record_theme_rename(
        self, theme: Theme, old_name: str, new_name: str
    ) -> None:
        await KnowledgeAuditService(self.session).record_change(
            node_type="theme",
            node_id=theme.id,
            change_type="renamed_artifact_theme",
            reason=(
                "Theme hygiene removed an internal wrapper prefix from a substantive "
                "theme while preserving its profile/coverage state."
            ),
            actor="theme_hygiene",
            subject_type="theme",
            subject_id=theme.id,
            metadata={"old_name": old_name, "new_name": new_name},
        )

    @classmethod
    def _label_from_profiles(cls, profiles: list[Profile]) -> str | None:
        values = [
            getattr(profile, field, None) or ""
            for profile in profiles
            for field in cls._PROFILE_TEXT_FIELDS
        ]
        return label_from_profile_texts(values, max_length=cls._MAX_CLEAN_LABEL_LENGTH)

    @staticmethod
    def _looks_truncated_label(value: str) -> bool:
        stripped = value.strip()
        if stripped.endswith("..."):
            return True
        if stripped.endswith((",", "/", "(", "-", ":")):
            return True
        if len(stripped) >= 95 and not stripped.endswith("?"):
            return True
        return False

    @classmethod
    def _clean_artifact_theme_name(
        cls, name: str | None, profiles: list[Profile] | None = None
    ) -> str | None:
        original = (name or "").strip()
        cleaned = strip_research_wrappers(original).strip(" .·-—").strip()
        if not cleaned or cleaned == original:
            return None
        profile_label = cls._label_from_profiles(profiles or [])
        ticker_match = cls._TICKER_PREFIX_RE.match(cleaned)
        if ticker_match:
            ticker = ticker_match.group(1)
            detail = profile_label or ticker_match.group(2).strip()
            cleaned = f"{ticker} - {detail}"
        cleaned = " ".join(cleaned.split()).strip()
        if not ticker_match and profile_label and cls._looks_truncated_label(cleaned):
            cleaned = profile_label
        if not cleaned or cleaned.casefold() == original.casefold():
            return None
        if is_artifact_subject_name(cleaned) or is_artifact_research_query(cleaned):
            return None
        return cleaned

    async def _clear_graph_cache_for_theme(self, theme_id) -> int:
        layout_result = await self.session.execute(
            delete(GraphNodeLayout).where(
                GraphNodeLayout.node_key == f"theme:{theme_id}"
            )
        )
        traversal_result = await self.session.execute(
            delete(GraphTraversalSet).where(
                or_(
                    (GraphTraversalSet.root_node_type == "theme")
                    & (GraphTraversalSet.root_node_id == theme_id),
                    GraphTraversalSet.node_ids.any(theme_id),
                )
            )
        )
        return int(layout_result.rowcount or 0) + int(traversal_result.rowcount or 0)

    async def _cascade_delete_theme(self, theme_id) -> dict[str, int]:
        moved: dict[str, int] = {}

        cov_ids = (
            (
                await self.session.execute(
                    select(CoverageMap.id).where(
                        CoverageMap.subject_type == "theme",
                        CoverageMap.subject_id == theme_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        moved["coverage"] = len(cov_ids)
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

        prof_ids = (
            (
                await self.session.execute(
                    select(Profile.id).where(
                        Profile.subject_type == "theme", Profile.subject_id == theme_id
                    )
                )
            )
            .scalars()
            .all()
        )
        moved["profiles"] = len(prof_ids)
        if prof_ids:
            await self.session.execute(
                text("DELETE FROM dossier_snapshots WHERE dossier_id = ANY(:ids)"),
                {"ids": prof_ids},
            )
            await self.session.execute(
                text("DELETE FROM dossier_deltas WHERE dossier_id = ANY(:ids)"),
                {"ids": prof_ids},
            )
            await self.session.execute(delete(Profile).where(Profile.id.in_(prof_ids)))

        conc_ids = (
            (
                await self.session.execute(
                    select(ConclusionState.id).where(
                        ConclusionState.subject_type == "theme",
                        ConclusionState.subject_id == theme_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        moved["conclusions"] = len(conc_ids)
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

        edge_result = await self.session.execute(
            delete(Edge).where(
                or_(
                    (Edge.source_type == "theme") & (Edge.source_id == theme_id),
                    (Edge.target_type == "theme") & (Edge.target_id == theme_id),
                )
            )
        )
        moved["edges"] = int(edge_result.rowcount or 0)

        alias_result = await self.session.execute(
            delete(SubjectAlias).where(
                SubjectAlias.subject_type == "theme",
                SubjectAlias.subject_id == theme_id,
            )
        )
        moved["aliases"] = int(alias_result.rowcount or 0)
        moved["graph_cache_rows"] = await self._clear_graph_cache_for_theme(theme_id)

        await self.session.execute(delete(Theme).where(Theme.id == theme_id))
        return moved

    async def run(self, *, dry_run: bool = False) -> dict[str, Any]:
        themes = (await self.session.execute(select(Theme))).scalars().all()
        existing_names = {
            theme.name.casefold(): theme.id for theme in themes if theme.name
        }
        deleted: list[dict[str, Any]] = []
        renamed: list[dict[str, Any]] = []
        flagged: list[dict[str, Any]] = []

        for theme in themes:
            if not self._is_artifact_theme_name(theme.name):
                continue
            satellites = await self._satellite_counts(theme.id)
            payload = await self._theme_payload(theme)
            record = {
                "id": str(theme.id),
                "name": theme.name,
                "normalized": normalize_subject_name(theme.name),
                "satellites": satellites,
            }
            deletable = (
                satellites["theses"] == 0
                and theme.triggering_event_id is None
                and not (theme.tagged_security_ids or [])
                and not payload["has_substantive_profile"]
                and not payload["has_substantive_conclusion"]
            )
            if not deletable:
                cleaned_name = self._clean_artifact_theme_name(
                    theme.name, payload["profiles"]
                )
                existing_id = existing_names.get((cleaned_name or "").casefold())
                if cleaned_name and (existing_id is None or existing_id == theme.id):
                    record["new_name"] = cleaned_name
                    if not dry_run:
                        old_name = theme.name
                        existing_names.pop(old_name.casefold(), None)
                        theme.name = cleaned_name
                        theme.last_updated_at = datetime.now(UTC)
                        existing_names[cleaned_name.casefold()] = theme.id
                        await self._record_theme_rename(theme, old_name, cleaned_name)
                    renamed.append(record)
                    continue
                record["kept_reason"] = (
                    "has substantive profile/conclusion, thesis, event, or tagged security"
                )
                if cleaned_name and existing_id and existing_id != theme.id:
                    record[
                        "kept_reason"
                    ] += "; cleaned name would collide with an existing theme"
                flagged.append(record)
                continue
            if not dry_run:
                await self._record_theme_deletion(theme, record)
                record["deleted_rows"] = await self._cascade_delete_theme(theme.id)
            deleted.append(record)

        if not dry_run:
            await self.session.commit()

        summary = {
            "ran_at": datetime.now(UTC).isoformat(),
            "dry_run": dry_run,
            "scanned": len(themes),
            "deleted_count": len(deleted),
            "renamed_count": len(renamed),
            "flagged_count": len(flagged),
            "deleted": deleted,
            "renamed": renamed,
            "flagged": flagged,
        }
        self._write_audit(summary)
        return summary

    @staticmethod
    def _audit_dir() -> str:
        return os.path.join(
            os.path.dirname(settings.STORAGE_DIR), "maintenance", "theme_hygiene"
        )

    def _write_audit(self, summary: dict[str, Any]) -> None:
        base = self._audit_dir()
        os.makedirs(base, exist_ok=True)
        ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        with open(os.path.join(base, f"{ts}.json"), "w") as f:
            json.dump(summary, f, indent=2)
        with open(os.path.join(base, "index.log"), "a") as f:
            f.write(
                f"{summary['ran_at']} scanned={summary['scanned']} "
                f"deleted={summary['deleted_count']} renamed={summary['renamed_count']} "
                f"flagged={summary['flagged_count']} "
                f"dry_run={summary['dry_run']}\n"
            )
