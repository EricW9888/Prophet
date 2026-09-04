from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from investos.config import settings
from investos.core.llm import call_llm_json, compact_exception_message
from investos.core.research_providers import (
    RESEARCH_PROVIDER_CAPABILITIES,
    ProviderSearchResponse,
    configured_research_providers,
    search_research_provider,
)
from investos.core.url_security import UnsafeUrlError, UrlFetchNetworkError
from investos.models.coverage import CoverageMap, Resolution, UnresolvedQuestion
from investos.models.entity import Entity, Security
from investos.models.evidence import (
    RawEvidence,
    ResearchDiscoveryObservation,
    SourceItem,
)
from investos.models.portfolio import Position
from investos.models.theme import Theme
from investos.schemas.evidence import RawEvidenceCreate
from investos.services.agent_action_log import AgentActionLogService
from investos.services.artifact_hygiene import (
    is_artifact_research_query,
    is_artifact_subject_name,
    strip_research_wrappers,
)
from investos.services.ingestion import IngestionService
from investos.services.runtime_settings import RuntimeSettingsStore
from investos.services.source_learning import SourceLearningService
from investos.workers.extraction import ExtractionWorker

QUESTION_RESOLUTION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "answered": {"type": "boolean"},
        "summary": {"type": "string"},
        "remaining_uncertainty": {"type": ["string", "null"]},
    },
    "required": ["answered", "summary", "remaining_uncertainty"],
}


@dataclass
class ResearchRunResult:
    started: bool
    reason: str
    evidence_id: UUID | None = None
    processed: bool = False
    loop_detail: dict[str, Any] | None = None
    query: str | None = None
    title: str | None = None

    @property
    def telemetry_status(self) -> str:
        """Translate the structured research outcome into operator severity."""
        if self.started or self.evidence_id is not None:
            return "ok"
        if self.reason == "research_provider_not_configured":
            return "waiting_for_config"
        if self.reason in {
            "duplicate_recent_research",
            "research_artifact_query_blocked",
        }:
            return "ok"
        return "warning"


@dataclass
class ResearchSearchResult:
    searched: bool
    reason: str
    query: str
    results: list[dict[str, Any]] = field(default_factory=list)
    request_id: str | None = None
    provider: str | None = None
    provider_attempts: list[dict[str, Any]] = field(default_factory=list)
    variants_tried: list[str] = field(default_factory=list)
    observation_ids_by_url: dict[str, UUID] = field(default_factory=dict)


DISCOVERY_OBSERVATION_LIMIT = 20
DISCOVERY_SNIPPET_LIMIT = 4000


class ResearchService:
    def __init__(self, session: AsyncSession):
        self.session = session
        self.ingestion = IngestionService(session)
        self.source_learning = SourceLearningService(session)

    @staticmethod
    def _log_research_action(
        *,
        status: str,
        summary: str,
        query: str,
        title: str,
        metadata_json: dict | None = None,
    ) -> None:
        metadata = dict(metadata_json or {})
        metadata.update(
            {
                "query": query,
                "title": title,
            }
        )
        session_id = metadata.get("session_id")
        AgentActionLogService.append(
            source="research",
            action_type="external_research",
            status=status,
            summary=summary,
            subject_id=metadata.get("subject_id"),
            subject_type=metadata.get("subject_type"),
            subject_name=metadata.get("subject_name"),
            session_id=str(session_id) if session_id else None,
            metadata=metadata,
        )

    @staticmethod
    def _usage_log_path() -> Path:
        path = Path(settings.STORAGE_DIR) / "_system" / "research_requests.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    async def _record_discovery_results(
        self,
        *,
        provider: str,
        request_id: str | None,
        input_query: str,
        effective_query: str,
        title: str,
        results: list[dict[str, Any]],
        metadata: dict[str, Any],
    ) -> dict[str, UUID]:
        if self.session is None:
            return {}
        observation_ids: dict[str, UUID] = {}
        seen_urls: set[str] = set()
        for rank, result in enumerate(results[:DISCOVERY_OBSERVATION_LIMIT], start=1):
            url = str(result.get("url") or "").strip()
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            content = " ".join(str(result.get("content") or "").split()).strip()
            observation = ResearchDiscoveryObservation(
                provider=provider,
                request_id=request_id,
                query=input_query,
                effective_query=effective_query,
                search_title=title,
                result_rank=rank,
                result_title=str(result.get("title") or url).strip(),
                url=url,
                snippet=content[:DISCOVERY_SNIPPET_LIMIT] or None,
                content_kind=str(result.get("content_kind") or "snippet"),
                outcome="observed",
                subject_type=(
                    str(metadata.get("subject_type"))
                    if metadata.get("subject_type")
                    else None
                ),
                subject_id=(
                    str(metadata.get("subject_id"))
                    if metadata.get("subject_id")
                    else None
                ),
                subject_name=(
                    str(metadata.get("subject_name"))
                    if metadata.get("subject_name")
                    else None
                ),
                metadata_json={
                    "score": result.get("score"),
                    "published_date": result.get("published_date"),
                    "engines": result.get("engines") or [],
                },
            )
            self.session.add(observation)
            await self.session.flush()
            observation_ids[url] = observation.id
        return observation_ids

    async def _update_discovery_outcome(
        self,
        observation_id: UUID | None,
        *,
        outcome: str,
        evidence_id: UUID | None = None,
        error: str | None = None,
    ) -> None:
        if self.session is None or observation_id is None:
            return
        await self.session.execute(
            update(ResearchDiscoveryObservation)
            .where(ResearchDiscoveryObservation.id == observation_id)
            .values(outcome=outcome, evidence_id=evidence_id, error=error)
        )

    @staticmethod
    def _legacy_usage_log_path() -> Path:
        return Path(settings.STORAGE_DIR) / "_system" / "tavily_requests.jsonl"

    @classmethod
    def _append_usage_log(cls, entry: dict[str, Any]) -> None:
        try:
            path = cls._usage_log_path()
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=True, default=str) + "\n")
        except Exception as exc:
            import logging

            logging.getLogger(__name__).warning(
                "Research usage log write failed: %s%s",
                type(exc).__name__,
                f": {str(exc)}" if str(exc).strip() else "",
            )
            # Research should not fail just because local diagnostics logging failed.
            return

    @classmethod
    def recent_request_log(cls, limit: int = 40) -> list[dict[str, Any]]:
        lines: list[str] = []
        for path in (cls._legacy_usage_log_path(), cls._usage_log_path()):
            if not path.exists():
                continue
            try:
                lines.extend(path.read_text(encoding="utf-8").splitlines())
            except Exception as exc:
                import logging

                logging.getLogger(__name__).warning(
                    "Research usage log read failed: %s%s",
                    type(exc).__name__,
                    f": {str(exc)}" if str(exc).strip() else "",
                )
        entries: list[dict[str, Any]] = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        entries.sort(key=lambda item: str(item.get("timestamp") or ""))
        return list(reversed(entries[-limit:]))

    @classmethod
    def _tavily_credits_used_this_month(cls) -> int:
        month_prefix = datetime.now(timezone.utc).strftime("%Y-%m")
        return sum(
            int(entry.get("estimated_credits") or 0)
            for entry in cls.recent_request_log(limit=100_000)
            if entry.get("provider") == "tavily"
            and str(entry.get("timestamp") or "").startswith(month_prefix)
        )

    @classmethod
    async def current_usage_snapshot(cls) -> dict[str, Any]:
        research = RuntimeSettingsStore.load().research
        recent_requests = cls.recent_request_log()
        configured = configured_research_providers(research)
        provider_label = " then ".join(
            RESEARCH_PROVIDER_CAPABILITIES[item].label for item in configured
        )
        if not configured:
            return {
                "provider": "none",
                "ready": False,
                "status_message": "Configure a SearXNG endpoint or Tavily API key.",
                "key": None,
                "account": None,
                "recent_requests": recent_requests,
            }

        if not research.api_key:
            return {
                "provider": provider_label,
                "ready": True,
                "status_message": "SearXNG is configured; no metered fallback is enabled.",
                "key": None,
                "account": None,
                "recent_requests": recent_requests,
            }

        async with httpx.AsyncClient(timeout=10.0, trust_env=False) as client:
            try:
                response = await client.get(
                    "https://api.tavily.com/usage",
                    headers={"Authorization": f"Bearer {research.api_key}"},
                )
                if response.status_code == 429:
                    return {
                        "provider": provider_label,
                        "ready": True,
                        "status_message": "Tavily is configured, but the usage endpoint is temporarily rate-limited.",
                        "key": None,
                        "account": None,
                        "recent_requests": recent_requests,
                    }
                response.raise_for_status()
                data = response.json()
                return {
                    "provider": provider_label,
                    "ready": True,
                    "status_message": "Tavily usage loaded.",
                    "key": data.get("key"),
                    "account": data.get("account"),
                    "recent_requests": recent_requests,
                }
            except Exception as exc:
                free_provider_ready = "searxng" in configured
                return {
                    "provider": provider_label,
                    "ready": free_provider_ready,
                    "status_message": f"Unable to load Tavily usage: {exc}",
                    "key": None,
                    "account": None,
                    "recent_requests": recent_requests,
                }

    @staticmethod
    def _compact_spaces(value: str | None) -> str:
        return " ".join((value or "").split()).strip()

    @classmethod
    def _comparison_key(cls, value: str | None) -> str:
        compact = cls._compact_spaces(value).lower()
        compact = re.sub(r"[^a-z0-9]+", " ", compact)
        return " ".join(compact.split())

    @classmethod
    def _strip_research_wrappers(cls, value: str | None) -> str:
        return strip_research_wrappers(value)

    @classmethod
    def _is_artifact_subject_name(cls, value: str | None) -> bool:
        return is_artifact_subject_name(value)

    @classmethod
    def _is_artifact_research_query(cls, value: str | None) -> bool:
        return is_artifact_research_query(value)

    @classmethod
    def _clean_research_title(
        cls,
        *,
        title: str,
        query: str,
        metadata_json: dict | None = None,
    ) -> str:
        compact = cls._compact_spaces(title)
        subject_name = cls._compact_spaces((metadata_json or {}).get("subject_name"))
        if subject_name and cls._is_artifact_subject_name(compact):
            return f"Research on {subject_name}: {cls._title_fragment(query)}"
        for _ in range(4):
            cleaned = re.sub(
                r"(?i)^\s*research on\s+research on\s+", "Research on ", compact
            ).strip()
            cleaned = re.sub(
                r"(?i)^\s*research on\s*:\s*research on\s+", "Research on ", cleaned
            ).strip()
            if cleaned == compact:
                break
            compact = cleaned
        return compact or cls._title_fragment(query)

    @staticmethod
    def _title_fragment(value: str | None, *, max_chars: int = 110) -> str:
        compact = " ".join((value or "").split()).strip(" .:")
        if not compact:
            return "external research"
        if len(compact) > max_chars:
            return compact[: max_chars - 3].rstrip() + "..."
        return compact

    @staticmethod
    def _normalize_search_query(query: str) -> str:
        compact = " ".join((query or "").split()).strip()
        if not compact:
            return compact
        compact = strip_research_wrappers(compact)
        compact = re.sub(
            r"(?i)^(what do you think about|can you analyze|please analyze|can you research|please research)\s+",
            "",
            compact,
        ).strip()
        compact = re.sub(
            r"(?i)^(i plan on|i am|i'm|i think|i believe|what about|how about|thoughts on)\s+",
            "",
            compact,
        ).strip()
        compact = re.sub(r"(?i)\bwhat do you think\??$", "", compact).strip(" .?")
        compact = compact.replace("?", " ").replace(":", " ")
        compact = " ".join(compact.split())
        # Truncate to avoid HTTP 432 (Request Header Fields Too Large)
        if len(compact) > 240:
            compact = compact[:237] + "..."
        return compact or query

    @classmethod
    def _search_query_variants(cls, query: str) -> list[str]:
        normalized = cls._normalize_search_query(query)
        variants: list[str] = []
        if normalized:
            variants.append(normalized)

        tokens = re.findall(r"[A-Za-z0-9][A-Za-z0-9+./-]*", normalized)
        stopwords = {
            "a",
            "an",
            "and",
            "are",
            "as",
            "at",
            "be",
            "because",
            "by",
            "for",
            "from",
            "how",
            "i",
            "if",
            "in",
            "into",
            "is",
            "it",
            "its",
            "of",
            "on",
            "or",
            "over",
            "should",
            "that",
            "the",
            "their",
            "they",
            "this",
            "to",
            "what",
            "when",
            "where",
            "which",
            "who",
            "why",
            "with",
            "would",
            "you",
            "your",
        }
        keyword_tokens: list[str] = []
        for token in tokens:
            lowered = token.lower()
            if lowered in stopwords:
                continue
            if len(lowered) <= 2 and not token.isupper():
                continue
            keyword_tokens.append(token)
        keyword_variant = " ".join(keyword_tokens[:12]).strip()
        if keyword_variant and keyword_variant not in variants:
            variants.append(keyword_variant)

        if len(keyword_tokens) >= 3:
            phrase_variant = " ".join(keyword_tokens[:6]).strip()
            quoted_variant = f'"{phrase_variant}"'
            if quoted_variant.lower() not in {item.lower() for item in variants}:
                variants.append(quoted_variant)

        if len(keyword_tokens) >= 2:
            concise_variant = " ".join(keyword_tokens[:4]).strip()
            if concise_variant and concise_variant.lower() not in {
                item.lower() for item in variants
            }:
                variants.append(concise_variant)

        deduped: list[str] = []
        seen: set[str] = set()
        for item in variants:
            key = item.lower()
            if key in seen:
                continue
            seen.add(key)
            deduped.append(item)
        return deduped[:4]

    async def run_targeted_question(
        self, question: UnresolvedQuestion
    ) -> ResearchRunResult:
        metadata = await self._question_metadata(question)
        subject_name = metadata.get("subject_name")
        if self._is_artifact_research_query(
            question.question_text
        ) or self._is_artifact_subject_name(subject_name):
            title_fragment = self._title_fragment(question.question_text)
            title = f"Research skipped: {title_fragment}"
            normalized_query = self._normalize_search_query(question.question_text)
            question.status = "obsolete"
            await self.session.commit()
            self._log_research_action(
                status="research_artifact_query_blocked",
                summary=f"Research loop skipped an internal artifact question: {title_fragment}",
                query=normalized_query,
                title=title,
                metadata_json={
                    "trigger": "research_loop",
                    "question_id": str(question.id),
                    **metadata,
                },
            )
            return ResearchRunResult(
                started=False,
                reason="research_artifact_query_blocked",
                query=normalized_query,
                title=title,
            )
        title = (
            f"Research on {subject_name}: {question.question_text}"
            if subject_name
            else f"Research on: {question.question_text}"
        )
        result = await self._search_and_ingest(
            query=self._normalize_search_query(question.question_text),
            title=title,
            search_depth="advanced" if question.urgency > 3 else "basic",
            metadata_json={
                "content_type": "text/plain",
                "trigger": "research_loop",
                "question_id": str(question.id),
                "research_question": question.question_text,
                **metadata,
            },
            process_after_ingest=True,
        )
        if result.started and result.evidence_id is not None:
            question.status = "investigating"
            question.originating_evidence_id = result.evidence_id
            await self.session.commit()
            await self._maybe_resolve_question(question, result.evidence_id)
        elif result.reason == "research_artifact_query_blocked":
            question.status = "obsolete"
            await self.session.commit()
        return result

    async def _maybe_resolve_question(
        self,
        question: UnresolvedQuestion,
        evidence_id: UUID,
    ) -> bool:
        source_item = (
            await self.session.execute(
                select(SourceItem).where(SourceItem.raw_evidence_id == evidence_id)
            )
        ).scalar_one_or_none()
        if source_item is None:
            return False

        evidence_text = "\n\n".join(
            part.strip()
            for part in (source_item.summary or "", source_item.extracted_text or "")
            if part and part.strip()
        )[:12000]
        if len(evidence_text) < 80:
            return False

        try:
            assessment = await call_llm_json(
                system_prompt=(
                    "Assess whether the supplied processed source directly answers the specific research question. "
                    "Related context, a plausible inference, or a lead for more research is not enough. Set answered=true "
                    "only when the source contains a concrete answer that can be summarized without adding unstated facts. "
                    "Keep the summary concise and state any material residual uncertainty."
                ),
                user_prompt=json.dumps(
                    {
                        "question": question.question_text,
                        "processed_source": evidence_text,
                    },
                    default=str,
                ),
                schema=QUESTION_RESOLUTION_SCHEMA,
                timeout_seconds=30,
            )
        except Exception as exc:
            self._log_research_action(
                status="resolution_assessment_deferred",
                summary=(
                    f"Resolution assessment deferred for question {question.id}: "
                    f"{compact_exception_message(exc)}"
                ),
                query=question.question_text,
                title=f"Question resolution: {self._title_fragment(question.question_text)}",
                metadata_json={
                    "question_id": str(question.id),
                    "evidence_id": str(evidence_id),
                },
            )
            return False

        if not assessment.get("answered"):
            self._log_research_action(
                status="question_still_open",
                summary=(
                    f"Processed evidence did not fully answer: {question.question_text}"
                ),
                query=question.question_text,
                title=f"Question still open: {self._title_fragment(question.question_text)}",
                metadata_json={
                    "question_id": str(question.id),
                    "evidence_id": str(evidence_id),
                    "assessment_summary": str(assessment.get("summary") or ""),
                    "remaining_uncertainty": assessment.get("remaining_uncertainty"),
                },
            )
            return False

        existing = (
            await self.session.execute(
                select(Resolution).where(
                    Resolution.unresolved_question_id == question.id
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            summary = str(assessment.get("summary") or "").strip()
            uncertainty = str(assessment.get("remaining_uncertainty") or "").strip()
            if uncertainty:
                summary = f"{summary} Remaining uncertainty: {uncertainty}".strip()
            self.session.add(
                Resolution(
                    unresolved_question_id=question.id,
                    resolving_evidence_ids=[evidence_id],
                    summary=summary
                    or "Processed evidence directly answered the research question.",
                )
            )
        question.status = "answered"
        await self.session.commit()
        self._log_research_action(
            status="question_resolved",
            summary=f"Research evidence answered: {question.question_text}",
            query=question.question_text,
            title=f"Question resolved: {self._title_fragment(question.question_text)}",
            metadata_json={
                "question_id": str(question.id),
                "evidence_id": str(evidence_id),
            },
        )
        return True

    async def _question_metadata(self, question: UnresolvedQuestion) -> dict[str, str]:
        coverage = await self.session.get(CoverageMap, question.coverage_map_id)
        if coverage is None:
            return {}
        metadata = {
            "subject_type": coverage.subject_type,
            "subject_id": str(coverage.subject_id),
        }
        subject_name = await self._subject_label(
            coverage.subject_type, coverage.subject_id
        )
        if subject_name:
            metadata["subject_name"] = subject_name
        return metadata

    async def _subject_label(self, subject_type: str, subject_id) -> str | None:
        if subject_type == "position":
            position = await self.session.get(Position, subject_id)
            if position is None:
                return None
            security = await self.session.get(Security, position.security_id)
            if security is None:
                return None
            entity = await self.session.get(Entity, security.entity_id)
            return (
                security.ticker
                if entity is None
                else f"{security.ticker} · {entity.name}"
            )
        if subject_type == "entity":
            entity = await self.session.get(Entity, subject_id)
            return None if entity is None else entity.name
        if subject_type == "theme":
            theme = await self.session.get(Theme, subject_id)
            return None if theme is None else theme.name
        return None

    async def run_ad_hoc_request(
        self,
        *,
        query: str,
        title: str,
        source_item_type: str = "web_research",
        metadata_json: dict | None = None,
        process_after_ingest: bool = True,
    ) -> ResearchRunResult:
        return await self._search_and_ingest(
            query=self._normalize_search_query(query),
            title=title,
            source_item_type=source_item_type,
            metadata_json={
                "content_type": "text/plain",
                **(metadata_json or {}),
            },
            process_after_ingest=process_after_ingest,
        )

    async def _discover(
        self,
        *,
        query: str,
        title: str,
        search_depth: str,
        include_raw_content: bool,
        metadata: dict[str, Any],
        timeout_seconds: float,
        provider_order: list[str] | None = None,
        query_variants: list[str] | None = None,
        initial_fallback_reason: str | None = None,
    ) -> ResearchSearchResult:
        research = RuntimeSettingsStore.load().research
        configured = configured_research_providers(research)
        if provider_order is not None:
            configured = [item for item in provider_order if item in configured]
        if not configured:
            self._append_usage_log(
                {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "query": query,
                    "title": title,
                    "search_depth": search_depth,
                    "status": "not_configured",
                    "metadata": metadata,
                }
            )
            return ResearchSearchResult(
                searched=False,
                reason="research_provider_not_configured",
                query=query,
            )

        query_variants = query_variants or self._search_query_variants(query)
        variants_tried: list[str] = []
        provider_attempts: list[dict[str, Any]] = []
        last_response: ProviderSearchResponse | None = None
        async with httpx.AsyncClient(
            timeout=timeout_seconds, trust_env=False
        ) as client:
            fallback_reason = initial_fallback_reason
            for provider in configured:
                for candidate_query in query_variants:
                    if candidate_query not in variants_tried:
                        variants_tried.append(candidate_query)
                    if provider == "tavily":
                        estimated_credits = 2 if search_depth == "advanced" else 1
                        budget = research.tavily_monthly_credit_budget
                        if (
                            budget is not None
                            and self._tavily_credits_used_this_month()
                            + estimated_credits
                            > budget
                        ):
                            response = ProviderSearchResponse(
                                provider="tavily",
                                status="research_provider_budget_exhausted",
                                query=candidate_query,
                                estimated_credits=0,
                            )
                        else:
                            response = await search_research_provider(
                                provider=provider,
                                client=client,
                                query=candidate_query,
                                search_depth=search_depth,
                                include_raw_content=include_raw_content,
                                tavily_api_key=research.api_key,
                            )
                    else:
                        response = await search_research_provider(
                            provider=provider,
                            client=client,
                            query=candidate_query,
                            search_depth=search_depth,
                            include_raw_content=include_raw_content,
                            searxng_base_url=research.searxng_base_url,
                        )

                    last_response = response
                    attempt = {
                        "provider": provider,
                        "query": candidate_query,
                        "status": response.status,
                        "result_count": len(response.results),
                        "request_id": response.request_id,
                        "estimated_credits": response.estimated_credits,
                        "fallback_reason": fallback_reason,
                    }
                    provider_attempts.append(attempt)
                    self._append_usage_log(
                        {
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                            "provider": provider,
                            "query": candidate_query,
                            "input_query": query,
                            "title": title,
                            "search_depth": search_depth,
                            "status": response.status,
                            "result_count": len(response.results),
                            "top_url": (
                                response.results[0].get("url")
                                if response.results
                                else None
                            ),
                            "request_id": response.request_id,
                            "estimated_credits": response.estimated_credits,
                            "fallback_reason": fallback_reason,
                            "error": response.error,
                            "metadata": metadata,
                        }
                    )
                    if response.status == "ok" and response.results:
                        observation_ids = await self._record_discovery_results(
                            provider=provider,
                            request_id=response.request_id,
                            input_query=query,
                            effective_query=candidate_query,
                            title=title,
                            results=response.results,
                            metadata=metadata,
                        )
                        return ResearchSearchResult(
                            searched=True,
                            reason="ok",
                            query=candidate_query,
                            results=response.results,
                            request_id=response.request_id,
                            provider=provider,
                            provider_attempts=provider_attempts,
                            variants_tried=variants_tried,
                            observation_ids_by_url=observation_ids,
                        )
                    if response.status != "no_result":
                        break
                fallback_reason = last_response.status if last_response else "no_result"

        return ResearchSearchResult(
            searched=True,
            reason=(last_response.status if last_response else "no_result"),
            query=(last_response.query if last_response else query),
            request_id=(last_response.request_id if last_response else None),
            provider=(last_response.provider if last_response else None),
            provider_attempts=provider_attempts,
            variants_tried=variants_tried,
        )

    async def search(
        self,
        *,
        query: str,
        title: str,
        search_depth: str = "basic",
        include_raw_content: bool = False,
        metadata_json: dict | None = None,
        timeout_seconds: float = 20.0,
    ) -> ResearchSearchResult:
        """Run provider search without ingesting, using the same connector boundary.

        This is for operational lookups such as calendar/date resolution where
        the app needs snippets first and should decide whether to persist a
        result separately.
        """
        metadata = dict(metadata_json or {})
        query = self._normalize_search_query(query)
        title = self._clean_research_title(
            title=title, query=query, metadata_json=metadata
        )
        subject_name = metadata.get("subject_name")

        if not query:
            self._append_usage_log(
                {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "query": query,
                    "title": title,
                    "search_depth": search_depth,
                    "status": "empty_search_query",
                    "metadata": metadata,
                }
            )
            return ResearchSearchResult(
                searched=False, reason="empty_search_query", query=query
            )

        if self._is_artifact_research_query(query) or self._is_artifact_subject_name(
            subject_name
        ):
            self._append_usage_log(
                {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "query": query,
                    "title": title,
                    "search_depth": search_depth,
                    "status": "research_artifact_query_blocked",
                    "metadata": metadata,
                }
            )
            return ResearchSearchResult(
                searched=False,
                reason="research_artifact_query_blocked",
                query=query,
            )

        result = await self._discover(
            query=query,
            title=title,
            search_depth=search_depth,
            include_raw_content=include_raw_content,
            metadata=metadata,
            timeout_seconds=timeout_seconds,
        )
        if self.session is not None:
            await self.session.commit()
        return result

    async def _find_recent_duplicate_research(
        self,
        *,
        title: str,
        query: str,
        url: str | None = None,
    ) -> RawEvidence | None:
        title_key = self._comparison_key(title)
        query_key = self._comparison_key(query)
        url_key = (url or "").strip()
        cutoff = datetime.now(timezone.utc) - timedelta(hours=12)
        recent = (
            (
                await self.session.execute(
                    select(RawEvidence)
                    .where(RawEvidence.created_at >= cutoff)
                    .order_by(RawEvidence.created_at.desc())
                    .limit(250)
                )
            )
            .scalars()
            .all()
        )

        for evidence in recent:
            metadata = evidence.metadata_json or {}
            existing_title_key = self._comparison_key(evidence.title)
            existing_query_key = self._comparison_key(
                metadata.get("normalized_query")
                or metadata.get("effective_query")
                or metadata.get("query")
            )
            if title_key and existing_title_key == title_key:
                return evidence
            if query_key and existing_query_key == query_key:
                return evidence
            if (
                url_key
                and evidence.url == url_key
                and (
                    bool(title_key and existing_title_key == title_key)
                    or bool(query_key and existing_query_key == query_key)
                )
            ):
                return evidence
        return None

    async def _search_and_ingest(
        self,
        *,
        query: str,
        title: str,
        search_depth: str = "advanced",
        source_item_type: str = "web_research",
        metadata_json: dict | None = None,
        process_after_ingest: bool,
    ) -> ResearchRunResult:
        query = self._normalize_search_query(query)
        title = self._clean_research_title(
            title=title, query=query, metadata_json=metadata_json
        )
        subject_name = (metadata_json or {}).get("subject_name")
        if not query:
            self._log_research_action(
                status="empty_research_query",
                summary=f"External research skipped for {title}. The normalized query was empty.",
                query=query,
                title=title,
                metadata_json=metadata_json,
            )
            return ResearchRunResult(
                started=False,
                reason="empty_research_query",
                query=query,
                title=title,
            )

        if self._is_artifact_research_query(query) or self._is_artifact_subject_name(
            subject_name
        ):
            self._log_research_action(
                status="research_artifact_query_blocked",
                summary=f"External research skipped for {title}. The query points at an internal research artifact.",
                query=query,
                title=title,
                metadata_json=metadata_json,
            )
            self._append_usage_log(
                {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "query": query,
                    "title": title,
                    "status": "research_artifact_query_blocked",
                    "metadata": metadata_json or {},
                }
            )
            return ResearchRunResult(
                started=False,
                reason="research_artifact_query_blocked",
                query=query,
                title=title,
            )

        duplicate = await self._find_recent_duplicate_research(title=title, query=query)
        if duplicate is not None:
            self._log_research_action(
                status="duplicate_recent_research",
                summary=f"External research skipped for {title}. A recent matching evidence item already exists.",
                query=query,
                title=title,
                metadata_json={
                    **(metadata_json or {}),
                    "duplicate_evidence_id": str(duplicate.id),
                    "duplicate_url": duplicate.url,
                },
            )
            self._append_usage_log(
                {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "query": query,
                    "title": title,
                    "status": "duplicate_recent_research",
                    "duplicate_evidence_id": str(duplicate.id),
                    "duplicate_url": duplicate.url,
                    "metadata": metadata_json or {},
                }
            )
            return ResearchRunResult(
                started=False,
                reason="duplicate_recent_research",
                evidence_id=duplicate.id,
                query=query,
                title=title,
            )

        research = RuntimeSettingsStore.load().research
        providers = configured_research_providers(research)
        if not providers:
            self._log_research_action(
                status="not_configured",
                summary=f"External research blocked for {title}. No discovery provider is configured.",
                query=query,
                title=title,
                metadata_json=metadata_json,
            )
            return ResearchRunResult(
                started=False,
                reason="research_provider_not_configured",
                query=query,
                title=title,
            )

        last_reason = "no_result"
        fallback_reason: str | None = None
        provider_attempts: list[dict[str, Any]] = []
        last_noneligible_result: ResearchRunResult | None = None
        query_variants = self._search_query_variants(query)
        for provider in providers:
            variant_offset = 0
            while variant_offset < len(query_variants):
                discovery = await self._discover(
                    query=query,
                    title=title,
                    search_depth=search_depth,
                    include_raw_content=True,
                    metadata=metadata_json or {},
                    timeout_seconds=30.0,
                    provider_order=[provider],
                    query_variants=query_variants[variant_offset:],
                    initial_fallback_reason=fallback_reason,
                )
                provider_attempts.extend(discovery.provider_attempts)
                variant_offset += max(len(discovery.variants_tried), 1)
                last_reason = discovery.reason
                if discovery.reason != "ok" or not discovery.results:
                    break

                for rank, candidate in enumerate(discovery.results[:5], start=1):
                    source_url = str(candidate.get("url") or "").strip()
                    observation_id = discovery.observation_ids_by_url.get(source_url)
                    duplicate = await self._find_recent_duplicate_research(
                        title=title,
                        query=query,
                        url=source_url,
                    )
                    if duplicate is not None:
                        await self._update_discovery_outcome(
                            observation_id,
                            outcome="duplicate_evidence",
                            evidence_id=duplicate.id,
                        )
                        await self.session.commit()
                        self._log_research_action(
                            status="duplicate_recent_research",
                            summary=f"External research skipped for {title}. The discovered source was ingested recently.",
                            query=discovery.query,
                            title=title,
                            metadata_json={
                                **(metadata_json or {}),
                                "discovery_provider": provider,
                                "duplicate_evidence_id": str(duplicate.id),
                                "duplicate_url": duplicate.url,
                            },
                        )
                        return ResearchRunResult(
                            started=False,
                            reason="duplicate_recent_research",
                            evidence_id=duplicate.id,
                            query=discovery.query,
                            title=title,
                        )

                    content_origin = "direct_page"
                    fetch_error: str | None = None
                    document_public_time = None
                    try:
                        document = await self.ingestion.fetch_url_document(source_url)
                        content = document.content
                        canonical_source_url = document.canonical_url
                        document_public_time = document.public_time
                    except (UnsafeUrlError, UrlFetchNetworkError, ValueError) as exc:
                        fetch_error = f"{type(exc).__name__}: {exc}"
                        raw_content = str(candidate.get("raw_content") or "").strip()
                        if provider != "tavily" or not raw_content:
                            await self._update_discovery_outcome(
                                observation_id,
                                outcome="fetch_failed",
                                error=fetch_error,
                            )
                            continue
                        content_origin = "provider_raw_content"
                        content = raw_content
                        canonical_source_url = source_url

                    source_context = await self.source_learning.get_or_create_source_for_url(
                        url=source_url,
                        title=candidate.get("title") or title,
                        preferred_type=source_item_type,
                        description=f"Auto-discovered via {RESEARCH_PROVIDER_CAPABILITIES[provider].label}.",
                    )
                    evidence_metadata = {
                        "content_type": (
                            "text/html"
                            if content_origin == "direct_page"
                            else "text/plain"
                        ),
                        **(metadata_json or {}),
                        "normalized_query": query,
                        "effective_query": discovery.query,
                        "search_title": title,
                        "discovery_provider": provider,
                        "discovery_request_id": discovery.request_id,
                        "discovery_result_rank": rank,
                        "content_origin": content_origin,
                        "canonical_source_url": canonical_source_url,
                        "publication_time_source": (
                            "document_metadata" if document_public_time else None
                        ),
                        "direct_fetch_error": fetch_error,
                        "provider_attempts": provider_attempts,
                    }
                    evidence = await self.ingestion.ingest_text(
                        RawEvidenceCreate(
                            title=title,
                            source_id=source_context.source.id,
                            source_item_type=source_context.inferred_type,
                            url=source_url,
                            public_time=document_public_time,
                            metadata_json=evidence_metadata,
                            content=content[:20000],
                        ),
                        process_now=False,
                    )
                    await self._update_discovery_outcome(
                        observation_id,
                        outcome="ingested_evidence",
                        evidence_id=evidence.id,
                        error=fetch_error,
                    )

                    loop_detail = None
                    processing_error: str | None = None
                    if process_after_ingest:
                        try:
                            loop_detail = await ExtractionWorker(
                                self.session
                            ).process_evidence(evidence.id)
                        except Exception as exc:
                            processing_error = str(exc)

                    relevance_status = str(
                        ((loop_detail or {}).get("relevance_assessment") or {}).get(
                            "status"
                        )
                        or ""
                    )
                    if processing_error:
                        status = "processed_with_errors"
                    elif bool((loop_detail or {}).get("deferred")):
                        status = "extraction_deferred"
                        await self._update_discovery_outcome(
                            observation_id,
                            outcome=status,
                            evidence_id=evidence.id,
                            error=None,
                        )
                    elif bool((loop_detail or {}).get("quarantined")):
                        status = (
                            "rejected_irrelevant"
                            if relevance_status == "irrelevant"
                            else "quarantined_uncertain"
                        )
                        await self._update_discovery_outcome(
                            observation_id,
                            outcome=status,
                            evidence_id=evidence.id,
                            error=None,
                        )
                    else:
                        status = "ok"
                    self._append_usage_log(
                        {
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                            "provider": provider,
                            "query": discovery.query,
                            "input_query": query,
                            "title": title,
                            "search_depth": search_depth,
                            "status": status,
                            "result_count": len(discovery.results),
                            "top_url": source_url,
                            "request_id": discovery.request_id,
                            "metadata": evidence_metadata,
                            "evidence_id": str(evidence.id),
                            "processed": process_after_ingest,
                            "processing_error": processing_error,
                        }
                    )
                    self._log_research_action(
                        status=status,
                        summary=(
                            f"External research ingested from the source page for {title}"
                            if content_origin == "direct_page"
                            else f"External research ingested from Tavily's source extraction for {title}"
                        ),
                        query=discovery.query,
                        title=title,
                        metadata_json={
                            **evidence_metadata,
                            "evidence_id": str(evidence.id),
                            "source_url": source_url,
                            "processed": process_after_ingest,
                            "processing_error": processing_error,
                        },
                    )
                    await self.session.commit()
                    run_result = ResearchRunResult(
                        started=status == "ok",
                        reason=status,
                        evidence_id=evidence.id,
                        processed=(
                            process_after_ingest
                            and processing_error is None
                            and not bool((loop_detail or {}).get("deferred"))
                        ),
                        loop_detail=loop_detail,
                        query=discovery.query,
                        title=title,
                    )
                    if status in {
                        "rejected_irrelevant",
                        "quarantined_uncertain",
                    }:
                        last_noneligible_result = run_result
                        last_reason = status
                        continue
                    return run_result

                last_reason = "no_fetchable_source"
            fallback_reason = last_reason

        if last_noneligible_result is not None:
            return last_noneligible_result

        self._log_research_action(
            status=last_reason,
            summary=f"External research found no fetchable, attributable source for {title}.",
            query=query,
            title=title,
            metadata_json=metadata_json,
        )
        if self.session is not None:
            await self.session.commit()
        return ResearchRunResult(
            started=False,
            reason=last_reason,
            query=query,
            title=title,
        )

    @staticmethod
    async def verify_research_readiness() -> tuple[bool, str]:
        research = RuntimeSettingsStore.load().research
        providers = configured_research_providers(research)
        if not providers:
            return False, "Configure a SearXNG endpoint or Tavily API key."

        failures: list[str] = []
        async with httpx.AsyncClient(timeout=5.0, trust_env=False) as client:
            for provider in providers:
                try:
                    if provider == "searxng":
                        response = await client.get(
                            f"{research.searxng_base_url.rstrip('/')}/config"
                        )
                        response.raise_for_status()
                        return (
                            True,
                            "Research connector (SearXNG) is ready; Tavily remains fallback-only.",
                        )

                    response = await client.get(
                        "https://api.tavily.com/usage",
                        headers={"Authorization": f"Bearer {research.api_key}"},
                    )
                    if response.status_code == 200:
                        prefix = "SearXNG is unavailable; " if failures else ""
                        return True, prefix + "research fallback (Tavily) is ready."
                    if response.status_code == 429:
                        return (
                            True,
                            "Tavily fallback is configured; its usage check is temporarily rate-limited.",
                        )
                    failures.append(f"Tavily returned HTTP {response.status_code}")
                except Exception as exc:
                    failures.append(
                        f"{RESEARCH_PROVIDER_CAPABILITIES[provider].label}: {type(exc).__name__}"
                    )
        return False, "Research connection failed: " + "; ".join(failures)
