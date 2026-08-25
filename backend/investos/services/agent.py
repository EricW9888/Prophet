from __future__ import annotations

import asyncio
import json
import re
from collections import OrderedDict
from datetime import UTC, datetime, timedelta
from inspect import isawaitable
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import httpx
from sqlalchemy import desc, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from investos.config import settings
from investos.core.llm import (
    available_llm_json_recovery_providers,
    call_llm_json,
    call_llm_tools,
    compact_exception_message,
)
from investos.core.prompting import (
    compact_context_text,
    compact_packet_context,
    compact_reasoning_result,
    estimate_tokens_from_payload,
)
from investos.core.storage import LocalStorage
from investos.models.conclusion import ConclusionState
from investos.models.coverage import UnresolvedQuestion
from investos.models.decision import DecisionJournal
from investos.models.entity import Entity, Security
from investos.models.evidence import RawEvidence, SourceItem
from investos.models.lesson import Lesson
from investos.models.portfolio import Position
from investos.models.profile import Profile
from investos.models.shadow import ExperimentResult, ShadowExperiment
from investos.models.source import Source
from investos.models.theme import Theme
from investos.schemas.agent import (
    AgentActionResponse,
    AgentContextCandidateResponse,
    AgentConversationEntryResponse,
    AgentConversationHistoryResponse,
    AgentConversationListResponse,
    AgentConversationSummaryResponse,
    AgentResolveResponse,
    AgentTurnRequest,
    AgentTurnResponse,
)
from investos.schemas.decision import DecisionJournalCreate
from investos.schemas.evidence import RawEvidenceCreate
from investos.schemas.shadow import ShadowExperimentCreate
from investos.schemas.verification import VerificationRequest
from investos.services.agent_action_log import AgentActionLogService
from investos.services.artifact_hygiene import is_internal_artifact_text
from investos.services.corroboration import CorroborationService
from investos.services.decision import DecisionService
from investos.services.ingestion import IngestionService
from investos.services.operating_state import OperatingStateService
from investos.services.portfolio_lookahead import PortfolioLookaheadService
from investos.services.pruning import PruningService
from investos.services.reasoning import ReasoningService
from investos.services.research import ResearchService
from investos.services.retrieval import RetrievalService
from investos.services.review import ReviewService
from investos.services.risk import RiskService
from investos.services.runtime_settings import RuntimeSettingsStore
from investos.services.shadow import ShadowService
from investos.services.subject_alias import SubjectAliasService
from investos.services.verification import VerificationService

CONVERSATION_SOURCE_NAME = "Prophet Agent Conversation"
PORTFOLIO_SUBJECT_ID = uuid5(NAMESPACE_URL, "investos://portfolio/root")
PORTFOLIO_SUBJECT_NAME = "Portfolio"
INTENT_CACHE_MAX_SIZE = 256
OPERATING_INTENT_CACHE_VERSION = "v7"
OPERATING_RESPONSE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "assistant_message": {"type": "string"},
    },
    "required": ["assistant_message"],
}
LOW_INFORMATION_RESEARCH_QUERIES = {
    "query",
    "search query",
    "web search",
    "current event",
    "latest news",
    "market news",
    "portfolio",
    "portfolio holdings",
    "what happened",
    "what happened lately",
}
LOW_INFORMATION_RESEARCH_STOPWORDS = {
    "a",
    "an",
    "and",
    "for",
    "from",
    "in",
    "is",
    "it",
    "latest",
    "market",
    "news",
    "of",
    "on",
    "or",
    "query",
    "search",
    "the",
    "to",
    "what",
    "with",
}


def _bounded_runtime_budget(
    value: int | None, *, default: int, upper_bound: int
) -> int:
    try:
        parsed = int(value if value is not None else default)
    except (TypeError, ValueError):
        parsed = default
    return max(0, min(parsed, upper_bound))


def _dynamic_analysis_lens_limit() -> int:
    return _bounded_runtime_budget(
        settings.AGENT_DYNAMIC_ANALYSIS_LENS_LIMIT,
        default=4,
        upper_bound=8,
    )


def _historical_analogy_context_limit() -> int:
    return _bounded_runtime_budget(
        settings.AGENT_HISTORICAL_ANALOGY_CONTEXT_LIMIT,
        default=4,
        upper_bound=8,
    )


AGENT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_portfolio_state",
            "description": "Get the current portfolio holdings, net capital deployed, and overall performance summary.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_performance_attribution",
            "description": "Measure what drove portfolio gains or losses over a requested window using dated prices and settled cash flows, with holding contributions and an aligned benchmark comparison.",
            "parameters": {
                "type": "object",
                "properties": {
                    "days": {
                        "type": "number",
                        "description": "Calendar-day measurement window; defaults to 21 and accepts 1 through 1825.",
                    }
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_entity_overview",
            "description": "Get a detailed research overview for a specific entity or ticker.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ticker": {
                        "type": "string",
                        "description": "The stock ticker (e.g. EXMPL)",
                    },
                },
                "required": ["ticker"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_research_status",
            "description": "Get the status of ongoing background research passes and automation telemetry.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_knowledge_status",
            "description": "Search the active and deprecated knowledge graph for saved facts, claims, or events related to the user's question.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The knowledge search question or terms.",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_trusted_sources",
            "description": "Get the list of configured trusted sources and domains for research.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_lessons",
            "description": "Retrieve user-defined lessons and feedback from past investment outcomes.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_shadow_status",
            "description": "Retrieve active shadow experiments, policies, and counterfactual results.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_review_queue",
            "description": "Get the list of pending findings and research gaps awaiting human review.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_benchmarks",
            "description": "Get performance benchmarks and relative indices data.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_portfolio_lookahead",
            "description": "Find dated portfolio catalysts, active reminders, earnings reports, and events to watch over the next few days or weeks.",
            "parameters": {
                "type": "object",
                "properties": {
                    "days": {
                        "type": "number",
                        "description": "Optional lookahead horizon in days.",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "start_research_pass",
            "description": "Start a new ad-hoc research pass for a specific company or topic.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The research query or topic",
                    },
                    "ticker": {
                        "type": "string",
                        "description": "Optional ticker to anchor on",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "watch_youtube_video",
            "description": "Ingest an individual YouTube video's transcript into the research graph. Prophet uses existing captions first and may use the explicitly enabled local audio-transcription fallback when captions are unavailable. It does not inspect video frames.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "The YouTube video URL"},
                    "title": {
                        "type": "string",
                        "description": "Optional title for the research entry",
                    },
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "discover_gmail_brokers",
            "description": "Scan the user's Gmail INBOX headers to automatically identify potential broker confirmation emails and suggest filters.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]

SUBJECT_HINT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "subject_kind": {
            "type": "string",
            "enum": ["none", "entity", "theme", "portfolio"],
        },
        "ticker": {"type": ["string", "null"]},
        "search_text": {"type": ["string", "null"]},
        "reason": {"type": "string"},
    },
    "required": ["subject_kind", "ticker", "search_text", "reason"],
}

AUTO_RESEARCH_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "should_research": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "required": ["should_research", "reason"],
}

AUTO_RESEARCH_PLAN_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "should_research": {"type": "boolean"},
        "query": {"type": "string"},
        "target_label": {"type": "string"},
        "information_needs": {
            "type": "array",
            "items": {"type": "string"},
        },
        "reason": {"type": "string"},
    },
    "required": [
        "should_research",
        "query",
        "target_label",
        "information_needs",
        "reason",
    ],
}

FRESH_RESEARCH_QUERY_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "query": {"type": "string"},
        "information_needs": {
            "type": "array",
            "items": {"type": "string"},
        },
        "reason": {"type": "string"},
    },
    "required": ["query", "information_needs", "reason"],
}

CONVERSATIONAL_HANDOFF_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "should_handoff": {"type": "boolean"},
        "assistant_message": {"type": ["string", "null"]},
        "reason": {"type": "string"},
    },
    "required": ["should_handoff", "assistant_message", "reason"],
}

TURN_INTENT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "route": {
            "type": "string",
            "enum": ["conversation", "clarify", "continue", "operate", "analyze"],
        },
        "assistant_message": {"type": ["string", "null"]},
        "requires_fresh_research": {"type": "boolean"},
        "freshness_reason": {"type": "string"},
        "reason": {"type": "string"},
    },
    "required": [
        "route",
        "assistant_message",
        "requires_fresh_research",
        "freshness_reason",
        "reason",
    ],
}

AGENT_RESPONSE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "assistant_message": {"type": "string"},
        "decision": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "should_create": {"type": "boolean"},
                "decision_type": {"type": ["string", "null"]},
                "rationale": {"type": ["string", "null"]},
                "expected_catalyst_timeframe": {"type": ["string", "null"]},
                "expected_return": {"type": ["number", "null"]},
            },
            "required": [
                "should_create",
                "decision_type",
                "rationale",
                "expected_catalyst_timeframe",
                "expected_return",
            ],
        },
        "shadow_experiment": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "should_create": {"type": "boolean"},
                "name": {"type": ["string", "null"]},
                "policy_description": {"type": ["string", "null"]},
                "auto_run": {"type": "boolean"},
            },
            "required": ["should_create", "name", "policy_description", "auto_run"],
        },
        "verification": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "should_run": {"type": "boolean"},
                "challenge_text": {"type": ["string", "null"]},
            },
            "required": ["should_run", "challenge_text"],
        },
        "pruning": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "should_run": {"type": "boolean"},
                "reason": {"type": ["string", "null"]},
            },
            "required": ["should_run", "reason"],
        },
        "memory_note": {"type": "string"},
        "rationale_summary": {"type": "string"},
    },
    "required": [
        "assistant_message",
        "decision",
        "shadow_experiment",
        "verification",
        "pruning",
        "memory_note",
        "rationale_summary",
    ],
}

STRATEGIC_PLANNING_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "strategic_goals": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "goal_name": {"type": "string"},
                    "target_ticker": {"type": ["string", "null"]},
                    "action": {
                        "type": "string",
                        "enum": ["research", "prospect", "none"],
                    },
                    "portfolio_connection": {
                        "type": "string",
                        "enum": [
                            "direct_holding",
                            "tracked_name",
                            "portfolio_theme",
                            "macro_portfolio",
                            "broad_context",
                        ],
                    },
                    "why_now": {"type": "string"},
                    "rationale": {"type": "string"},
                },
                "required": [
                    "goal_name",
                    "target_ticker",
                    "action",
                    "portfolio_connection",
                    "why_now",
                    "rationale",
                ],
            },
        }
    },
    "required": ["strategic_goals"],
}


class AgentService:
    def __init__(self, session: AsyncSession):
        self.session = session
        self.ingestion = IngestionService(session)
        self.storage = LocalStorage()
        self._operating_intent_cache: OrderedDict[
            tuple[str, str, str, str, str], str
        ] = OrderedDict()

    @staticmethod
    def _state_updates_allowed(*, auto_execute: bool, has_fresh_research: bool) -> bool:
        """Keep exploratory or ephemeral context outside accepted state."""
        return bool(auto_execute and not has_fresh_research)

    @staticmethod
    def _watchers_allowed(reasoning_result: dict, *, auto_execute: bool) -> list[dict]:
        if not auto_execute:
            return []
        return list(reasoning_result.get("active_watchers") or [])

    @staticmethod
    def _enforce_action_evidence_boundary(
        orchestration: dict, reasoning_result: dict
    ) -> dict:
        if bool((reasoning_result.get("corroboration") or {}).get("can_promote")):
            return orchestration
        decision = orchestration.setdefault("decision", {})
        decision["should_create"] = False
        decision["rationale"] = (
            "Decision creation requires independently corroborated material assertions."
        )
        return orchestration

    async def handle_turn(
        self,
        payload: AgentTurnRequest,
        progress_callback: (
            Callable[[str, str, dict[str, Any] | None], Awaitable[None] | None] | None
        ) = None,
    ) -> AgentTurnResponse:
        session_id = payload.session_id or uuid4()
        await self._emit_progress(
            progress_callback,
            "routing",
            "Classifying the turn intent before attaching portfolio context.",
        )
        turn_intent = await self._classify_turn_intent(payload.message)
        if turn_intent.get("route") == "conversation":
            await self._emit_progress(
                progress_callback,
                "routing",
                "Handling this as a conversational turn.",
            )
            assistant_message = (
                str(turn_intent.get("assistant_message") or "").strip()
                or self._smalltalk_response()
            )
            AgentActionLogService.append(
                source="chat",
                action_type="conversation",
                status="ok",
                summary=assistant_message[:240],
                subject_id=str(PORTFOLIO_SUBJECT_ID),
                subject_type="portfolio",
                subject_name=PORTFOLIO_SUBJECT_NAME,
                session_id=str(session_id),
                metadata={
                    "process_mode": "conversation",
                    "intent_reason": turn_intent.get("reason"),
                },
            )
            return AgentTurnResponse(
                session_id=session_id,
                assistant_message=assistant_message,
                subject_id=PORTFOLIO_SUBJECT_ID,
                subject_type="portfolio",
                subject_name=PORTFOLIO_SUBJECT_NAME,
                resolution_reason=str(
                    turn_intent.get("reason")
                    or "Conversation does not need portfolio context, retrieval, or research."
                ),
                process_mode="conversation",
                reasoning_run_id=None,
                stance=None,
                confidence_band=None,
                thesis_summary=None,
                rationale_summary="The intent router classified this as conversation rather than a portfolio analysis request.",
                actions=[],
                responded_at=datetime.now(UTC),
            )
        if (
            payload.subject_id is None
            and payload.session_id is None
            and turn_intent.get("route") == "clarify"
        ):
            assistant_message = str(turn_intent.get("assistant_message") or "").strip()
            if not assistant_message:
                ambiguous = self._ambiguous_standalone_token(payload.message)
                assistant_message = (
                    self._ambiguous_context_message(ambiguous)
                    if ambiguous
                    else "I need one more anchor before I analyze this. Name the holding, theme, event, or portfolio link you want tested."
                )
            AgentActionLogService.append(
                source="chat",
                action_type="clarification",
                status="ok",
                summary=assistant_message[:240],
                subject_id=str(PORTFOLIO_SUBJECT_ID),
                subject_type="portfolio",
                subject_name=PORTFOLIO_SUBJECT_NAME,
                session_id=str(session_id),
                metadata={
                    "process_mode": "clarification",
                    "intent_reason": turn_intent.get("reason"),
                },
            )
            return AgentTurnResponse(
                session_id=session_id,
                assistant_message=assistant_message,
                subject_id=PORTFOLIO_SUBJECT_ID,
                subject_type="portfolio",
                subject_name=PORTFOLIO_SUBJECT_NAME,
                resolution_reason=str(
                    turn_intent.get("reason")
                    or "The turn needs clarification before analysis."
                ),
                process_mode="clarification",
                reasoning_run_id=None,
                stance=None,
                confidence_band=None,
                thesis_summary=None,
                rationale_summary="The intent router asked for clarification instead of attaching the prompt to an arbitrary subject.",
                actions=[],
                responded_at=datetime.now(UTC),
            )

        await self._emit_progress(
            progress_callback,
            "context",
            "Resolving the best portfolio context for this turn.",
        )
        # Load recent session history early so context resolution can preserve
        # legitimate follow-up continuity instead of falling back to portfolio scope.
        history_resp = await self.conversation_history(
            session_id=session_id,
            subject_id=None,
            subject_type="portfolio",
        )
        recent_turns = history_resp.entries[-8:] if history_resp.entries else []

        resolved = await self.resolve_context(
            message=payload.message,
            subject_id=payload.subject_id,
            subject_type=payload.subject_type,
            session_id=session_id,
            intent_route=str(turn_intent.get("route") or ""),
        )
        await self._emit_progress(
            progress_callback,
            "context",
            f"Using {resolved.subject_name} as the working context.",
            {
                "subject_id": str(resolved.subject_id),
                "subject_type": resolved.subject_type,
                "subject_name": resolved.subject_name,
            },
        )
        ambiguous_token = self._ambiguous_standalone_token(payload.message)
        if (
            ambiguous_token
            and not resolved.candidates
            and resolved.subject_type == "portfolio"
        ):
            assistant_message = self._ambiguous_context_message(ambiguous_token)
            await self._persist_conversation_exchange(
                session_id=session_id,
                user_message=payload.message,
                user_subject_id=resolved.subject_id,
                user_subject_type=resolved.subject_type,
                assistant_message=assistant_message,
                assistant_subject_id=resolved.subject_id,
                assistant_subject_type=resolved.subject_type,
                assistant_metadata={
                    "origin": "agent_chat",
                    "actions": [],
                    "operating_context_answer": False,
                    "process_mode": "clarification",
                    "resolution_reason": "Short standalone token was too ambiguous to attach to a ticker, theme, or previous session subject.",
                    "stance": None,
                    "confidence_band": None,
                    "thesis_summary": None,
                    "rationale_summary": "Prophet did not analyze because the prompt was too ambiguous to attach safely.",
                },
            )
            AgentActionLogService.append(
                source="chat",
                action_type="clarification",
                status="ok",
                summary=assistant_message[:240],
                subject_id=str(resolved.subject_id),
                subject_type=resolved.subject_type,
                subject_name=resolved.subject_name,
                session_id=str(session_id),
                metadata={
                    "process_mode": "clarification",
                    "ambiguous_token": ambiguous_token,
                },
            )
            return AgentTurnResponse(
                session_id=session_id,
                assistant_message=assistant_message,
                subject_id=resolved.subject_id,
                subject_type=resolved.subject_type,
                subject_name=resolved.subject_name,
                resolution_reason="Short standalone token needs clarification before analysis.",
                process_mode="clarification",
                reasoning_run_id=None,
                stance=None,
                confidence_band=None,
                thesis_summary=None,
                rationale_summary="Prophet did not analyze because the prompt was too ambiguous to attach safely.",
                actions=[],
                responded_at=datetime.now(UTC),
            )

        await self._emit_progress(
            progress_callback,
            "routing",
            "Checking whether this is a direct operating-system request.",
        )

        conversational_handoff = None
        if turn_intent.get("route") == "continue":
            conversational_handoff = await self._maybe_conversational_handoff(
                message=payload.message,
                resolved_subject_name=resolved.subject_name,
                resolved_subject_type=resolved.subject_type,
                history=recent_turns,
            )
        if conversational_handoff is not None:
            assistant_message = conversational_handoff
            await self._persist_conversation_exchange(
                session_id=session_id,
                user_message=payload.message,
                user_subject_id=resolved.subject_id,
                user_subject_type=resolved.subject_type,
                assistant_message=assistant_message,
                assistant_subject_id=resolved.subject_id,
                assistant_subject_type=resolved.subject_type,
                assistant_metadata={
                    "origin": "agent_chat",
                    "actions": [],
                    "operating_context_answer": False,
                    "process_mode": "conversation_handoff",
                    "resolution_reason": resolved.resolution_reason,
                    "stance": None,
                    "confidence_band": None,
                    "thesis_summary": None,
                    "rationale_summary": "Handled as conversational continuity rather than a portfolio evidence update.",
                },
            )
            AgentActionLogService.append(
                source="chat",
                action_type="conversation_handoff",
                status="ok",
                summary=assistant_message[:240],
                subject_id=str(resolved.subject_id),
                subject_type=resolved.subject_type,
                subject_name=resolved.subject_name,
                session_id=str(session_id),
                metadata={"process_mode": "conversation_handoff"},
            )
            return AgentTurnResponse(
                session_id=session_id,
                assistant_message=assistant_message,
                subject_id=resolved.subject_id,
                subject_type=resolved.subject_type,
                subject_name=resolved.subject_name,
                resolution_reason=resolved.resolution_reason,
                process_mode="conversation_handoff",
                reasoning_run_id=None,
                stance=None,
                confidence_band=None,
                thesis_summary=None,
                rationale_summary="Handled as conversational continuity rather than a portfolio evidence update.",
                actions=[],
                responded_at=datetime.now(UTC),
            )

        direct_answer = None
        if turn_intent.get("route") == "operate":
            direct_answer = await self._maybe_operating_context_answer(
                session_id=session_id,
                message=payload.message,
                resolved_subject_id=resolved.subject_id,
                resolved_subject_type=resolved.subject_type,
                resolved_subject_name=resolved.subject_name,
                allow_actions=payload.auto_execute,
                history=recent_turns,
            )
        else:
            await self._emit_progress(
                progress_callback,
                "routing",
                "Using the evidence analyst path for this reasoning turn.",
            )
        if direct_answer is not None:
            await self._emit_progress(
                progress_callback,
                "routing",
                "Handled as an operating-system turn without deeper evidence reasoning.",
                {
                    "operating_query_type": str(
                        direct_answer.get("operating_query_type") or ""
                    )
                },
            )
            assistant_message = self._normalize_assistant_message(
                direct_answer["assistant_message"],
                reasoning_result=None,
            )
            response_subject_id = direct_answer.get("subject_id", resolved.subject_id)
            response_subject_type = direct_answer.get(
                "subject_type", resolved.subject_type
            )
            response_subject_name = direct_answer.get(
                "subject_name", resolved.subject_name
            )
            response_resolution_reason = direct_answer.get(
                "resolution_reason", resolved.resolution_reason
            )
            should_persist = self._should_persist_conversation_exchange(
                user_message=payload.message,
                assistant_message=assistant_message,
                operating_query_type=str(
                    direct_answer.get("operating_query_type") or ""
                ),
                process_mode="operating_context_llm",
                actions=[],
                stance=None,
                confidence_band=None,
                thesis_summary=None,
                reasoning_run_id=None,
            )
            if should_persist:
                await self._emit_progress(
                    progress_callback,
                    "memory",
                    "Saving this turn into durable conversation memory.",
                )
                await self._persist_conversation_exchange(
                    session_id=session_id,
                    user_message=payload.message,
                    user_subject_id=resolved.subject_id,
                    user_subject_type=resolved.subject_type,
                    assistant_message=assistant_message,
                    assistant_subject_id=response_subject_id,
                    assistant_subject_type=response_subject_type,
                    assistant_metadata={
                        "origin": "agent_chat",
                        "actions": [],
                        "operating_context_answer": True,
                        "operating_query_type": direct_answer.get(
                            "operating_query_type"
                        ),
                        "process_mode": "operating_context_llm",
                        "resolution_reason": response_resolution_reason,
                        "stance": None,
                        "confidence_band": None,
                        "thesis_summary": None,
                        "rationale_summary": direct_answer.get("rationale_summary"),
                    },
                )
            AgentActionLogService.append(
                source="chat",
                action_type=str(
                    direct_answer.get("operating_query_type")
                    or "operating_context_answer"
                ),
                status="ok",
                summary=assistant_message[:240],
                subject_id=str(response_subject_id),
                subject_type=response_subject_type,
                subject_name=response_subject_name,
                session_id=str(session_id),
                metadata={
                    "process_mode": "operating_context_llm",
                },
            )
            return AgentTurnResponse(
                session_id=session_id,
                assistant_message=assistant_message,
                subject_id=response_subject_id,
                subject_type=response_subject_type,
                subject_name=response_subject_name,
                resolution_reason=response_resolution_reason,
                process_mode="operating_context_llm",
                reasoning_run_id=None,
                stance=None,
                confidence_band=None,
                thesis_summary=None,
                rationale_summary=direct_answer.get("rationale_summary"),
                actions=[],
                responded_at=datetime.now(UTC),
            )

        retrieval = RetrievalService(self.session)
        await self._emit_progress(
            progress_callback,
            "retrieval",
            "Assembling direct, connected, historical, and contradiction evidence.",
        )
        packet = await retrieval.retrieve_evidence(
            query=payload.message,
            subject_id=resolved.subject_id,
            subject_type=resolved.subject_type,
            max_depth=6,
        )
        packet_context = await retrieval.hydrate_packet(packet)
        conversation_context = self._build_conversation_context(
            recent_turns=recent_turns,
            current_message=payload.message,
            subject_name=resolved.subject_name,
            subject_type=resolved.subject_type,
        )
        if conversation_context:
            packet_context["conversation_context"] = conversation_context
        fresh_research_context = await self._maybe_fresh_research_context(
            message=payload.message,
            subject_name=resolved.subject_name,
            subject_type=resolved.subject_type,
            packet_context=packet_context,
            freshness_requirement=turn_intent,
        )
        if fresh_research_context:
            packet_context["fresh_research_context"] = fresh_research_context
            await self._emit_progress(
                progress_callback,
                "research",
                (
                    "Fresh/current-event language detected, so Prophet checked external research before reasoning."
                    if fresh_research_context.get("status") == "ok"
                    else "Fresh/current-event language detected, but external research did not return usable fresh context."
                ),
                {
                    "required": bool(fresh_research_context.get("required")),
                    "status": str(fresh_research_context.get("status") or ""),
                    "query": str(fresh_research_context.get("query") or ""),
                    "result_count": len(fresh_research_context.get("results") or []),
                },
            )
        await self._emit_progress(
            progress_callback,
            "retrieval",
            "Evidence packet assembled.",
            {
                "direct_evidence_count": packet.direct_evidence_count,
                "connected_evidence_count": packet.connected_evidence_count,
                "historical_evidence_count": packet.historical_evidence_count,
                "contradiction_evidence_count": packet.contradiction_evidence_count,
            },
        )
        await self._emit_progress(
            progress_callback,
            "reasoning",
            "Running the evidence analyst pass.",
        )

        async def on_reasoning_chunk(chunk_text: str):
            display_text = chunk_text.split("\n")[-1].strip()
            if len(display_text) > 80:
                display_text = "..." + display_text[-77:]

            await self._emit_progress(
                progress_callback,
                "thinking",
                f"Analyst: {display_text}",
                detail={"progress_hint": display_text},
            )

        supplemental_context: dict[str, Any] = {}
        if conversation_context:
            supplemental_context["conversation_context"] = conversation_context
        if fresh_research_context:
            supplemental_context["fresh_research_context"] = fresh_research_context

        reasoning_service = ReasoningService(self.session)
        reasoning_run, reasoning_result = await reasoning_service.run_analysis(
            packet.id,
            on_chunk=on_reasoning_chunk,
            include_critique=False,
            supplemental_context=supplemental_context or None,
            allow_state_update=False,
        )
        reasoning_result.pop("state_update_blocked_reason", None)
        subagent_insights: dict[str, str] = {}
        if not reasoning_result.get("is_fallback"):
            subagent_insights = await self._dispatch_dynamic_subagents(
                payload, resolved, packet_context, progress_callback
            )

        # Strategic Deliberation & Refinement Phase
        await self._emit_progress(
            progress_callback,
            "reasoning",
            "Synthesizing the answer.",
        )
        reasoning_result = await self._synthesize_and_refine(
            payload, resolved, packet_context, reasoning_result, subagent_insights
        )

        corroboration = CorroborationService(
            minimum_independent_sources=settings.CORROBORATION_MIN_INDEPENDENT_SOURCES,
            near_duplicate_max_distance=settings.CORROBORATION_NEAR_DUPLICATE_MAX_DISTANCE,
        )
        corroboration.assess_result(reasoning_result, packet_context)
        independent_review = await reasoning_service._create_critique(
            reasoning_run,
            compact_packet_context(packet_context),
            reasoning_result,
        )
        corroboration.apply_independent_review(reasoning_result, independent_review)
        if self._state_updates_allowed(
            auto_execute=payload.auto_execute,
            has_fresh_research=bool(fresh_research_context),
        ):
            state_updated = await reasoning_service._update_conclusion_state(
                packet.subject_id,
                packet.subject_type,
                reasoning_run,
                reasoning_result,
            )
            if state_updated:
                await reasoning_service._update_profile(
                    packet.subject_id,
                    packet.subject_type,
                    reasoning_result,
                )
        elif fresh_research_context:
            reasoning_result["state_update_blocked_reason"] = (
                "fresh_search_context_not_promoted"
            )
        else:
            reasoning_result["state_update_blocked_reason"] = (
                "user_disabled_state_updates"
            )
        reasoning_run.output_text = reasoning_result.get("reasoning")
        reasoning_run.output_tokens = estimate_tokens_from_payload(reasoning_result)
        reasoning_run.structured_output_json = dict(reasoning_result)

        await self._emit_progress(
            progress_callback,
            "reasoning",
            "Deliberation complete.",
            {
                "stance": reasoning_result.get("stance"),
                "confidence_band": reasoning_result.get("confidence_band"),
            },
        )
        auto_research = await self._maybe_auto_research_after_gap(
            payload=payload,
            resolved_subject_id=resolved.subject_id,
            resolved_subject_type=resolved.subject_type,
            resolved_subject_name=resolved.subject_name,
            packet_context=packet_context,
            reasoning_result=reasoning_result,
        )
        if auto_research is not None:
            await self._emit_progress(
                progress_callback,
                "research",
                (
                    "The current packet is too thin for this question, so Prophet is broadening research automatically."
                    if auto_research.get("started")
                    else "Broader research was considered, but could not start cleanly."
                ),
                {
                    "started": bool(auto_research.get("started")),
                    "reason": str(auto_research.get("reason") or ""),
                    "query": str(auto_research.get("query") or ""),
                },
            )
        await self._emit_progress(
            progress_callback,
            "planning",
            "Translating the result into possible operating actions.",
        )
        orchestration = await self._plan_agent_actions(
            payload=payload,
            subject_id=resolved.subject_id,
            subject_type=resolved.subject_type,
            packet_context=packet_context,
            reasoning_result=reasoning_result,
            subagent_insights=subagent_insights,
        )
        orchestration = self._enforce_action_evidence_boundary(
            orchestration, reasoning_result
        )

        actions: list[AgentActionResponse] = []
        if payload.auto_execute:
            await self._emit_progress(
                progress_callback,
                "actions",
                "State updates are enabled, so Prophet can execute justified follow-up actions.",
            )
            actions.extend(
                await self._execute_actions(
                    subject_id=resolved.subject_id,
                    subject_type=resolved.subject_type,
                    payload=payload,
                    orchestration=orchestration,
                )
            )
        else:
            await self._emit_progress(
                progress_callback,
                "actions",
                "State updates are off, so Prophet is previewing actions only.",
            )
            actions.extend(self._preview_actions(orchestration))
        if auto_research and auto_research.get("started"):
            actions.append(
                AgentActionResponse(
                    action_type="research_pass",
                    status="executed",
                    summary=self._auto_research_action_summary(auto_research),
                    resource_id=(
                        UUID(str(auto_research["evidence_id"]))
                        if auto_research.get("evidence_id")
                        else None
                    ),
                    resource_type="raw_evidence",
                )
            )
        elif auto_research and auto_research.get("reason") in {
            "duplicate_recent_research",
            "research_artifact_query_blocked",
            "empty_research_query",
        }:
            actions.append(
                AgentActionResponse(
                    action_type="research_pass",
                    status="skipped",
                    summary=self._auto_research_skip_summary(auto_research),
                    resource_id=(
                        UUID(str(auto_research["evidence_id"]))
                        if auto_research.get("evidence_id")
                        else None
                    ),
                    resource_type=(
                        "raw_evidence" if auto_research.get("evidence_id") else None
                    ),
                )
            )

        assistant_message = self._normalize_assistant_message(
            orchestration["assistant_message"],
            reasoning_result=reasoning_result,
        )
        assistant_message = self._merge_fresh_research_context_into_answer(
            assistant_message,
            fresh_research_context,
        )
        if auto_research and auto_research.get("started"):
            assistant_message = self._merge_research_follow_up_into_answer(
                assistant_message,
                processed=bool(auto_research.get("processed")),
            )
        elif (
            auto_research and auto_research.get("reason") == "duplicate_recent_research"
        ):
            assistant_message = self._merge_research_status_into_answer(
                assistant_message,
                "I found a recent targeted research pass for this same gap, so I reused that instead of spending another external search call.",
            )
        if not payload.auto_execute and any(
            action.status == "planned" for action in actions
        ):
            assistant_message = (
                "Preview only: state updates are off, so Prophet has not changed portfolio state, watchlists, or experiments yet. "
                + assistant_message
            )
        historical_analogy_lenses = self._compact_historical_analogy_lenses(
            packet_context
        )
        should_persist = self._should_persist_conversation_exchange(
            user_message=payload.message,
            assistant_message=assistant_message,
            operating_query_type="",
            process_mode="reasoning_analysis",
            actions=actions,
            stance=reasoning_result.get("stance"),
            confidence_band=reasoning_result.get("confidence_band"),
            thesis_summary=reasoning_result.get("thesis_summary"),
            reasoning_run_id=reasoning_run.id,
        )
        if should_persist:
            await self._emit_progress(
                progress_callback,
                "memory",
                "Saving the completed turn into durable conversation memory.",
            )
            await self._persist_conversation_exchange(
                session_id=session_id,
                user_message=payload.message,
                user_subject_id=resolved.subject_id,
                user_subject_type=resolved.subject_type,
                assistant_message=assistant_message,
                assistant_subject_id=resolved.subject_id,
                assistant_subject_type=resolved.subject_type,
                assistant_metadata={
                    "origin": "agent_chat",
                    "reasoning_run_id": str(reasoning_run.id),
                    "actions": [item.model_dump(mode="json") for item in actions],
                    "process_mode": "reasoning_analysis",
                    "resolution_reason": resolved.resolution_reason,
                    "stance": reasoning_result.get("stance"),
                    "confidence_band": reasoning_result.get("confidence_band"),
                    "thesis_summary": reasoning_result.get("thesis_summary"),
                    "rationale_summary": orchestration.get("rationale_summary"),
                    "source_feedback_influence": reasoning_result.get(
                        "source_feedback_influence"
                    ),
                    "historical_analogy_lenses": historical_analogy_lenses,
                    "subagents": subagent_insights,
                },
            )

        response = AgentTurnResponse(
            session_id=session_id,
            assistant_message=assistant_message,
            subject_id=resolved.subject_id,
            subject_type=resolved.subject_type,
            subject_name=resolved.subject_name,
            resolution_reason=resolved.resolution_reason,
            process_mode="reasoning_analysis",
            reasoning_run_id=reasoning_run.id,
            stance=reasoning_result.get("stance"),
            confidence_band=reasoning_result.get("confidence_band"),
            thesis_summary=reasoning_result.get("thesis_summary"),
            rationale_summary=orchestration.get("rationale_summary"),
            source_feedback_influence=reasoning_result.get("source_feedback_influence"),
            historical_analogy_lenses=historical_analogy_lenses,
            actions=actions,
            subagents=subagent_insights,
            responded_at=datetime.now(UTC),
        )
        AgentActionLogService.append(
            source="chat",
            action_type="reasoning_analysis",
            status="ok",
            summary=assistant_message[:240],
            subject_id=str(resolved.subject_id),
            subject_type=resolved.subject_type,
            subject_name=resolved.subject_name,
            session_id=str(session_id),
            metadata={
                "stance": reasoning_result.get("stance"),
                "confidence_band": reasoning_result.get("confidence_band"),
                "actions": [item.model_dump(mode="json") for item in actions],
                "reasoning_run_id": str(reasoning_run.id),
                "source_feedback_influence": reasoning_result.get(
                    "source_feedback_influence"
                ),
                "historical_analogy_lenses": historical_analogy_lenses,
            },
        )
        # Process active watchers if returned by the agent
        watchers = self._watchers_allowed(
            reasoning_result,
            auto_execute=payload.auto_execute,
        )
        for w_data in watchers:
            try:
                from investos.services.watcher import WatcherService

                deadline = None
                if w_data.get("deadline_hours"):
                    deadline = datetime.now(UTC) + timedelta(
                        hours=float(w_data["deadline_hours"])
                    )

                await WatcherService(self.session).register_watcher(
                    source="chat",
                    source_id=reasoning_run.id,
                    ticker=w_data.get("ticker"),
                    condition_type=w_data["condition_type"],
                    condition_params={"threshold": w_data.get("threshold")},
                    objective=w_data["objective"],
                    adjustment_plan=w_data["adjustment_plan"],
                    deadline=deadline,
                )
            except Exception:
                import logging

                logging.getLogger(__name__).exception(
                    "Failed to register active watcher from reasoning result"
                )

        await self.session.commit()
        return response

    async def _run_subagent(
        self,
        analysis_instruction: str,
        payload: AgentTurnRequest,
        resolved: AgentResolveResponse,
        packet_context: dict,
        progress_callback: (
            Callable[[str, str, dict[str, Any] | None], Awaitable[None] | None] | None
        ) = None,
    ) -> str | None:
        """Run one bounded, independent analysis lens."""
        try:
            from investos.core.llm import call_llm_json

            result = await call_llm_json(
                system_prompt=(
                    "Complete the following bounded analysis task using the supplied query and evidence packet. "
                    "Do not adopt a persona or invent facts. Separate observations, causal inference, and uncertainty. "
                    f"Task: {analysis_instruction}"
                ),
                user_prompt=json.dumps(
                    {
                        "query": payload.message,
                        "target": resolved.subject_name,
                        "evidence_packet": compact_packet_context(
                            packet_context,
                            max_items_per_layer=4,
                            max_chars=260,
                        ),
                        "portfolio_holdings": (
                            packet_context.get("portfolio_context") or {}
                        ).get("top_holdings", []),
                        "historical_analogy_lenses": (
                            packet_context.get("historical_analogy_lenses") or []
                        )[: _historical_analogy_context_limit()],
                    }
                ),
                schema={
                    "type": "object",
                    "properties": {"insight": {"type": "string"}},
                    "required": ["insight"],
                },
                timeout_seconds=5,
            )
            return result.get("insight")
        except Exception as exc:
            import logging

            logging.getLogger(__name__).warning(
                "Subagent skipped after LLM failure: %s",
                self._compact_exception(exc),
            )
            return None

    @staticmethod
    def _analysis_lens_dispatch_prompt() -> str:
        lens_limit = _dynamic_analysis_lens_limit()
        return (
            f"Return up to {lens_limit} independent analysis lenses that would materially improve this investment question. "
            "Each lens must be a precise inspection task derived from the query, target, and portfolio context. "
            "Prefer non-overlapping causal questions, contradiction checks, bottleneck analysis, or portfolio transmission paths. "
            "When historical_analogy_lenses are supplied, use them to form present-day causal-channel checks rather than treating history as a prediction. "
            "The lens cap is an execution and latency budget, not a closed ontology of investor dimensions. "
            "Do not treat examples as complete; add any material missing dimension implied by the evidence, setup, or portfolio exposure. "
            "If more dimensions matter than fit the budget, choose the most decision-relevant and state the concrete unresolved gap in an instruction. "
            "Do not create personas, job titles, seniority, or role-play instructions. "
            "Return a short label and a concrete instruction for each lens."
        )

    @staticmethod
    def _analysis_lens_synthesis_prompt() -> str:
        return (
            "Combine the initial structured analysis with the independent analysis-lens findings. "
            "Resolve contradictions explicitly from the supplied evidence; do not average incompatible claims. "
            "Every material assertion and evidence ID must remain grounded in the supplied evidence_packet. "
            "Keep unsupported possibilities labeled as assumptions or alternative hypotheses, never facts. "
            "Do not treat multiple claims from one source or copied reports as independent corroboration. "
            "Return the same structured reasoning schema with a concise user-safe rationale."
        )

    async def _dispatch_dynamic_subagents(
        self,
        payload: AgentTurnRequest,
        resolved: AgentResolveResponse,
        packet_context: dict,
        progress_callback: (
            Callable[[str, str, dict[str, Any] | None], Awaitable[None] | None] | None
        ) = None,
    ) -> dict[str, str]:
        """Dynamically dispatch subagents based on the context."""
        try:
            from investos.core.llm import call_llm_json

            dispatcher_prompt = self._analysis_lens_dispatch_prompt()
            lens_limit = _dynamic_analysis_lens_limit()
            dispatch_schema = {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "analysis_lenses": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "label": {"type": "string"},
                                "instruction": {"type": "string"},
                            },
                            "required": ["label", "instruction"],
                        },
                        "maxItems": lens_limit,
                    }
                },
                "required": ["analysis_lenses"],
            }
            dispatch_result = await call_llm_json(
                system_prompt=dispatcher_prompt,
                user_prompt=json.dumps(
                    {
                        "query": payload.message,
                        "target": resolved.subject_name,
                        "portfolio_holdings": (
                            packet_context.get("portfolio_context") or {}
                        ).get("top_holdings", []),
                        "historical_analogy_lenses": (
                            packet_context.get("historical_analogy_lenses") or []
                        )[: _historical_analogy_context_limit()],
                    }
                ),
                schema=dispatch_schema,
                timeout_seconds=5,
            )

            analysis_lenses = dispatch_result.get("analysis_lenses") or []
            tasks = []
            labels = []

            async def wrapped_run(label: str, instruction: str):
                await self._emit_progress(
                    progress_callback, "reasoning", f"Analysis lens '{label}' starting."
                )
                res = await self._run_subagent(
                    instruction, payload, resolved, packet_context, progress_callback
                )
                if res:
                    await self._emit_progress(
                        progress_callback,
                        "reasoning",
                        f"Analysis lens '{label}' finished.",
                        {"analysis_lens": label, "analysis_lens_finding": res},
                    )
                return res

            for lens in analysis_lenses[:lens_limit]:
                label = lens.get("label")
                instruction = lens.get("instruction")
                if label and instruction:
                    labels.append(label)
                    tasks.append(wrapped_run(label, instruction))

            results = await asyncio.gather(*tasks, return_exceptions=True)
            insights = {}
            for label, insight in zip(labels, results):
                if isinstance(insight, Exception):
                    import logging

                    logging.getLogger(__name__).warning(
                        "Analysis lens '%s' failed or timed out: %s",
                        label,
                        self._compact_exception(insight),
                    )
                    continue
                if insight:
                    insights[label] = insight
            return insights
        except Exception as exc:
            import logging

            logging.getLogger(__name__).warning(
                "Dynamic subagent dispatch skipped after LLM failure: %s",
                self._compact_exception(exc),
            )
            return {}

    async def _synthesize_and_refine(
        self,
        payload: AgentTurnRequest,
        resolved: AgentResolveResponse,
        packet_context: dict,
        initial_result: dict,
        subagent_insights: dict[str, str],
    ) -> dict:
        """Synthesize the main analysis with sub-agent insights to produce a superior, refined result."""
        if initial_result.get("is_fallback") or not subagent_insights:
            return initial_result
        try:
            from investos.core.llm import call_llm_json
            from investos.services.reasoning import REASONING_SCHEMA

            synthesis_prompt = self._analysis_lens_synthesis_prompt()

            synthesis_input = {
                "user_query": payload.message,
                "initial_analysis": initial_result,
                "analysis_lens_findings": subagent_insights,
                "evidence_packet": compact_packet_context(
                    packet_context,
                    max_items_per_layer=6,
                    max_chars=320,
                ),
                "historical_analogies": packet_context.get("historical_analogies")
                or [],
                "historical_analogy_lenses": packet_context.get(
                    "historical_analogy_lenses"
                )
                or [],
                "portfolio_context": (packet_context.get("portfolio_context") or {}),
            }

            refined = await call_llm_json(
                system_prompt=synthesis_prompt,
                user_prompt=json.dumps(synthesis_input),
                schema=REASONING_SCHEMA,
                timeout_seconds=25,
            )
            refined = ReasoningService(self.session)._bounded_reasoning_result(refined)
            if initial_result.get("source_feedback_influence"):
                refined["source_feedback_influence"] = initial_result[
                    "source_feedback_influence"
                ]
            return refined
        except Exception as exc:
            import logging

            logging.getLogger(__name__).warning(
                "Synthesis skipped after LLM failure: %s",
                self._compact_exception(exc),
            )
            return initial_result

    @staticmethod
    def _compact_historical_analogy_lenses(packet_context: dict) -> list[dict] | None:
        lenses = packet_context.get("historical_analogy_lenses") or []
        compacted: list[dict] = []
        for lens in lenses[:2]:
            if not isinstance(lens, dict):
                continue
            compacted.append(
                {
                    "name": compact_context_text(lens.get("name"), max_chars=140),
                    "period": str(lens.get("period") or ""),
                    "lens_use_policy": compact_context_text(
                        lens.get("lens_use_policy"), max_chars=260
                    ),
                    "current_application_prompt": compact_context_text(
                        lens.get("current_application_prompt"),
                        max_chars=260,
                    ),
                    "what_rhymes": compact_context_text(
                        lens.get("what_rhymes"), max_chars=260
                    ),
                    "dominant_channel_test": compact_context_text(
                        lens.get("dominant_channel_test"),
                        max_chars=260,
                    ),
                    "where_analogy_breaks": compact_context_text(
                        lens.get("where_analogy_breaks"),
                        max_chars=260,
                    ),
                    "portfolio_transmission": compact_context_text(
                        lens.get("portfolio_transmission"),
                        max_chars=260,
                    ),
                    "best_next_check": compact_context_text(
                        lens.get("best_next_check"), max_chars=220
                    ),
                    "investor_questions": [
                        compact_context_text(str(question), max_chars=220)
                        for question in (lens.get("investor_questions") or [])[:4]
                        if str(question).strip()
                    ],
                }
            )
        return compacted or None

    async def _emit_progress(
        self,
        callback: (
            Callable[[str, str, dict[str, Any] | None], Awaitable[None] | None] | None
        ),
        phase: str,
        message: str,
        detail: dict[str, Any] | None = None,
    ) -> None:
        if callback is None:
            return
        result = callback(phase, message, detail)
        if isawaitable(result):
            await result

    async def _classify_turn_intent(self, message: str) -> dict[str, Any]:
        normalized = " ".join((message or "").split())
        if not normalized:
            return {
                "route": "conversation",
                "assistant_message": self._smalltalk_response(),
                "requires_fresh_research": False,
                "freshness_reason": "",
                "reason": "Empty turn.",
            }
        if PortfolioLookaheadService.looks_like_lookahead_request(normalized):
            return {
                "route": "operate",
                "assistant_message": None,
                "requires_fresh_research": False,
                "freshness_reason": "",
                "reason": "Deterministic router matched a portfolio lookahead/calendar request.",
            }
        try:
            return await asyncio.wait_for(
                call_llm_json(
                    system_prompt=(
                        "Classify the user's turn before portfolio context is attached.\n"
                        "Routes:\n"
                        "- conversation: pure greeting, thanks, acknowledgement, or interface chatter with no analysis request.\n"
                        "- clarify: too ambiguous to analyze safely as a standalone turn, such as an unexplained acronym or single token.\n"
                        "- continue: short follow-up that likely depends on the active conversation context.\n"
                        "- operate: asks Prophet to inspect or act on its own system state, such as portfolio snapshot, transactions, settings, trusted sources, knowledge graph/search/storage, lessons, research status, review queue, benchmarks, shadow experiments, Gmail, YouTube, or starting a research pass.\n"
                        "- analyze: asks for investment reasoning, market implications, thesis work, valuation, long-run return, what matters, critique, or what to do about a holding/event.\n"
                        "Set requires_fresh_research when the answer depends on facts that may have changed or an external event/access state, including current listings, availability, prices, filings, deals, earnings, guidance, regulation, or recent developments. "
                        "Do not set it merely because a question is about investing. Put the concrete temporal dependency in freshness_reason, or an empty string when fresh research is unnecessary. "
                        "Prefer analyze for investment judgment. Prefer operate only when the user is asking for operating-system state or an explicit tool action. "
                        "If route is conversation or clarify, include a concise assistant_message; otherwise assistant_message should be null. "
                        "Output JSON only."
                    ),
                    user_prompt=json.dumps({"message": normalized}, ensure_ascii=True),
                    schema=TURN_INTENT_SCHEMA,
                    timeout_seconds=3,
                ),
                timeout=3,
            )
        except Exception as exc:
            import logging

            logging.getLogger(__name__).warning(
                "Turn intent classifier fell back after LLM failure: %s",
                self._compact_exception(exc),
            )
            return self._fallback_turn_intent(normalized)

    def _fallback_turn_intent(self, message: str) -> dict[str, Any]:
        normalized = " ".join((message or "").lower().split())
        freshness = self._fallback_fresh_context_requirement(message)
        if self._is_small_talk_query(normalized):
            return {
                "route": "conversation",
                "assistant_message": self._smalltalk_response(),
                "requires_fresh_research": False,
                "freshness_reason": "",
                "reason": "Fallback classifier saw a low-information conversational turn.",
            }
        token = self._ambiguous_standalone_token(message)
        if token:
            return {
                "route": "clarify",
                "assistant_message": self._ambiguous_context_message(token),
                "requires_fresh_research": False,
                "freshness_reason": "",
                "reason": "Fallback classifier saw an ambiguous standalone token.",
            }
        if self._is_bare_follow_up(message):
            return {
                "route": "continue",
                "assistant_message": None,
                "requires_fresh_research": freshness["required"],
                "freshness_reason": freshness["reason"],
                "reason": "Fallback classifier saw a short context-dependent follow-up.",
            }
        if self._fallback_operating_intent(normalized):
            return {
                "route": "operate",
                "assistant_message": None,
                "requires_fresh_research": freshness["required"],
                "freshness_reason": freshness["reason"],
                "reason": "Fallback classifier saw an operating-system request.",
            }
        return {
            "route": "analyze",
            "assistant_message": None,
            "requires_fresh_research": freshness["required"],
            "freshness_reason": freshness["reason"],
            "reason": "Fallback classifier found a substantive turn.",
        }

    def _fallback_operating_intent(self, normalized_message: str) -> bool:
        if not normalized_message:
            return False
        if PortfolioLookaheadService.looks_like_lookahead_request(normalized_message):
            return True
        operating_terms = {
            "portfolio",
            "positions",
            "holdings",
            "transactions",
            "sources",
            "trusted sources",
            "settings",
            "api key",
            "knowledge",
            "graph",
            "nodes",
            "node",
            "stored",
            "saved",
            "gmail",
            "email",
            "backfill",
            "lessons",
            "review queue",
            "research status",
            "shadow",
            "benchmarks",
        }
        command_terms = {
            "show",
            "list",
            "get",
            "status",
            "did",
            "catch",
            "caught",
            "latest",
            "refresh",
            "sync",
            "backfill",
            "start",
            "run",
            "set",
            "open",
        }
        return any(term in normalized_message for term in operating_terms) and any(
            term in normalized_message for term in command_terms
        )

    def _build_conversation_context(
        self,
        *,
        recent_turns: list[AgentConversationEntryResponse],
        current_message: str,
        subject_name: str,
        subject_type: str,
    ) -> dict[str, Any] | None:
        trimmed: list[dict[str, Any]] = []
        normalized_current = " ".join((current_message or "").lower().split())
        for turn in recent_turns[-6:]:
            content = " ".join((turn.content or "").split())
            if not content:
                continue
            if " ".join(content.lower().split()) == normalized_current:
                continue
            trimmed.append(
                {
                    "role": turn.role,
                    "content": compact_context_text(content, max_chars=420),
                    "process_mode": turn.process_mode,
                    "stance": turn.stance,
                    "confidence_band": turn.confidence_band,
                    "thesis_summary": turn.thesis_summary,
                }
            )
        if not trimmed:
            return None
        return {
            "subject_name": subject_name,
            "subject_type": subject_type,
            "recent_turns": trimmed,
        }

    async def resolve_context(
        self,
        *,
        message: str,
        subject_id: UUID | None,
        subject_type: str | None,
        session_id: UUID | None = None,
        intent_route: str | None = None,
    ) -> AgentResolveResponse:
        if subject_id is not None and subject_type:
            return AgentResolveResponse(
                subject_id=subject_id,
                subject_type=subject_type,
                subject_name=await self._subject_name(subject_id, subject_type),
                resolution_reason="Using the manually pinned context.",
                candidates=[],
            )

        normalized_message = " ".join((message or "").lower().split())
        if self._is_small_talk_query(normalized_message):
            return AgentResolveResponse(
                subject_id=PORTFOLIO_SUBJECT_ID,
                subject_type="portfolio",
                subject_name=PORTFOLIO_SUBJECT_NAME,
                resolution_reason="Conversation does not resolve to an investment subject.",
                candidates=[],
            )

        candidates = await self._subject_candidates(message)
        should_continue_session = intent_route == "continue" or (
            intent_route is None and self._is_bare_follow_up(message)
        )
        if not candidates and session_id is not None and should_continue_session:
            recent_subject = await self._recent_session_subject(session_id)
            if recent_subject is not None:
                recent_subject_id, recent_subject_type, recent_subject_name = (
                    recent_subject
                )
                return AgentResolveResponse(
                    subject_id=recent_subject_id,
                    subject_type=recent_subject_type,
                    subject_name=recent_subject_name,
                    resolution_reason=(
                        f"Continuing the same-session context for {recent_subject_name} because this is a short follow-up."
                    ),
                    candidates=[],
                )
        if self._ambiguous_standalone_token(message) and not candidates:
            hinted: list[AgentContextCandidateResponse] = []
        else:
            hinted = await self._subject_candidates_from_hint(message)
        merged = self._merge_context_candidates(candidates, hinted)
        if not merged:
            if session_id is not None and should_continue_session:
                recent_subject = await self._recent_session_subject(session_id)
                if recent_subject is not None:
                    recent_subject_id, recent_subject_type, recent_subject_name = (
                        recent_subject
                    )
                    return AgentResolveResponse(
                        subject_id=recent_subject_id,
                        subject_type=recent_subject_type,
                        subject_name=recent_subject_name,
                        resolution_reason=(
                            f"Continuing the same-session context for {recent_subject_name} because the new message did not resolve cleanly on its own."
                        ),
                        candidates=[],
                    )
            return AgentResolveResponse(
                subject_id=PORTFOLIO_SUBJECT_ID,
                subject_type="portfolio",
                subject_name=PORTFOLIO_SUBJECT_NAME,
                resolution_reason=(
                    "Using portfolio-wide context because no stronger stored subject match was found."
                ),
                candidates=[],
            )

        best = merged[0]
        return AgentResolveResponse(
            subject_id=best.subject_id,
            subject_type=best.subject_type,
            subject_name=best.subject_name,
            resolution_reason=best.reason,
            candidates=merged[:5],
        )

    async def run_reflection_cycle(self) -> dict[str, str | int]:
        candidate = await self._select_autonomous_candidate()
        if candidate is None:
            return {"status": "idle", "detail": "no_autonomous_candidate", "actions": 0}

        position, security = candidate
        payload = AgentTurnRequest(
            subject_id=security.entity_id,
            subject_type="entity",
            message=(
                f"Autonomous reflection cycle for {security.ticker}. "
                "Review the accepted state, refresh journal memory if needed, and run a useful shadow experiment."
            ),
            auto_execute=True,
        )
        response = await self.handle_turn(
            payload=payload,
        )
        return {
            "status": "ok",
            "detail": f"{security.ticker} actions={len(response.actions)} stance={response.stance}",
            "actions": len(response.actions),
        }

    async def run_strategic_planning_cycle(self) -> dict[str, object]:
        """
        The NMVA (Next Most Valuable Action) engine.
        Synthesizes lessons, portfolio state, and graph gaps to propose autonomous actions.
        """
        # 1. Gather Context
        from investos.services.operating_state import OperatingStateService

        state = OperatingStateService(self.session)

        portfolio = await state.portfolio_state_payload()
        monitor = await state.portfolio_monitor_payload()
        review_queue = await state.review_queue_payload()
        lessons_data = await state.lessons_payload()

        # Pull top themes
        theme_rows = (
            (
                await self.session.execute(
                    select(Theme).order_by(desc(Theme.last_updated_at)).limit(5)
                )
            )
            .scalars()
            .all()
        )
        themes = [t.name for t in theme_rows]
        tracked_tickers = [
            str(item.get("ticker"))
            for item in portfolio.get("top_holdings", [])
            if item.get("ticker")
        ]

        # 2. Call Strategist LLM
        strategist_system_prompt = (
            "Select the next most valuable autonomous research or prospecting move for Prophet from the supplied operating state."
            "\n\nCORE RULES:"
            "\n- Stay portfolio-aware. Every goal must explain a concrete path back to the live book, tracked names, portfolio-level macro exposure, or an active research cluster."
            "\n- Do not drift into broad context unless you can justify why it matters now."
            "\n- If a topic is only ambient world knowledge with no clear portfolio consequence, mark it as broad_context and set action='none'."
            "\n- Prefer specific, high-leverage questions over generic curiosity."
            "\n- Use user lessons and current review pressure to choose what deserves attention next."
            "\n- Use peer_exposures as candidate relationship patterns between tracked names; investigate them when the mechanism and portfolio weight make the shared exposure actionable."
            "\n- Prefer leading, source-backed changes that could alter future fundamentals or expectations before lagging reported results make the change obvious."
            "\n- Do not infer an opportunity from price movement alone; use an unexplained move as a research trigger."
            "\n- Keep the search open-ended. These rules are quality constraints, not a preset list of acceptable investment lenses."
            "\n- Distinguish between a direct holding problem, a tracked-name adjacency, a portfolio theme, and a broad macro issue."
            "\n\nOUTPUT RULES:"
            "\n- Propose at most 2 strategic goals."
            "\n- Keep each field concise and practical."
            "\n- Each goal must include portfolio_connection and why_now."
            "\n- Only set action to research/prospect if the connection is real and timely."
            "\n- If no strong goal exists, return one item with action='none'."
        )
        strategist_user_prompt = json.dumps(
            {
                "portfolio_summary": portfolio,
                "portfolio_monitor": monitor,
                "review_queue": review_queue,
                "user_lessons": lessons_data.get("recent_lessons", []),
                "active_themes": themes,
                "tracked_tickers": tracked_tickers,
            },
            ensure_ascii=True,
        )
        try:
            result = await call_llm_json(
                system_prompt=strategist_system_prompt,
                user_prompt=strategist_user_prompt,
                schema=STRATEGIC_PLANNING_SCHEMA,
                timeout_seconds=12,
            )
        except Exception as e:
            message = str(e).strip() or repr(e)
            return {"status": "error", "detail": f"LLM strategy failed: {message}"}

        goals = result.get("strategic_goals", [])
        actions_taken = []

        # 3. Execute Top Goal
        executable_goals = [
            goal
            for goal in goals
            if goal.get("action") in {"research", "prospect"}
            and goal.get("portfolio_connection")
            in {"direct_holding", "tracked_name", "portfolio_theme", "macro_portfolio"}
        ]
        for goal in executable_goals[
            :1
        ]:  # Keep execution conservative; planning can still surface more than one goal.
            ticker = goal.get("target_ticker")
            action = goal.get("action")

            if ticker and action in {"research", "prospect"}:
                # Seed the research
                from investos.services.research import ResearchService

                res = await ResearchService(self.session).run_ad_hoc_request(
                    query=f"Analysis of {ticker} focusing on {goal['goal_name']}. Rationale: {goal['rationale']}",
                    title=f"Autonomous Discovery: {ticker}",
                    metadata_json={
                        "is_autonomous": True,
                        "review_reason": goal["rationale"],
                        "goal_name": goal["goal_name"],
                        "portfolio_connection": goal["portfolio_connection"],
                        "why_now": goal["why_now"],
                    },
                )
                if res.started:
                    # Update Profile/Position metadata
                    if res.loop_detail and res.loop_detail.get("subject_id"):
                        subject_id = UUID(str(res.loop_detail["subject_id"]))
                        if goal["action"] == "prospect":
                            profile = (
                                await self.session.execute(
                                    select(Profile).where(
                                        Profile.subject_id == subject_id,
                                        Profile.subject_type == "entity",
                                    )
                                )
                            ).scalar_one_or_none()
                            if profile:
                                profile.is_autonomous = True
                                profile.review_status = "pending"
                                profile.review_reason = goal["rationale"]
                                profile.strategist_reasoning = (
                                    f"Agent prospect identified {goal['why_now']}."
                                )
                                profile.source_rationale = goal["why_now"]

                    actions_taken.append(
                        f"Started research on {ticker} "
                        f"({goal['portfolio_connection']}: {goal['why_now']})"
                    )

        await self.session.commit()
        return {
            "status": "ok",
            "detail": (
                f"Planned {len(goals)} goals. "
                + (
                    "; ".join(actions_taken)
                    if actions_taken
                    else "No sufficiently portfolio-linked autonomous action was started."
                )
            ),
            "goals": goals,
        }

    async def conversation_history(
        self,
        session_id: UUID | None,
        subject_id: UUID | None,
        subject_type: str,
        include_artifacts: bool = False,
    ) -> AgentConversationHistoryResponse:
        source = await self._get_or_create_conversation_source()
        evidence_rows = (
            (
                await self.session.execute(
                    select(RawEvidence)
                    .where(RawEvidence.source_id == source.id)
                    .order_by(RawEvidence.created_at.asc())
                )
            )
            .scalars()
            .all()
        )
        entries: list[AgentConversationEntryResponse] = []
        resolved_subject_id = subject_id or PORTFOLIO_SUBJECT_ID
        resolved_subject_type = subject_type
        for evidence in evidence_rows:
            metadata = evidence.metadata_json or {}
            if session_id is not None:
                if metadata.get("session_id") != str(session_id):
                    continue
                metadata_subject_id = self._safe_uuid(metadata.get("subject_id"))
                if metadata_subject_id is not None:
                    resolved_subject_id = metadata_subject_id
                if metadata.get("subject_type"):
                    resolved_subject_type = metadata["subject_type"]
            else:
                if (
                    metadata.get("subject_id") != str(subject_id)
                    or metadata.get("subject_type") != subject_type
                ):
                    continue
            content = await self._load_raw_content(evidence)
            is_artifact = self._is_background_conversation_memory(
                evidence, metadata, content=content
            )
            if is_artifact and not include_artifacts:
                continue
            entries.append(
                AgentConversationEntryResponse(
                    id=evidence.id,
                    role=self._conversation_display_role(
                        metadata, is_artifact=is_artifact
                    ),
                    content=content,
                    created_at=evidence.created_at,
                    message_kind=self._conversation_message_kind(
                        evidence,
                        metadata,
                        content=content,
                        is_artifact=is_artifact,
                    ),
                    is_artifact=is_artifact,
                    origin=metadata.get("origin"),
                    process_mode=metadata.get("process_mode"),
                    resolution_reason=metadata.get("resolution_reason"),
                    reasoning_run_id=(
                        UUID(str(metadata["reasoning_run_id"]))
                        if metadata.get("reasoning_run_id")
                        else None
                    ),
                    stance=metadata.get("stance"),
                    confidence_band=metadata.get("confidence_band"),
                    thesis_summary=metadata.get("thesis_summary"),
                    rationale_summary=metadata.get("rationale_summary"),
                    source_feedback_influence=(
                        metadata.get("source_feedback_influence")
                        if isinstance(metadata.get("source_feedback_influence"), dict)
                        else None
                    ),
                    historical_analogy_lenses=(
                        metadata.get("historical_analogy_lenses")
                        if isinstance(metadata.get("historical_analogy_lenses"), list)
                        else None
                    ),
                    actions=[
                        AgentActionResponse.model_validate(item)
                        for item in metadata.get("actions", [])
                        if isinstance(item, dict)
                    ],
                    subagents=(
                        metadata.get("subagents")
                        if isinstance(metadata.get("subagents"), dict)
                        else None
                    ),
                )
            )
        return AgentConversationHistoryResponse(
            session_id=session_id,
            subject_id=resolved_subject_id,
            subject_type=resolved_subject_type,
            entries=entries[-60:],
        )

    async def list_conversations(self) -> AgentConversationListResponse:
        source = await self._get_or_create_conversation_source()
        evidence_rows = (
            (
                await self.session.execute(
                    select(RawEvidence)
                    .where(RawEvidence.source_id == source.id)
                    .order_by(RawEvidence.created_at.desc())
                )
            )
            .scalars()
            .all()
        )
        grouped: dict[str, dict] = {}
        artifact_counts: dict[str, int] = {}
        for evidence in evidence_rows:
            metadata = evidence.metadata_json or {}
            raw_session_id = metadata.get("session_id")
            if not raw_session_id:
                continue
            session_key = str(raw_session_id)
            session_uuid = self._safe_uuid(session_key)
            if session_uuid is None:
                continue
            content = await self._load_raw_content(evidence)
            if self._is_background_conversation_memory(
                evidence, metadata, content=content
            ):
                artifact_counts[session_key] = artifact_counts.get(session_key, 0) + 1
                continue
            if session_key in grouped:
                continue
            subject_uuid = self._safe_uuid(metadata.get("subject_id"))
            subject_type_value = metadata.get("subject_type")
            title = metadata.get("session_title") or self._session_title_from_message(
                content
            )
            grouped[session_key] = {
                "session_id": session_uuid,
                "title": title,
                "subject_id": subject_uuid,
                "subject_type": subject_type_value,
                "subject_name": (
                    await self._subject_name(subject_uuid, subject_type_value)
                    if subject_uuid is not None and subject_type_value
                    else None
                ),
                "latest_message_preview": content[:140] if content else None,
                "updated_at": evidence.created_at,
            }
        conversations = sorted(
            [
                AgentConversationSummaryResponse(
                    **data,
                    artifact_count=artifact_counts.get(session_key, 0),
                )
                for session_key, data in grouped.items()
            ],
            key=lambda item: item.updated_at,
            reverse=True,
        )
        return AgentConversationListResponse(conversations=conversations)

    async def update_conversation(
        self, session_id: UUID, *, title: str
    ) -> AgentConversationSummaryResponse:
        source = await self._get_or_create_conversation_source()
        clean_title = self._session_title_from_message(title)
        evidence_rows = (
            (
                await self.session.execute(
                    select(RawEvidence)
                    .where(RawEvidence.source_id == source.id)
                    .order_by(RawEvidence.created_at.desc())
                )
            )
            .scalars()
            .all()
        )
        session_rows = [
            evidence
            for evidence in evidence_rows
            if (evidence.metadata_json or {}).get("session_id") == str(session_id)
            and not self._is_background_conversation_memory(
                evidence, evidence.metadata_json or {}
            )
        ]
        if not session_rows:
            raise ValueError("conversation_not_found")
        for evidence in session_rows:
            metadata = dict(evidence.metadata_json or {})
            metadata["session_title"] = clean_title
            metadata["session_title_user_set"] = True
            evidence.metadata_json = metadata
        await self.session.commit()
        updated = await self.list_conversations()
        for conversation in updated.conversations:
            if conversation.session_id == session_id:
                return conversation
        raise ValueError("conversation_not_found")

    async def _plan_agent_actions(
        self,
        *,
        payload: AgentTurnRequest,
        subject_id: UUID,
        subject_type: str,
        packet_context: dict,
        reasoning_result: dict,
        subagent_insights: dict[str, str] | None = None,
    ) -> dict:
        if reasoning_result.get("is_fallback"):
            return self._fast_path_orchestration(payload, reasoning_result)

        security_ticker = await self._subject_ticker(subject_id, subject_type)
        compact_packet = compact_packet_context(
            packet_context, max_items_per_layer=3, max_chars=180
        )
        compact_result = compact_reasoning_result(reasoning_result)

        agent_context = {
            "user_message": payload.message,
            "subject_id": str(subject_id),
            "subject_type": subject_type,
            "subject_ticker": security_ticker,
            "packet_context": compact_packet,
            "reasoning_result": compact_result,
        }
        if subagent_insights:
            agent_context["analysis_lens_findings"] = subagent_insights

        try:
            return await asyncio.wait_for(
                call_llm_json(
                    system_prompt=(
                        "Plan justified operating actions from the evidence-backed analysis and any independent analysis-lens findings. "
                        "Resolve disagreements from the supplied evidence rather than averaging them. "
                        "Surface structural bottlenecks or multi-step causal chains only when the packet supports them. "
                        "When historical analogy lenses are present, action plans should preserve the what-rhymes/what-breaks/current-channel distinction. "
                        "Populate 'rationale_summary' with a concise user-facing rationale summary. Output JSON only."
                    ),
                    user_prompt=json.dumps(
                        agent_context, ensure_ascii=True, default=str
                    ),
                    schema=AGENT_RESPONSE_SCHEMA,
                ),
                timeout=12,
            )
        except TimeoutError:
            return self._timeout_orchestration(reasoning_result)
        except Exception as exc:
            import logging

            logging.getLogger(__name__).warning(
                "Action planning skipped after LLM failure: %s",
                self._compact_exception(exc),
            )
            return self._fast_path_orchestration(payload, reasoning_result)

    def _should_use_fast_path(
        self, packet_context: dict, reasoning_result: dict
    ) -> bool:
        direct = packet_context.get("direct_evidence") or []
        connected = packet_context.get("connected_evidence") or []
        historical = packet_context.get("historical_evidence") or []
        contradiction = packet_context.get("contradiction_evidence") or []
        support_ids = reasoning_result.get("supporting_evidence_ids") or []
        contradiction_ids = reasoning_result.get("contradicting_evidence_ids") or []
        return reasoning_result.get("stance") == "no_view" and (
            not (direct or connected or historical or contradiction)
            or (
                reasoning_result.get("confidence_band") in {"low", "very_low", "high"}
                and not support_ids
                and not contradiction_ids
            )
        )

    def _fast_path_orchestration(
        self, payload: AgentTurnRequest, reasoning_result: dict
    ) -> dict:
        """Only used when the agent LLM times out. Build a readable response from reasoning result."""
        thesis = (reasoning_result.get("thesis_summary") or "").strip()
        reasoning = (reasoning_result.get("reasoning") or "").strip()
        strengthen = [
            str(item).strip()
            for item in (reasoning_result.get("what_would_strengthen") or [])[:3]
            if str(item).strip()
        ]
        falsify = [
            str(item).strip()
            for item in (reasoning_result.get("what_would_falsify") or [])[:2]
            if str(item).strip()
        ]
        sections: list[str] = []
        if thesis and reasoning and thesis != reasoning:
            sections.extend([thesis, reasoning])
        elif thesis:
            sections.append(thesis)
        elif reasoning:
            sections.append(reasoning)
        else:
            query = (payload.message or "this topic").strip()
            sections.append(
                f"Current read: I do not have enough source-backed context to form a high-conviction view on '{query}'."
            )
        if strengthen:
            sections.append(
                "What would strengthen this:\n"
                + "\n".join(f"- {item}" for item in strengthen)
            )
        if falsify:
            sections.append(
                "What would change or break this read:\n"
                + "\n".join(f"- {item}" for item in falsify)
            )
        message = "\n\n".join(sections).strip()

        return {
            "assistant_message": message,
            "decision": {
                "should_create": False,
                "decision_type": None,
                "rationale": None,
                "expected_catalyst_timeframe": None,
                "expected_return": None,
            },
            "shadow_experiment": {
                "should_create": False,
                "name": None,
                "policy_description": None,
                "auto_run": False,
            },
            "verification": {
                "should_run": False,
                "challenge_text": None,
            },
            "pruning": {
                "should_run": False,
                "reason": None,
            },
            "memory_note": "",
            "rationale_summary": "Fast path execution used the available structured reasoning result.",
        }

    def _timeout_orchestration(self, reasoning_result: dict) -> dict:
        thesis = (reasoning_result.get("thesis_summary") or "").strip()
        reasoning = (reasoning_result.get("reasoning") or "").strip()
        message = (
            thesis
            or reasoning
            or (
                "The analysis took too long for this interactive turn. "
                "Try again or check the timeline for background updates."
            )
        )
        return {
            "assistant_message": message,
            "decision": {
                "should_create": False,
                "decision_type": None,
                "rationale": None,
                "expected_catalyst_timeframe": None,
                "expected_return": None,
            },
            "shadow_experiment": {
                "should_create": False,
                "name": None,
                "policy_description": None,
                "auto_run": False,
            },
            "verification": {
                "should_run": False,
                "challenge_text": None,
            },
            "pruning": {
                "should_run": False,
                "reason": None,
            },
            "memory_note": "",
            "rationale_summary": (
                "The evidence analysis completed, but the follow-up action planner timed out; "
                "Prophet returned the analysis and did not create decision, verification, pruning, or shadow actions from the timed-out planner."
            ),
        }

    async def _execute_actions(
        self,
        *,
        subject_id: UUID,
        subject_type: str,
        payload: AgentTurnRequest,
        orchestration: dict,
    ) -> list[AgentActionResponse]:
        actions: list[AgentActionResponse] = []

        decision_plan = orchestration["decision"]
        position_id = await self._resolve_position_id(subject_id, subject_type)
        if (
            decision_plan["should_create"]
            and decision_plan["decision_type"]
            and decision_plan["rationale"]
        ):
            decision = await DecisionService(self.session).create_decision(
                DecisionJournalCreate(
                    position_id=position_id,
                    decision_type=decision_plan["decision_type"],
                    rationale=decision_plan["rationale"],
                    expected_catalyst_timeframe=decision_plan[
                        "expected_catalyst_timeframe"
                    ],
                    expected_return=decision_plan["expected_return"],
                )
            )
            actions.append(
                AgentActionResponse(
                    action_type="decision_journal",
                    status="executed",
                    summary=f"Recorded {decision.decision_type} journal entry.",
                    resource_id=decision.id,
                    resource_type="decision_journal",
                )
            )

        shadow_plan = orchestration["shadow_experiment"]
        if (
            shadow_plan["should_create"]
            and shadow_plan["name"]
            and shadow_plan["policy_description"]
        ):
            shadow_service = ShadowService(self.session)
            experiment = await shadow_service.create_experiment(
                ShadowExperimentCreate(
                    name=shadow_plan["name"],
                    policy_description=shadow_plan["policy_description"],
                    trigger_type="agent_turn",
                    trigger_reason=payload.message[:400],
                    horizon_label="adaptive",
                    initiated_by="agent",
                )
            )
            summary = "Created shadow experiment."
            if shadow_plan.get("auto_run", False):
                experiment = await shadow_service.run_experiment(experiment.id)
                summary = (
                    f"Created and ran shadow experiment ({experiment.run_status})."
                )
            actions.append(
                AgentActionResponse(
                    action_type="shadow_experiment",
                    status="executed",
                    summary=summary,
                    resource_id=experiment.id,
                    resource_type="shadow_experiment",
                )
            )

        verification_plan = orchestration["verification"]
        if verification_plan["should_run"] and verification_plan["challenge_text"]:
            verification = await VerificationService(self.session).run(
                VerificationRequest(
                    subject_id=subject_id,
                    subject_type=subject_type,
                    trigger="user_challenge",
                    challenge_text=verification_plan["challenge_text"],
                )
            )
            actions.append(
                AgentActionResponse(
                    action_type="verification",
                    status="executed",
                    summary=f"Ran verification. Verified stance: {verification.verified_stance}.",
                    resource_id=verification.id,
                    resource_type="verification_run",
                )
            )

        pruning_plan = orchestration.get("pruning")
        if pruning_plan and pruning_plan["should_run"]:
            try:
                pruning_service = PruningService(self.session)
                result = await pruning_service.prune_stale_knowledge(
                    subject_id=subject_id,
                    subject_type=subject_type,
                )
                pruned_count = result.get("pruned_count", 0)
                review_required = bool(result.get("review_required"))
                detail = str(result.get("detail") or "").strip()
                actions.append(
                    AgentActionResponse(
                        action_type="knowledge_pruning",
                        status="review_required" if review_required else "executed",
                        summary=(
                            detail
                            if review_required
                            else f"Ran knowledge pruning. Soft-deleted {pruned_count} stale nodes."
                        ),
                        resource_type="pruning_run",
                    )
                )
            except Exception as e:
                pass

        if orchestration.get("memory_note"):
            actions.append(
                AgentActionResponse(
                    action_type="memory_update",
                    status="executed",
                    summary=orchestration["memory_note"],
                    resource_type="conversation_memory",
                )
            )
        return actions

    def _preview_actions(self, orchestration: dict) -> list[AgentActionResponse]:
        actions: list[AgentActionResponse] = []
        if orchestration["decision"]["should_create"]:
            actions.append(
                AgentActionResponse(
                    action_type="decision_journal",
                    status="planned",
                    summary="Agent would write a decision journal entry from this turn.",
                    resource_type="decision_journal",
                )
            )
        if orchestration["shadow_experiment"]["should_create"]:
            actions.append(
                AgentActionResponse(
                    action_type="shadow_experiment",
                    status="planned",
                    summary="Agent would create a shadow experiment from this turn.",
                    resource_type="shadow_experiment",
                )
            )
        if orchestration["verification"]["should_run"]:
            actions.append(
                AgentActionResponse(
                    action_type="verification",
                    status="planned",
                    summary="Agent would trigger verification mode from this turn.",
                    resource_type="verification_run",
                )
            )
        if orchestration.get("memory_note"):
            actions.append(
                AgentActionResponse(
                    action_type="memory_update",
                    status="planned",
                    summary=orchestration["memory_note"],
                    resource_type="conversation_memory",
                )
            )
        return actions

    async def _get_or_create_conversation_source(self) -> Source:
        source = (
            await self.session.execute(
                select(Source).where(Source.name == CONVERSATION_SOURCE_NAME)
            )
        ).scalar_one_or_none()
        if source is not None:
            if source.is_trusted:
                source.is_trusted = False
            return source
        source = Source(
            name=CONVERSATION_SOURCE_NAME,
            source_type="manual",
            description="Prophet agent conversation turns and autonomous operating updates.",
            is_trusted=False,
        )
        self.session.add(source)
        await self.session.flush()
        return source

    async def _store_agent_memory(
        self,
        *,
        source_id: UUID,
        title: str,
        content: str,
        source_item_type: str,
        metadata_json: dict,
    ) -> None:
        await self.ingestion.ingest_text(
            RawEvidenceCreate(
                title=title,
                source_id=source_id,
                source_item_type=source_item_type,
                metadata_json=metadata_json,
                content=content,
            ),
            process_now=False,
        )

    async def _persist_conversation_exchange(
        self,
        *,
        session_id: UUID,
        user_message: str,
        user_subject_id: UUID,
        user_subject_type: str,
        assistant_message: str,
        assistant_subject_id: UUID,
        assistant_subject_type: str,
        assistant_metadata: dict,
    ) -> None:
        source = await self._get_or_create_conversation_source()
        session_title = self._session_title_from_message(user_message)
        await self._store_agent_memory(
            source_id=source.id,
            title=f"User turn: {user_message[:80]}",
            content=user_message,
            source_item_type="conversation_turn",
            metadata_json={
                "role": "user",
                "session_id": str(session_id),
                "session_title": session_title,
                "subject_id": str(user_subject_id),
                "subject_type": user_subject_type,
                "origin": "agent_chat",
                "message_kind": "chat",
            },
        )
        await self._store_agent_memory(
            source_id=source.id,
            title=f"Assistant turn: {assistant_message[:80]}",
            content=assistant_message,
            source_item_type="conversation_turn",
            metadata_json={
                "role": "assistant",
                "session_id": str(session_id),
                "session_title": session_title,
                "subject_id": str(assistant_subject_id),
                "subject_type": assistant_subject_type,
                **assistant_metadata,
                "origin": "agent_chat",
                "message_kind": "chat",
            },
        )

    def _should_persist_conversation_exchange(
        self,
        *,
        user_message: str,
        assistant_message: str,
        operating_query_type: str,
        process_mode: str,
        actions: list[AgentActionResponse],
        stance: str | None,
        confidence_band: str | None,
        thesis_summary: str | None,
        reasoning_run_id: UUID | None,
    ) -> bool:
        normalized_user = " ".join(user_message.lower().split())
        normalized_assistant = " ".join(assistant_message.lower().split())
        if self._is_background_conversation_text(normalized_user):
            return False
        useful_action_types = {
            action.action_type
            for action in actions
            if action.action_type not in {"memory_update"}
        }
        if operating_query_type in {
            "trusted_sources",
            "trusted_source_detail",
            "benchmark",
            "portfolio_state",
            "entity_source_gap",
            "knowledge_status",
            "shadow_status",
            "lessons",
            "review_queue",
        }:
            return True
        if operating_query_type in {"conversation", "smalltalk"}:
            return False
        if self._is_low_value_prompt(normalized_user):
            return False
        if process_mode == "reasoning_analysis":
            return True
        if useful_action_types:
            return True
        thin_markers = [
            "i do not have enough evidence",
            "the current accepted state remains no_view",
            "no investable view is justified yet",
            "the packet is empty",
            "coverage is missing",
        ]
        if any(marker in normalized_assistant for marker in thin_markers):
            return False
        if thesis_summary and thesis_summary.strip():
            return True
        if reasoning_run_id is not None and confidence_band in {"high", "very_high"}:
            return True
        if len(normalized_user) < 10 and not reasoning_run_id:
            return False
        return process_mode == "operating_context_llm"

    def _conversation_message_kind(
        self,
        evidence: RawEvidence,
        metadata: dict,
        *,
        content: str | None = None,
        is_artifact: bool | None = None,
    ) -> str:
        raw_kind = str(metadata.get("message_kind") or "").strip().lower()
        if raw_kind in {
            "chat",
            "system_artifact",
            "research_artifact",
            "live_trace",
            "tool_action",
        }:
            return raw_kind
        if is_artifact is None:
            is_artifact = self._is_background_conversation_memory(
                evidence, metadata, content=content
            )
        if not is_artifact:
            return "chat"
        marker_text = " ".join(
            [
                evidence.title or "",
                str(metadata.get("session_title") or ""),
                str(metadata.get("process_mode") or ""),
                content or "",
            ]
        ).lower()
        if "research" in marker_text:
            return "research_artifact"
        if (
            "live trace" in marker_text
            or "queued" in marker_text
            or "running" in marker_text
        ):
            return "live_trace"
        return "system_artifact"

    @staticmethod
    def _conversation_display_role(metadata: dict, *, is_artifact: bool) -> str:
        if is_artifact:
            return "system"
        role = str(metadata.get("role") or "system").lower()
        if role in {"assistant", "user", "system"}:
            return role
        return "system"

    @staticmethod
    def _safe_uuid(value: object) -> UUID | None:
        if value is None:
            return None
        try:
            return UUID(str(value))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _compact_exception(exc: BaseException) -> str:
        return compact_exception_message(exc)

    def _is_background_conversation_memory(
        self,
        evidence: RawEvidence,
        metadata: dict,
        *,
        content: str | None = None,
    ) -> bool:
        if metadata.get("origin") == "agent_reflection":
            return True
        title_parts = [
            evidence.title or "",
            str(metadata.get("session_title") or ""),
            str(metadata.get("process_mode") or ""),
        ]
        if content is not None:
            title_parts.append(content)
        return any(self._is_background_conversation_text(part) for part in title_parts)

    @staticmethod
    def _is_background_conversation_text(text: str | None) -> bool:
        return is_internal_artifact_text(text)

    def _normalize_assistant_message(
        self,
        assistant_message: str,
        *,
        reasoning_result: dict | None,
    ) -> str:
        text = assistant_message.strip()
        if not text.startswith("{"):
            return assistant_message
        try:
            payload = json.loads(text)
        except Exception as exc:
            import logging

            logging.getLogger(__name__).warning(
                "Assistant message JSON normalization skipped: %s",
                self._compact_exception(exc),
            )
            return assistant_message
        if not isinstance(payload, dict):
            return assistant_message
        if "assistant_message" in payload and isinstance(
            payload["assistant_message"], str
        ):
            return payload["assistant_message"]
        if reasoning_result is not None and {
            "stance",
            "confidence_band",
            "thesis_summary",
            "reasoning",
        }.issubset(payload.keys()):
            return self._fast_path_orchestration(
                AgentTurnRequest(message="", auto_execute=False),
                reasoning_result,
            )["assistant_message"]
        return assistant_message

    def _merge_research_follow_up_into_answer(
        self, assistant_message: str, *, processed: bool
    ) -> str:
        text = " ".join((assistant_message or "").split()).strip()
        note = (
            "I also started a targeted research pass on the weakest part of this view and pushed it into the evidence loop."
            if processed
            else "I also started a targeted research pass on the weakest part of this view and queued it for follow-on extraction."
        )
        follow_up = (
            "It will show up in Activity/Research status, and it will not change the accepted thesis "
            "until source-backed extraction succeeds."
        )
        if not text:
            return f"{note} {follow_up}"
        if text.endswith((".", "!", "?")):
            return f"{text} {note} {follow_up}"
        return f"{text}. {note} {follow_up}"

    def _merge_research_status_into_answer(
        self, assistant_message: str, note: str
    ) -> str:
        text = " ".join((assistant_message or "").split()).strip()
        note = " ".join((note or "").split()).strip()
        if not note:
            return text
        if not text:
            return note
        if text.endswith((".", "!", "?")):
            return f"{text} {note}"
        return f"{text}. {note}"

    async def _load_raw_content(self, evidence: RawEvidence) -> str:
        if not evidence.raw_content_ref:
            return evidence.title or ""
        try:
            raw_bytes = await self.storage.get_object(evidence.raw_content_ref)
            return raw_bytes.decode("utf-8", errors="ignore")
        except FileNotFoundError:
            source_item = (
                await self.session.execute(
                    select(SourceItem).where(SourceItem.raw_evidence_id == evidence.id)
                )
            ).scalar_one_or_none()
            fallback_parts = [
                evidence.title.strip() if evidence.title else "",
                (
                    source_item.summary.strip()
                    if source_item and source_item.summary
                    else ""
                ),
                (
                    source_item.extracted_text.strip()[:2400]
                    if source_item and source_item.extracted_text
                    else ""
                ),
                evidence.url.strip() if evidence.url else "",
            ]
            fallback_text = "\n\n".join(part for part in fallback_parts if part)
            if fallback_text:
                return fallback_text
            return evidence.title or evidence.url or ""
        except Exception as exc:
            import logging

            logging.getLogger(__name__).warning(
                "Raw evidence content fallback failed: %s",
                self._compact_exception(exc),
            )
            return evidence.title or evidence.url or ""

    async def _resolve_position_id(
        self, subject_id: UUID, subject_type: str
    ) -> UUID | None:
        if subject_type == "position":
            return subject_id
        if subject_type != "entity":
            return None
        positions = list(
            (
                await self.session.execute(
                    select(Position)
                    .join(Security, Position.security_id == Security.id)
                    .where(
                        Security.entity_id == subject_id,
                        Position.list_type == "holding",
                    )
                    .order_by(desc(Position.market_value))
                )
            )
            .scalars()
            .all()
        )
        return positions[0].id if positions else None

    async def _subject_ticker(self, subject_id: UUID, subject_type: str) -> str | None:
        if subject_type == "portfolio":
            return "PORTFOLIO"
        if subject_type == "position":
            position = (
                await self.session.execute(
                    select(Position).where(Position.id == subject_id)
                )
            ).scalar_one_or_none()
            if position is None:
                return None
            security = (
                await self.session.execute(
                    select(Security).where(Security.id == position.security_id)
                )
            ).scalar_one_or_none()
            return None if security is None else security.ticker
        if subject_type == "entity":
            security = (
                (
                    await self.session.execute(
                        select(Security)
                        .where(Security.entity_id == subject_id)
                        .order_by(Security.ticker.asc())
                    )
                )
                .scalars()
                .first()
            )
            return None if security is None else security.ticker
        return None

    async def _select_autonomous_candidate(self) -> tuple[Position, Security] | None:
        rows = (
            await self.session.execute(
                select(Position, Security)
                .join(Security, Position.security_id == Security.id)
                .where(Position.list_type == "holding", Position.quantity > 0)
                .order_by(desc(Position.market_value))
            )
        ).all()
        cutoff = datetime.now(UTC).replace(microsecond=0)
        for position, security in rows:
            recent_decision = (
                (
                    await self.session.execute(
                        select(DecisionJournal)
                        .where(DecisionJournal.position_id == position.id)
                        .order_by(desc(DecisionJournal.created_at))
                        .limit(1)
                    )
                )
                .scalars()
                .first()
            )
            recent_shadow = (
                (
                    await self.session.execute(
                        select(ShadowExperiment)
                        .where(
                            ShadowExperiment.name
                            == f"Autonomous review: {security.ticker}"
                        )
                        .order_by(desc(ShadowExperiment.created_at))
                        .limit(1)
                    )
                )
                .scalars()
                .first()
            )
            if recent_decision is None:
                return position, security
            if (cutoff - recent_decision.created_at).total_seconds() > 86400:
                return position, security
            if (
                recent_shadow is None
                or (cutoff - recent_shadow.created_at).total_seconds() > 86400
            ):
                return position, security
        return None

    async def _subject_candidates(
        self, message: str
    ) -> list[AgentContextCandidateResponse]:
        query = message.strip()
        query_lower = query.lower()
        exact_ticker_tokens = set(re.findall(r"\b[A-Z]{1,5}\b", message))
        query_tokens = set(re.findall(r"\b[a-z0-9]{1,20}\b", query_lower))
        broad_query = self._broad_subject_match_query(message)
        rows = (
            await self.session.execute(
                select(Position, Security, Entity)
                .join(Security, Position.security_id == Security.id)
                .join(Entity, Security.entity_id == Entity.id)
                .where(
                    Position.list_type.in_(
                        ["holding", "watchlist", "considering", "theme_basket"]
                    )
                )
            )
        ).all()
        candidates: list[AgentContextCandidateResponse] = []
        seen: set[tuple[UUID, str]] = set()

        for position, security, entity in rows:
            score = 0
            reasons: list[str] = []
            matched = False
            ticker = (security.ticker or "").upper()
            entity_name = (entity.name or "").lower()
            if ticker and ticker in exact_ticker_tokens:
                score += 100
                reasons.append(f"Matched ticker {ticker}")
                matched = True
            if ticker and ticker.lower() in query_tokens:
                score += 80
                reasons.append(f"Matched ticker text {ticker}")
                matched = True
            if entity_name and entity_name in query_lower:
                score += 70
                reasons.append(f"Matched company name {entity.name}")
                matched = True
            if matched and position.list_type == "holding":
                score += 35
                reasons.append("Active holding receives portfolio priority")
            elif matched and position.list_type == "considering":
                score += 20
            elif matched and position.list_type == "watchlist":
                score += 15
            if score <= 0:
                continue
            key = (security.entity_id, "entity")
            if key in seen:
                continue
            seen.add(key)
            candidates.append(
                AgentContextCandidateResponse(
                    subject_id=security.entity_id,
                    subject_type="entity",
                    subject_name=f"{ticker} · {entity.name}",
                    score=score,
                    reason="; ".join(reasons),
                )
            )

        for alias_candidate in await SubjectAliasService(
            self.session
        ).candidates_for_message(message):
            key = (alias_candidate.subject_id, alias_candidate.subject_type)
            if key in seen:
                continue
            seen.add(key)
            candidates.append(
                AgentContextCandidateResponse(
                    subject_id=alias_candidate.subject_id,
                    subject_type=alias_candidate.subject_type,
                    subject_name=alias_candidate.subject_name,
                    score=alias_candidate.score,
                    reason=alias_candidate.reason,
                )
            )

        if not candidates and broad_query:
            entity_rows = (
                await self.session.execute(
                    select(Entity, Security)
                    .join(Security, Security.entity_id == Entity.id)
                    .where(
                        or_(
                            Entity.name.ilike(f"%{broad_query}%"),
                            Security.ticker.ilike(f"%{broad_query.upper()}%"),
                        )
                    )
                    .limit(5)
                )
            ).all()
            for entity, security in entity_rows:
                candidates.append(
                    AgentContextCandidateResponse(
                        subject_id=entity.id,
                        subject_type="entity",
                        subject_name=f"{security.ticker} · {entity.name}",
                        score=25,
                        reason="Matched known security/entity in stored research objects.",
                    )
                )

        if broad_query:
            theme_rows = (
                (
                    await self.session.execute(
                        select(Theme)
                        .where(Theme.name.ilike(f"%{broad_query}%"))
                        .limit(5)
                    )
                )
                .scalars()
                .all()
            )
            for theme in theme_rows:
                candidates.append(
                    AgentContextCandidateResponse(
                        subject_id=theme.id,
                        subject_type="theme",
                        subject_name=theme.name,
                        score=18,
                        reason="Matched theme name in stored research.",
                    )
                )

        candidates.sort(key=lambda item: (item.score, item.subject_name), reverse=True)
        return candidates

    def _merge_context_candidates(
        self,
        primary: list[AgentContextCandidateResponse],
        hinted: list[AgentContextCandidateResponse],
    ) -> list[AgentContextCandidateResponse]:
        merged: OrderedDict[tuple[UUID, str], AgentContextCandidateResponse] = (
            OrderedDict()
        )
        for candidate in [*primary, *hinted]:
            key = (candidate.subject_id, candidate.subject_type)
            current = merged.get(key)
            if current is None or candidate.score > current.score:
                merged[key] = candidate
        return sorted(
            merged.values(),
            key=lambda item: (item.score, item.subject_name),
            reverse=True,
        )

    async def _subject_candidates_from_hint(
        self, message: str
    ) -> list[AgentContextCandidateResponse]:
        try:
            result = await asyncio.wait_for(
                call_llm_json(
                    system_prompt=(
                        "You are the context-resolution engine for Prophet, a sophisticated investment research platform. "
                        "Given the user's message, identify the primary subject they are inquiring about. "
                        "\n\nRULES:"
                        "\n1. SUBJECT_KIND: Use 'entity' for companies, tickers, specific people, or assets. "
                        "Use 'theme' for broader narratives, sectors (AI, Energy), or geopolitical situations. "
                        "Use 'portfolio' ONLY if they ask about 'my book', 'my holdings', or 'the whole portfolio'. "
                        "Use 'none' if the query is general or doesn't target a specific research object."
                        "\n2. TOPIC SHIFTS: Be sensitive to shifts. If the user was discussing one company and then asks about a distinct geopolitical event, "
                        "the event is the new subject. Do not drag old context into a distinct query unless the user or evidence clearly links them."
                        "\n3. MAPPING: Return the stock ticker if applicable, and a descriptive search_text for the research system."
                    ),
                    user_prompt=json.dumps({"message": message}, ensure_ascii=True),
                    schema=SUBJECT_HINT_SCHEMA,
                    timeout_seconds=3,
                ),
                timeout=3,
            )
        except Exception as exc:
            import logging

            logging.getLogger(__name__).warning(
                "Subject hint skipped after LLM failure: %s",
                self._compact_exception(exc),
            )
            return []

        subject_kind = str(result.get("subject_kind") or "none")
        if subject_kind not in {"entity", "theme", "portfolio"}:
            return []

        ticker = str(result.get("ticker") or "").strip().upper()
        search_text = str(result.get("search_text") or "").strip()

        candidates: list[AgentContextCandidateResponse] = []
        if subject_kind == "portfolio":
            candidates.append(
                AgentContextCandidateResponse(
                    subject_id=PORTFOLIO_SUBJECT_ID,
                    subject_type="portfolio",
                    subject_name=PORTFOLIO_SUBJECT_NAME,
                    score=48,
                    reason=str(
                        result.get("reason")
                        or "LLM subject hint classified this as a portfolio-wide request."
                    ),
                )
            )
        elif subject_kind == "entity":
            entity_rows = (
                await self.session.execute(
                    select(Entity, Security)
                    .join(Security, Security.entity_id == Entity.id)
                    .where(
                        or_(
                            Security.ticker.ilike(f"%{ticker}%") if ticker else False,
                            (
                                Entity.name.ilike(f"%{search_text}%")
                                if search_text
                                else False
                            ),
                        )
                    )
                    .limit(6)
                )
            ).all()
            for entity, security in entity_rows:
                label = (
                    security.ticker
                    if not entity.name
                    else f"{security.ticker} · {entity.name}"
                )
                candidates.append(
                    AgentContextCandidateResponse(
                        subject_id=entity.id,
                        subject_type="entity",
                        subject_name=label,
                        score=42 if ticker else 34,
                        reason=f"LLM subject hint matched {label}.",
                    )
                )
        elif subject_kind == "theme" and search_text:
            themes = (
                (
                    await self.session.execute(
                        select(Theme)
                        .where(Theme.name.ilike(f"%{search_text}%"))
                        .limit(6)
                    )
                )
                .scalars()
                .all()
            )
            for theme in themes:
                candidates.append(
                    AgentContextCandidateResponse(
                        subject_id=theme.id,
                        subject_type="theme",
                        subject_name=theme.name,
                        score=30,
                        reason=f"LLM subject hint matched theme {theme.name}.",
                    )
                )
        return candidates

    async def _maybe_conversational_handoff(
        self,
        *,
        message: str,
        resolved_subject_name: str,
        resolved_subject_type: str,
        history: list[AgentConversationEntryResponse] | None = None,
    ) -> str | None:
        safe_history = history or []
        history_context = "\n".join(
            [f"{t.role.upper()}: {t.content}" for t in safe_history[-6:]]
        )
        try:
            prompt_payload = json.dumps(
                {
                    "message": message,
                    "resolved_subject_name": resolved_subject_name,
                    "resolved_subject_type": resolved_subject_type,
                    "recent_history": history_context,
                },
                ensure_ascii=True,
            )
            handoff_system_prompt = (
                "Decide whether the user is asking Prophet to analyze now, or whether they are still offering to share their own thesis/view first and need an invitation to continue. "
                "Use should_handoff=true only when the user has not actually shared the substance yet and the best next move is to invite them to say more. "
                "Examples that usually need handoff: asking whether you want to hear their thoughts, view, thesis, or reasoning before they have stated it. "
                "Do not use handoff when they already gave a real thesis, evidence, concrete claim, or argument to react to."
            )
            try:
                result = await asyncio.wait_for(
                    call_llm_json(
                        system_prompt=handoff_system_prompt,
                        user_prompt=prompt_payload,
                        schema=CONVERSATIONAL_HANDOFF_SCHEMA,
                        timeout_seconds=3,
                    ),
                    timeout=3,
                )
            except Exception as exc:
                import logging

                logging.getLogger(__name__).warning(
                    "Conversation handoff check skipped after LLM failure: %s",
                    self._compact_exception(exc),
                )
                return None
        except Exception as exc:
            import logging

            logging.getLogger(__name__).warning(
                "Conversation handoff setup skipped after failure: %s",
                self._compact_exception(exc),
            )
            return None
        if not bool(result.get("should_handoff")):
            return None
        handoff = str(result.get("assistant_message") or "").strip()
        if self._looks_like_failed_operating_answer({"assistant_message": handoff}):
            return None
        if handoff:
            return handoff
        subject_label = resolved_subject_name or "it"
        return f"Yes. Share your current view on {subject_label} in full, and I’ll pressure-test it, connect it to the rest of the portfolio, and tell you what seems strong, weak, or missing."

    async def _maybe_operating_context_answer(
        self,
        *,
        session_id: UUID,
        message: str,
        resolved_subject_id: UUID,
        resolved_subject_type: str,
        resolved_subject_name: str,
        allow_actions: bool,
        history: list[AgentConversationEntryResponse] | None = None,
    ) -> dict[str, object] | None:
        """
        The dynamic tool-calling pass for the operating agent.
        Instead of hardcoded buckets, we let the LLM decide which tools to call
        to answer from the deterministic operating state.
        """
        if PortfolioLookaheadService.looks_like_lookahead_request(message):
            payload = {
                "query_type": "portfolio_lookahead",
                "user_message": message,
                "portfolio_lookahead": await PortfolioLookaheadService(
                    self.session
                ).build_payload(
                    message=message,
                    run_live_scan=allow_actions,
                ),
            }
            result = await self._llm_operating_answer(payload)
            result["operating_query_type"] = "portfolio_lookahead"
            result["subject_id"] = resolved_subject_id
            result["subject_type"] = resolved_subject_type
            result["subject_name"] = resolved_subject_name
            result["resolution_reason"] = (
                "Handled by deterministic portfolio lookahead before tool-router dispatch."
            )
            return result

        recent_topic = await self._recent_session_topic(session_id)
        safe_history = history or []

        system_prompt = (
            "Select the best tool for the user's operating-state question, or answer directly for pure conversation. "
            "Use deterministic tools for portfolio/accounting state, saved Knowledge graph contents, trusted sources, lessons, research automation, review queue, benchmarks, shadow experiments, Gmail, and YouTube ingestion. "
            "Use get_portfolio_lookahead when the user asks what to watch, what is coming up, what to pay attention to, reminders, countdowns, upcoming earnings, or next-week portfolio catalysts. "
            "Use get_knowledge_status when the user asks whether information was saved, stored, visible in Knowledge, present as nodes, or recently ingested into facts/claims/events. "
            "Use start_research_pass only for a real external research need or explicit action request. "
            "If the user asks for investment judgment, thesis work, market implications, valuation, or complex dot-connecting and deterministic state is not enough, return no content and no tool calls so the reasoning analyst can handle it. "
            "If recent history shows an action was blocked by analysis-only mode and the user retries with actions allowed, call the relevant action tool. "
            f"\n\nCURRENT CONTEXT: session_topic={recent_topic}, subject={resolved_subject_name} ({resolved_subject_type}), allow_actions={allow_actions}."
        )

        history_context = "\n".join(
            [f"{t.role.upper()}: {t.content}" for t in safe_history]
        )
        if history_context:
            system_prompt += f"\n\nRECENT CONVERSATION HISTORY:\n{history_context}"

        try:
            # Step 1: Resolve Tools
            tool_call_message = await call_llm_tools(
                system_prompt=system_prompt,
                user_prompt=message,
                tools=AGENT_TOOLS,
                timeout_seconds=5,
            )
        except Exception as e:
            # Fallback for providers that don't support tool calling yet
            if PortfolioLookaheadService.looks_like_lookahead_request(message):
                payload = {
                    "query_type": "portfolio_lookahead",
                    "user_message": message,
                    "portfolio_lookahead": await PortfolioLookaheadService(
                        self.session
                    ).build_payload(
                        message=message,
                        run_live_scan=allow_actions,
                    ),
                }
                result = await self._llm_operating_answer(payload)
                result["operating_query_type"] = "portfolio_lookahead"
                result["subject_id"] = resolved_subject_id
                result["subject_type"] = resolved_subject_type
                result["subject_name"] = resolved_subject_name
                result["resolution_reason"] = (
                    "Handled as portfolio lookahead after tool router fallback."
                )
                return result
            return None

        # Handle a direct LLM response with no tool call.
        if not tool_call_message.get("tool_calls"):
            content = tool_call_message.get("content")
            if not content:
                return None
            return {
                "assistant_message": content,
                "operating_query_type": "conversation",
                "subject_id": resolved_subject_id,
                "subject_type": resolved_subject_type,
                "subject_name": resolved_subject_name,
                "resolution_reason": "Handling conversational turn directly.",
            }

        # Step 2: Execute first tool call (single dispatch for now for speed)
        tool_call = tool_call_message["tool_calls"][0]
        tool_name = tool_call["function"]["name"]
        args = json.loads(tool_call["function"]["arguments"] or "{}")

        payload = {"query_type": tool_name, "user_message": message}
        target_subject_id = resolved_subject_id
        target_subject_type = resolved_subject_type
        target_subject_name = resolved_subject_name

        if tool_name == "get_portfolio_state":
            payload["query_type"] = "portfolio_state"
            payload["portfolio_state"] = await self._portfolio_state_payload()
        elif tool_name == "get_performance_attribution":
            payload["query_type"] = "performance_attribution"
            raw_days = args.get("days")
            try:
                days = int(float(raw_days)) if raw_days is not None else 21
            except (TypeError, ValueError):
                days = 21
            days = max(1, min(days, 1825))
            attribution = await RiskService(self.session).get_performance_attribution(
                window_days=days
            )
            payload["performance_attribution"] = attribution.model_dump(mode="json")
        elif tool_name == "get_entity_overview":
            payload["query_type"] = "entity_overview"
            ticker = args.get("ticker")
            if ticker:
                # Basic ticker resolution if the LLM provided one
                # but we usually prefer the resolved subject from the agent cycle
                pass
            payload["entity_overview"] = await self._entity_overview_payload(
                target_subject_id, target_subject_name
            )
        elif tool_name == "get_research_status":
            payload["query_type"] = "research_status"
            payload["research_status"] = await self._research_status_payload()
        elif tool_name == "get_knowledge_status":
            payload["query_type"] = "knowledge_status"
            query = args.get("query")
            if not isinstance(query, str) or not query.strip():
                query = message
            payload["knowledge_status"] = await self._knowledge_status_payload(
                subject_id=target_subject_id,
                subject_type=target_subject_type,
                subject_name=target_subject_name,
                query=query,
            )
        elif tool_name == "get_trusted_sources":
            payload["query_type"] = "trusted_sources"
            payload["trusted_sources"] = await self._trusted_sources_payload()
        elif tool_name == "get_lessons":
            payload["query_type"] = "lessons"
            payload["lessons"] = await self._lessons_payload()
        elif tool_name == "get_shadow_status":
            payload["query_type"] = "shadow_status"
            payload["shadow_status"] = await self._shadow_status_payload()
        elif tool_name == "get_review_queue":
            payload["query_type"] = "review_queue"
            payload["review_queue"] = await self._review_queue_payload()
        elif tool_name == "get_benchmarks":
            payload["query_type"] = "benchmark"
            payload["benchmark"] = await self._benchmark_payload()
        elif tool_name == "get_portfolio_lookahead":
            payload["query_type"] = "portfolio_lookahead"
            raw_days = args.get("days")
            try:
                days = int(float(raw_days)) if raw_days is not None else None
            except (TypeError, ValueError):
                days = None
            payload["portfolio_lookahead"] = await PortfolioLookaheadService(
                self.session
            ).build_payload(
                message=message,
                days=days,
                run_live_scan=allow_actions,
            )
        elif tool_name == "start_research_pass":
            payload["query_type"] = "research_start"
            if not allow_actions:
                return {
                    "assistant_message": "I can start research from here, but this turn is in analysis-only mode. Enable state updates first.",
                    "operating_query_type": "research_start",
                }
            research_query = args.get("query")
            if not isinstance(research_query, str) or not research_query.strip():
                research_query = message
            focus_source = research_query.strip()
            if self._is_affirmative_follow_up(
                " ".join(message.lower().split())
            ) or self._is_low_value_prompt(" ".join(message.lower().split())):
                prior_message = self._recent_substantive_user_message(
                    safe_history, message
                )
                if prior_message:
                    research_query = prior_message
                    focus_source = prior_message
            payload["research_start"] = await self._run_research_from_chat(
                session_id=session_id,
                subject_id=target_subject_id,
                subject_type=target_subject_type,
                subject_name=target_subject_name,
                user_message=message,
                research_query=research_query,
                focus_label=self._research_focus_label(
                    research_query=focus_source,
                    subject_name=target_subject_name,
                    subject_type=target_subject_type,
                ),
            )
        elif tool_name == "watch_youtube_video":
            if not allow_actions:
                return {
                    "assistant_message": "I can ingest YouTube caption transcripts, but this turn is in analysis-only mode. Enable state updates first.",
                    "operating_query_type": "youtube_start",
                }
            from investos.services.youtube import YouTubeService

            video_url = args.get("url")
            video_title = args.get("title")
            payload["youtube_result"] = await YouTubeService(self.session).ingest_video(
                url=video_url, title=video_title
            )
        elif tool_name == "discover_gmail_brokers":
            from investos.services.gmail_discovery import GmailDiscoveryService

            payload["discovery_result"] = await GmailDiscoveryService(
                self.session
            ).discover_potential_brokers()
        else:
            # Unknown tool
            return None

        # Step 3: Final Synthesis
        result = await self._llm_operating_answer(payload)
        if self._looks_like_failed_operating_answer(result):
            return None
        result["operating_query_type"] = str(payload.get("query_type") or tool_name)
        result["subject_id"] = target_subject_id
        result["subject_type"] = target_subject_type
        result["subject_name"] = target_subject_name
        result["resolution_reason"] = f"Responded via autonomous tool call: {tool_name}"
        return result

    def _looks_like_failed_operating_answer(
        self, result: dict[str, object] | None
    ) -> bool:
        if not isinstance(result, dict):
            return True
        message = str(result.get("assistant_message") or "").strip().lower()
        if not message:
            return True
        failure_markers = {
            "could not summarize the current operating state clearly",
            "could not summarize the operating state",
            "i could not summarize",
            "unable to summarize the current operating state",
            "unable to provide a response to this query",
            "not related to the provided context",
            "context is not clear",
            "please provide more information or clarify",
            "i need to handle this situation carefully",
            "i should not proceed",
            "i will explain that i cannot assist",
        }
        return any(marker in message for marker in failure_markers)

    def _broad_subject_match_query(self, query: str) -> str | None:
        normalized = " ".join(
            re.sub(r"[^a-z0-9+./\-\s]", " ", (query or "").lower()).split()
        )
        if not normalized or self._is_low_value_prompt(normalized):
            return None
        searchable = re.sub(
            r"^(about|on|regarding|re|for|tell me about|look at|check|analyze|research)\s+",
            "",
            normalized,
        ).strip()
        if not searchable:
            return None
        stopwords = {
            "a",
            "an",
            "and",
            "are",
            "as",
            "at",
            "be",
            "but",
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
            "me",
            "my",
            "of",
            "on",
            "or",
            "please",
            "should",
            "tell",
            "that",
            "the",
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
        meaningful = [
            token
            for token in re.findall(r"[a-z0-9][a-z0-9+./-]*", searchable)
            if len(token) >= 3 and token not in stopwords
        ]
        if not meaningful:
            return None
        return searchable

    def _is_small_talk_query(self, query_lower: str) -> bool:
        cleaned = re.sub(r"[^a-z0-9\s]", " ", query_lower)
        normalized = " ".join(cleaned.split())
        if not normalized:
            return True
        exact_smalltalk = {
            "hi",
            "hello",
            "hey",
            "yo",
            "sup",
            "good morning",
            "good afternoon",
            "good evening",
            "thanks",
            "thank you",
            "ok",
            "okay",
            "cool",
            "nice",
            "sounds good",
            "test",
            "testing",
        }
        if normalized in exact_smalltalk:
            return True
        tokens = normalized.split()
        filler_tokens = {
            "hi",
            "hello",
            "hey",
            "yo",
            "thanks",
            "thank",
            "you",
            "ok",
            "okay",
        }
        return len(tokens) <= 3 and all(token in filler_tokens for token in tokens)

    def _smalltalk_response(self) -> str:
        return "Hi. Tell me the holding, theme, market event, or portfolio decision you want Prophet to analyze."

    def _is_low_value_prompt(self, query_lower: str) -> bool:
        if self._is_small_talk_query(query_lower):
            return True
        normalized = " ".join(query_lower.split())
        low_value_markers = {
            "follow up",
            "continue",
            "go on",
            "more",
        }
        return normalized in low_value_markers

    def _is_affirmative_follow_up(self, query_lower: str) -> bool:
        normalized = " ".join(re.sub(r"[^a-z0-9\s]", " ", query_lower).split())
        return normalized in {
            "go ahead",
            "go for it",
            "do it",
            "yes",
            "yes do it",
            "yep",
            "sure",
            "okay do it",
            "ok do it",
            "proceed",
            "continue",
        }

    def _ambiguous_standalone_token(self, query: str) -> str | None:
        cleaned = " ".join(re.sub(r"[^A-Za-z0-9\s]", " ", (query or "")).split())
        if not cleaned:
            return None
        tokens = cleaned.split()
        if len(tokens) != 1:
            return None
        token = tokens[0]
        normalized = token.lower()
        follow_up_words = {
            "why",
            "how",
            "more",
            "continue",
            "yes",
            "no",
            "ok",
            "okay",
            "sure",
            "thanks",
            "again",
            "explain",
            "thought",
            "thoughts",
            "view",
            "views",
            "take",
            "opinion",
            "reaction",
            "elaborate",
        }
        if normalized in follow_up_words or self._is_small_talk_query(normalized):
            return None
        if not re.fullmatch(r"[A-Za-z0-9]{2,8}", token):
            return None
        return token.upper()

    def _is_bare_follow_up(self, query: str) -> bool:
        normalized = " ".join(
            re.sub(r"[^a-z0-9\s]", " ", (query or "").lower()).split()
        )
        if not normalized:
            return False
        if normalized.startswith(
            ("i mean ", "to clarify ", "more specifically ", "specifically ")
        ):
            return True
        bare_followups = {
            "thought",
            "thoughts",
            "your thoughts",
            "what are your thoughts",
            "view",
            "views",
            "your view",
            "take",
            "your take",
            "opinion",
            "reaction",
            "why",
            "why so",
            "how so",
            "explain",
            "expand",
            "elaborate",
            "continue",
            "go on",
            "more",
        }
        if normalized in bare_followups:
            return True
        return normalized.endswith(" thoughts") or normalized.endswith(" take")

    def _ambiguous_context_message(self, token: str) -> str:
        return (
            f"I'm not sure what `{token}` refers to, so I'm not going to attach it to the last ticker or create a no-view thesis. "
            "If it is a ticker, include the company name; if it is an acronym/theme, spell it out once; if it is a follow-up, name the link you want tested."
        )

    def _recent_substantive_user_message(
        self,
        history: list[AgentConversationEntryResponse],
        current_message: str,
    ) -> str | None:
        current_normalized = " ".join(current_message.lower().split())
        for entry in reversed(history):
            if entry.role != "user":
                continue
            candidate = (entry.content or "").strip()
            if not candidate:
                continue
            normalized = " ".join(candidate.lower().split())
            if normalized == current_normalized:
                continue
            if self._is_low_value_prompt(normalized) or self._is_affirmative_follow_up(
                normalized
            ):
                continue
            return candidate
        return None

    def _research_focus_label(
        self,
        *,
        research_query: str,
        subject_name: str,
        subject_type: str,
    ) -> str:
        query = " ".join((research_query or "").strip().split())
        if subject_type == "entity" and subject_name:
            return subject_name
        if not query:
            return subject_name or PORTFOLIO_SUBJECT_NAME
        return self._truncate_research_title(query)

    def _truncate_research_title(self, text: str, *, limit: int = 72) -> str:
        normalized = " ".join((text or "").split())
        if len(normalized) <= limit:
            return normalized
        return normalized[: limit - 1].rstrip() + "…"

    async def _trusted_sources_payload(self) -> dict[str, object]:
        return await OperatingStateService(self.session).trusted_sources_payload(
            exclude_names={CONVERSATION_SOURCE_NAME}
        )

    async def _recent_session_topic(self, session_id: UUID) -> str | None:
        source = await self._get_or_create_conversation_source()
        evidence_rows = (
            (
                await self.session.execute(
                    select(RawEvidence)
                    .where(RawEvidence.source_id == source.id)
                    .order_by(RawEvidence.created_at.desc())
                )
            )
            .scalars()
            .all()
        )
        for evidence in evidence_rows:
            metadata = evidence.metadata_json or {}
            if metadata.get("session_id") != str(session_id):
                continue
            if metadata.get("operating_query_type"):
                return str(metadata["operating_query_type"])
        return None

    async def _recent_session_subject(
        self, session_id: UUID
    ) -> tuple[UUID, str, str] | None:
        source = await self._get_or_create_conversation_source()
        evidence_rows = (
            (
                await self.session.execute(
                    select(RawEvidence)
                    .where(RawEvidence.source_id == source.id)
                    .order_by(RawEvidence.created_at.desc())
                )
            )
            .scalars()
            .all()
        )
        for evidence in evidence_rows:
            metadata = evidence.metadata_json or {}
            if metadata.get("session_id") != str(session_id):
                continue
            raw_subject_id = metadata.get("subject_id")
            raw_subject_type = metadata.get("subject_type")
            if not raw_subject_id or not raw_subject_type:
                continue
            if raw_subject_type == "portfolio":
                continue
            subject_id = UUID(str(raw_subject_id))
            subject_type = str(raw_subject_type)
            return (
                subject_id,
                subject_type,
                await self._subject_name(subject_id, subject_type),
            )
        return None

    async def _benchmark_payload(self) -> dict[str, object]:
        return await OperatingStateService(self.session).benchmark_payload()

    async def _portfolio_state_payload(self) -> dict[str, object]:
        return await OperatingStateService(self.session).portfolio_state_payload()

    async def _entity_overview_payload(
        self,
        subject_id: UUID,
        subject_name: str,
    ) -> dict[str, object]:
        return await OperatingStateService(self.session).entity_overview_payload(
            subject_id=subject_id,
            subject_name=subject_name,
        )

    async def _research_status_payload(
        self, session_id: UUID | None = None
    ) -> dict[str, object]:
        return await OperatingStateService(self.session).research_status_payload(
            session_id=session_id
        )

    async def _shadow_status_payload(self) -> dict[str, object]:
        return await OperatingStateService(self.session).shadow_status_payload()

    async def _knowledge_status_payload(
        self,
        *,
        subject_id: UUID,
        subject_type: str,
        subject_name: str,
        query: str,
    ) -> dict[str, object]:
        return await OperatingStateService(self.session).knowledge_status_payload(
            subject_id=subject_id,
            subject_type=subject_type,
            subject_name=subject_name,
            query=query,
        )

    async def _lessons_payload(self) -> dict[str, object]:
        return await OperatingStateService(self.session).lessons_payload()

    async def _review_queue_payload(self) -> dict[str, object]:
        return await OperatingStateService(self.session).review_queue_payload()

    async def _run_research_from_chat(
        self,
        *,
        session_id: UUID,
        subject_id: UUID,
        subject_type: str,
        subject_name: str,
        user_message: str,
        research_query: str | None = None,
        focus_label: str | None = None,
    ) -> dict[str, object]:
        query = (research_query or "").strip() or user_message.strip()
        if not query:
            query = (
                f"{subject_name}: what materially changed recently, what do official sources say, "
                "what are the strongest support and contradiction points, and what portfolio implications matter?"
                if subject_type == "entity"
                else "What materially changed across current holdings, what benchmark or macro confounders matter, and where is coverage still thin?"
            )
        if (
            subject_type == "entity"
            and subject_name
            and subject_name.lower() not in query.lower()
        ):
            query = f"{subject_name}: {query}"
        effective_focus = focus_label or self._research_focus_label(
            research_query=query,
            subject_name=subject_name,
            subject_type=subject_type,
        )
        title = (
            f"Research on: {self._truncate_research_title(effective_focus or query)}"
        )
        result = await ResearchService(self.session).run_ad_hoc_request(
            query=query,
            title=title,
            metadata_json={
                "trigger": "chat_research_start",
                "subject_id": str(subject_id),
                "subject_type": subject_type,
                "session_id": str(session_id),
                "requested_via": user_message[:240],
                "focus_label": effective_focus,
                "query": query[:400],
            },
            process_after_ingest=True,
        )
        return {
            "started": result.started,
            "reason": result.reason,
            "evidence_id": str(result.evidence_id) if result.evidence_id else None,
            "processed": result.processed,
            "loop_detail": result.loop_detail,
            "focus_label": effective_focus,
            "query": query,
            "title": title,
        }

    async def _maybe_fresh_research_context(
        self,
        *,
        message: str,
        subject_name: str,
        subject_type: str,
        packet_context: dict[str, Any] | None = None,
        freshness_requirement: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        requirement = (
            {
                "required": bool(freshness_requirement.get("requires_fresh_research")),
                "reason": str(
                    freshness_requirement.get("freshness_reason") or ""
                ).strip(),
            }
            if freshness_requirement is not None
            else self._fallback_fresh_context_requirement(message)
        )
        if not requirement["required"]:
            return None
        if not requirement["reason"]:
            requirement["reason"] = (
                "The answer depends on current external information."
            )

        fallback_query = self._fresh_research_query(
            message=message,
            subject_name=subject_name,
            subject_type=subject_type,
            packet_context=packet_context,
        )
        if not RuntimeSettingsStore.load().research.api_key:
            return {
                "required": True,
                "reason": requirement["reason"],
                "searched": False,
                "status": "research_provider_not_configured",
                "query": fallback_query,
                "checked_at": datetime.now(UTC).isoformat(),
                "results": [],
            }
        query_plan = await self._fresh_research_query_plan(
            message=message,
            subject_name=subject_name,
            subject_type=subject_type,
            fallback_query=fallback_query,
            packet_context=packet_context,
        )
        query = query_plan["query"]

        result = await ResearchService(self.session).search(
            query=query,
            title=f"Fresh check: {self._truncate_research_title(query, limit=96)}",
            search_depth="advanced",
            include_raw_content=False,
            metadata_json={
                "trigger": "chat_freshness_preflight",
                "subject_type": subject_type,
                "subject_name": subject_name,
                "freshness_reason": requirement["reason"],
                "fresh_query_plan_reason": query_plan.get("reason"),
                "information_needs": query_plan.get("information_needs") or [],
            },
            timeout_seconds=12.0,
        )
        return {
            "required": True,
            "reason": requirement["reason"],
            "searched": result.searched,
            "status": result.reason,
            "query": result.query,
            "query_plan": query_plan,
            "checked_at": datetime.now(UTC).isoformat(),
            "results": [
                {
                    "title": str(item.get("title") or "").strip(),
                    "url": str(item.get("url") or "").strip(),
                    "content": compact_context_text(
                        str(item.get("content") or ""), max_chars=520
                    ),
                    "published_date": item.get("published_date"),
                    "score": item.get("score"),
                }
                for item in (result.results or [])[:5]
                if isinstance(item, dict)
            ],
            "variants_tried": result.variants_tried[:4],
        }

    @staticmethod
    def _fallback_fresh_context_requirement(message: str) -> dict[str, str | bool]:
        normalized = " ".join((message or "").lower().split())
        if not normalized:
            return {"required": False, "reason": ""}
        current_event_patterns = (
            r"\b(latest|lately|recent|recently|today|yesterday|this week|this month|right now|now)\b",
            r"\b(happen(?:ed|ing)?|new|fresh|update|changed|move(?:d)?|reaction|read[- ]?through)\b",
            r"\b(deal|agreement|partnership|contract|announcement|press release)\b",
            r"\b(earnings|report|guidance|transcript|quarter|call|results|preannounce|raise[ds]?|cut[ds]?)\b",
            r"\b(acquisition|merger|spin[- ]?off|financing|credit facility|lawsuit|regulatory|approval)\b",
            r"\b(listed|listing|traded|tradable|available (?:for|to) trad(?:e|ing)|u\.?s\.? exchange|adr|otc|ticker symbol)\b",
        )
        matched = [
            pattern
            for pattern in current_event_patterns
            if re.search(pattern, normalized)
        ]
        if not matched:
            return {"required": False, "reason": ""}
        return {
            "required": True,
            "reason": "The user asked about a current or event-specific development, so stored local context may be stale.",
        }

    def _fresh_research_query(
        self,
        *,
        message: str,
        subject_name: str,
        subject_type: str,
        packet_context: dict[str, Any] | None = None,
    ) -> str:
        compact_message = " ".join((message or "").split()).strip(" .?")
        if not compact_message:
            compact_message = "latest company news and market implications"
        subject = " ".join((subject_name or "").split()).strip()
        if (
            subject_type == "entity"
            and subject
            and subject.lower() not in compact_message.lower()
        ):
            return self._augment_event_research_query(f"{subject} {compact_message}")
        if subject_type == "portfolio":
            holdings = self._fresh_research_portfolio_terms(packet_context)
            portfolio_prefix = f"portfolio holdings {holdings}".strip()
            return self._augment_event_research_query(
                f"{portfolio_prefix} {compact_message}"
            )
        return self._augment_event_research_query(compact_message)

    async def _fresh_research_query_plan(
        self,
        *,
        message: str,
        subject_name: str,
        subject_type: str,
        fallback_query: str,
        packet_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        portfolio_terms = self._fresh_research_portfolio_terms(packet_context)
        payload = {
            "message": message,
            "subject_name": subject_name,
            "subject_type": subject_type,
            "fallback_query": fallback_query,
            "portfolio_terms": portfolio_terms,
        }
        try:
            result = await call_llm_json(
                system_prompt=self._fresh_research_query_system_prompt(),
                user_prompt=json.dumps(payload, ensure_ascii=True, default=str),
                schema=FRESH_RESEARCH_QUERY_SCHEMA,
                timeout_seconds=5,
            )
        except Exception:
            return {
                "query": fallback_query,
                "information_needs": [
                    "fresh external evidence for the current event",
                    "investor setup versus actual result before treating the event as bullish or bearish",
                ],
                "reason": "Fresh query planner unavailable; used the compact deterministic fallback query.",
                "planner_fallback": True,
            }
        return self._normalize_fresh_research_query_plan(
            result,
            message=message,
            subject_name=subject_name,
            subject_type=subject_type,
            fallback_query=fallback_query,
            portfolio_terms=portfolio_terms,
        )

    @staticmethod
    def _fresh_research_query_system_prompt() -> str:
        return (
            "Plan one web research query for Prophet's current-event preflight. "
            "Use the user's question and subject to infer what evidence would actually decide the investment read. "
            "Do not use a fixed checklist or include every possible evidence class. "
            "For market-moving events, prefer a query that can recover the market setup versus actual outcome: "
            "what investors likely expected, what happened, how the market reacted, and what changed for the subject or portfolio. "
            "Examples of useful setup evidence can include consensus, whisper, crowding, positioning, sentiment, flows, options-implied move, "
            "guidance revisions, peer read-through, or official event terms, but choose only what fits the case. "
            "Return a concise query and short human-readable information_needs; do not reveal chain of thought."
        )

    def _normalize_fresh_research_query_plan(
        self,
        result: dict[str, Any],
        *,
        message: str,
        subject_name: str,
        subject_type: str,
        fallback_query: str,
        portfolio_terms: str | None = None,
    ) -> dict[str, Any]:
        query = " ".join(str(result.get("query") or "").split()).strip()
        if self._fresh_query_is_low_information(query):
            query = fallback_query
        if (
            subject_type == "entity"
            and subject_name
            and subject_name.lower() not in query.lower()
        ):
            query = f"{subject_name} {query}"
        if subject_type == "portfolio" and portfolio_terms:
            missing_terms = [
                term
                for term in portfolio_terms.split()
                if term
                and not re.search(rf"\b{re.escape(term)}\b", query, flags=re.IGNORECASE)
            ][:4]
            if missing_terms:
                query = f"{' '.join(missing_terms)} {query}"
        if len(query) > 260:
            query = query[:260].rsplit(" ", 1)[0].strip()
        information_needs = [
            " ".join(str(item).split())
            for item in (result.get("information_needs") or [])
            if str(item).strip()
        ][:6]
        if not information_needs:
            information_needs = [
                "fresh external evidence for the current event",
                "investor setup versus actual result before treating the event as bullish or bearish",
            ]
        return {
            "query": query or fallback_query or message.strip(),
            "information_needs": information_needs,
            "reason": str(
                result.get("reason")
                or "Planner selected an event-specific search query."
            ),
            "planner_fallback": False,
        }

    @staticmethod
    def _fresh_query_is_low_information(query: str | None) -> bool:
        compact = " ".join((query or "").split()).strip().lower()
        if not compact:
            return True
        if compact in LOW_INFORMATION_RESEARCH_QUERIES:
            return True
        tokens = re.findall(r"[a-z0-9][a-z0-9+./-]*", compact)
        useful_tokens = [
            token for token in tokens if token not in LOW_INFORMATION_RESEARCH_STOPWORDS
        ]
        return len(useful_tokens) < 2

    @staticmethod
    def _fresh_research_portfolio_terms(packet_context: dict[str, Any] | None) -> str:
        portfolio_context = (packet_context or {}).get("portfolio_context") or {}
        terms: list[str] = []
        for holding in (portfolio_context.get("top_holdings") or [])[:6]:
            if not isinstance(holding, dict):
                continue
            ticker = str(holding.get("ticker") or "").strip().upper()
            if ticker and ticker not in terms:
                terms.append(ticker)
        return " ".join(terms[:6])

    @staticmethod
    def _looks_like_investor_event_question(query: str) -> bool:
        normalized = " ".join((query or "").lower().split())
        if not normalized:
            return False
        event_patterns = (
            r"\bearnings?\b",
            r"\breport\b",
            r"\bresults?\b",
            r"\bguidance\b",
            r"\btranscript\b",
            r"\bquarter\b",
            r"\bcall\b",
            r"\bpreannounce\b",
            r"\bdeal\b",
            r"\bagreement\b",
            r"\bpartnership\b",
            r"\bcontract\b",
            r"\bannouncement\b",
            r"\bnews\b",
            r"\bupdate\b",
            r"\bchanged?\b",
            r"\bhappen(?:ed|ing)?\b",
            r"\blatest\b",
            r"\brecent(?:ly)?\b",
            r"\blately\b",
            r"\bmarket reaction\b",
            r"\bprice reaction\b",
            r"\brevision\b",
        )
        return any(re.search(pattern, normalized) for pattern in event_patterns)

    @classmethod
    def _augment_event_research_query(cls, query: str) -> str:
        compact = " ".join((query or "").split()).strip()
        if not compact or not cls._looks_like_investor_event_question(compact):
            return compact
        fallback_frame = "investor setup versus actual result expectation delta market reaction portfolio read-through"
        if fallback_frame in compact.lower():
            return compact
        return f"{compact} {fallback_frame}"

    def _merge_fresh_research_context_into_answer(
        self,
        assistant_message: str,
        fresh_context: dict[str, Any] | None,
    ) -> str:
        if not fresh_context:
            return assistant_message
        status = str(fresh_context.get("status") or "unknown")
        results = [
            item
            for item in (fresh_context.get("results") or [])[:3]
            if isinstance(item, dict) and (item.get("title") or item.get("url"))
        ]
        source_bits: list[str] = []
        for item in results:
            title = str(item.get("title") or "source").strip()
            url = str(item.get("url") or "").strip()
            domain = urlparse(url).netloc.replace("www.", "") if url else ""
            label = title if not domain else f"{title} ({domain})"
            source_bits.append(label)

        if status == "ok" and source_bits:
            note = (
                "Fresh source check: found "
                + str(len(source_bits))
                + " candidate source"
                + ("" if len(source_bits) == 1 else "s")
                + (
                    f" ({'; '.join(source_bits[:2])}"
                    + ("; ..." if len(source_bits) > 2 else "")
                    + ")"
                )
                + ". These are pre-ingestion snippets, so treat current-event claims as provisional until the evidence is extracted and dated."
            )
        else:
            note = (
                "Fresh source check: current external context was required but did not return usable evidence"
                + (f" (status: {status})." if status else ".")
                + " Treat current-event claims as unverified until fresh evidence is ingested."
            )
        if (
            "Fresh source check:" in assistant_message
            or "Fresh check:" in assistant_message
        ):
            return assistant_message
        return f"{assistant_message}\n\n{note}".strip()

    async def _maybe_auto_research_after_gap(
        self,
        *,
        payload: AgentTurnRequest,
        resolved_subject_id: UUID,
        resolved_subject_type: str,
        resolved_subject_name: str,
        packet_context: dict[str, Any],
        reasoning_result: dict[str, Any],
    ) -> dict[str, Any] | None:
        normalized_message = " ".join((payload.message or "").lower().split())
        if self._is_low_value_prompt(normalized_message):
            return None
        if not RuntimeSettingsStore.load().research.api_key:
            return None
        # Fire research for any thin-evidence, low-confidence result
        stance = reasoning_result.get("stance") or "no_view"
        confidence = str(reasoning_result.get("confidence_band") or "")
        if stance not in {"no_view", "uncertain"} and confidence not in {
            "very_low",
            "low",
        }:
            return None
        research_plan = await self._auto_research_plan(
            message=payload.message,
            subject_type=resolved_subject_type,
            subject_name=resolved_subject_name,
            packet_context=packet_context,
            reasoning_result=reasoning_result,
        )
        if not research_plan["should_research"]:
            if reasoning_result.get("is_fallback"):
                return None
            if not await self._should_broaden_research(
                message=payload.message,
                subject_type=resolved_subject_type,
                subject_name=resolved_subject_name,
                packet_context=packet_context,
                reasoning_result=reasoning_result,
            ):
                return None
            research_plan = self._generic_auto_research_plan(
                message=payload.message,
                subject_type=resolved_subject_type,
                subject_name=resolved_subject_name,
            )

        target_label = str(research_plan["target_label"])
        query = str(research_plan["query"])
        title = f"Auto research: {target_label}"
        result = await ResearchService(self.session).run_ad_hoc_request(
            query=query,
            title=title,
            metadata_json={
                "trigger": "chat_auto_research_gap",
                "subject_id": str(resolved_subject_id),
                "subject_type": resolved_subject_type,
                "requested_via": payload.message[:240],
                "information_needs": research_plan.get("information_needs") or [],
                "research_plan_reason": research_plan.get("reason"),
                "focus_label": target_label,
                "query": query[:400],
            },
            process_after_ingest=False,
        )
        return {
            "started": result.started,
            "reason": result.reason,
            "evidence_id": str(result.evidence_id) if result.evidence_id else None,
            "processed": result.processed,
            "loop_detail": result.loop_detail,
            "query": query,
            "target_label": target_label,
            "information_needs": research_plan.get("information_needs") or [],
            "plan_reason": research_plan.get("reason"),
        }

    async def _auto_research_plan(
        self,
        *,
        message: str,
        subject_type: str,
        subject_name: str,
        packet_context: dict[str, Any],
        reasoning_result: dict[str, Any],
    ) -> dict[str, Any]:
        normalized = " ".join((message or "").lower().split())
        if self._is_low_value_prompt(normalized):
            return {
                "should_research": False,
                "query": "",
                "target_label": "",
                "information_needs": [],
                "reason": "Low-information conversational prompt does not justify autonomous research.",
            }
        direct = len(packet_context.get("direct_evidence") or [])
        connected = len(packet_context.get("connected_evidence") or [])
        historical = len(packet_context.get("historical_evidence") or [])
        contradiction = len(packet_context.get("contradiction_evidence") or [])
        gaps = packet_context.get("gap_flags") or []
        focus_gap = self._query_focus_gap(
            message=message, packet_context=packet_context
        )
        has_research_trigger = (
            bool(reasoning_result.get("is_fallback"))
            or direct == 0
            or bool(gaps)
            or bool(focus_gap.get("should_broaden"))
            or contradiction == 0
        )
        if not has_research_trigger:
            return {
                "should_research": False,
                "query": "",
                "target_label": "",
                "information_needs": [],
                "reason": "Stored packet is not thin enough to require autonomous research.",
            }
        payload = {
            "message": message,
            "subject_type": subject_type,
            "subject_name": subject_name,
            "evidence_counts": {
                "direct": direct,
                "connected": connected,
                "historical": historical,
                "contradiction": contradiction,
            },
            "coverage": packet_context.get("coverage") or {},
            "gap_flags": gaps[:6],
            "query_focus_gap": focus_gap,
            "packet": compact_packet_context(
                packet_context, max_items_per_layer=3, max_chars=220
            ),
            "reasoning_result": compact_reasoning_result(reasoning_result),
        }
        system_prompt = self._auto_research_planner_system_prompt()
        try:
            result = await self._call_auto_research_planner(
                payload=payload,
                system_prompt=system_prompt,
                timeout_seconds=5,
            )
        except Exception as exc:
            import logging

            logging.getLogger(__name__).warning(
                "Auto-research planner fell back after LLM failure: %s",
                self._compact_exception(exc),
            )
            recovered = await self._auto_research_plan_alternate_provider(
                message=message,
                subject_type=subject_type,
                subject_name=subject_name,
                payload=payload,
                system_prompt=system_prompt,
            )
            if recovered:
                return recovered
            return self._generic_auto_research_plan(
                message=message,
                subject_type=subject_type,
                subject_name=subject_name,
            )
        return self._normalize_auto_research_plan_result(
            result,
            message=message,
            subject_type=subject_type,
            subject_name=subject_name,
        )

    @staticmethod
    def _auto_research_planner_system_prompt() -> str:
        return (
            "You are Prophet's information-needs planner. Decide whether a thin or low-confidence answer "
            "needs one external research pass, then write the best search query.\n"
            "Use semantic reasoning from the user's question, subject, evidence packet, coverage gaps, "
            "and current holdings. Do not use hidden ticker/topic templates. Do not invent company-specific "
            "drivers unless they are implied by the prompt, subject, or evidence.\n"
            "Research only real external market, company, macro, or industry questions. Reject greetings, "
            "internal artifacts, and recursive prompts such as 'additional evidence would strengthen the current view'.\n"
            "For earnings, guidance, deals, announcements, or other market events, frame the information need around "
            "the expectation delta: prior consensus/whisper/hurdle, actual result or deal terms, price reaction, "
            "estimate or guidance revisions, peer read-through, and portfolio sizing/timing implications.\n"
            "If research is useful, query must be concrete enough for a web research provider and should name the "
            "subject when one is known. information_needs should be short human-readable needs, not chain of thought. "
            "Output JSON only."
        )

    async def _call_auto_research_planner(
        self,
        *,
        payload: dict[str, Any],
        system_prompt: str,
        timeout_seconds: int,
        provider_override: str | None = None,
    ) -> dict[str, Any]:
        return await asyncio.wait_for(
            call_llm_json(
                system_prompt=system_prompt,
                user_prompt=json.dumps(payload, ensure_ascii=True, default=str),
                schema=AUTO_RESEARCH_PLAN_SCHEMA,
                timeout_seconds=timeout_seconds,
                provider_override=provider_override,
            ),
            timeout=timeout_seconds,
        )

    async def _auto_research_plan_alternate_provider(
        self,
        *,
        message: str,
        subject_type: str,
        subject_name: str,
        payload: dict[str, Any],
        system_prompt: str,
    ) -> dict[str, Any] | None:
        for provider in await available_llm_json_recovery_providers():
            try:
                result = await self._call_auto_research_planner(
                    payload=payload,
                    system_prompt=system_prompt,
                    timeout_seconds=10,
                    provider_override=provider,
                )
                plan = self._normalize_auto_research_plan_result(
                    result,
                    message=message,
                    subject_type=subject_type,
                    subject_name=subject_name,
                )
                plan["planner_recovery_provider"] = provider
                if plan.get("reason"):
                    plan["reason"] = (
                        f"{plan['reason']} Planned by alternate provider: {provider}."
                    )
                return plan
            except Exception:
                continue
        return None

    def _normalize_auto_research_plan_result(
        self,
        result: dict[str, Any],
        *,
        message: str,
        subject_type: str,
        subject_name: str,
    ) -> dict[str, Any]:
        should_research = bool(result.get("should_research"))
        query = " ".join(str(result.get("query") or "").split())
        target_label = " ".join(str(result.get("target_label") or "").split())
        information_needs = [
            " ".join(str(item).split())
            for item in (result.get("information_needs") or [])
            if str(item).strip()
        ][:6]
        if not should_research:
            return {
                "should_research": False,
                "query": "",
                "target_label": "",
                "information_needs": [],
                "reason": str(
                    result.get("reason")
                    or "Planner did not find a useful external research need."
                ),
            }
        if not query:
            query = (
                message.strip()
                if subject_type == "portfolio"
                else f"{subject_name}: {message.strip()}"
            )
        if (
            subject_type == "entity"
            and subject_name
            and subject_name.lower() not in query.lower()
        ):
            query = f"{subject_name}: {query}"
        query = self._augment_event_research_query(query)
        if len(query) > 260:
            query = query[:260].rsplit(" ", 1)[0]
        if not target_label:
            target_label = self._auto_research_target_label(
                message, subject_name, subject_type
            )
        return {
            "should_research": True,
            "query": query,
            "target_label": self._truncate_research_title(target_label, limit=96),
            "information_needs": information_needs
            or ["external evidence targeted to the weak part of the current answer"],
            "reason": str(
                result.get("reason") or "Planner found a useful external research need."
            ),
        }

    def _generic_auto_research_plan(
        self,
        *,
        message: str,
        subject_type: str,
        subject_name: str,
    ) -> dict[str, Any]:
        target_label = self._auto_research_target_label(
            message, subject_name, subject_type
        )
        query = (
            message.strip()
            if subject_type == "portfolio"
            else f"{subject_name}: {message.strip()}"
        )
        query = self._augment_event_research_query(query)
        return {
            "should_research": True,
            "query": query,
            "target_label": target_label,
            "information_needs": [
                "broader external evidence for the current low-confidence answer"
            ],
            "reason": "Low-confidence answer with thin/off-topic evidence packet.",
        }

    def _auto_research_action_summary(self, auto_research: dict[str, Any]) -> str:
        target_label = str(auto_research.get("target_label") or "the missing angle")
        needs = [
            str(item)
            for item in (auto_research.get("information_needs") or [])
            if str(item).strip()
        ]
        if needs:
            compact_needs = "; ".join(needs[:2])
            return f"Started targeted research for {target_label}. Looking for: {compact_needs}."
        return f"Started targeted research for {target_label}."

    def _auto_research_skip_summary(self, auto_research: dict[str, Any]) -> str:
        target_label = str(auto_research.get("target_label") or "the missing angle")
        reason = str(auto_research.get("reason") or "")
        if reason == "duplicate_recent_research":
            return f"Skipped new research for {target_label}; a matching research pass already exists from the last 12 hours."
        if reason == "research_artifact_query_blocked":
            return f"Skipped research for {target_label}; the query pointed at an internal research artifact, not an external market question."
        if reason == "empty_research_query":
            return (
                f"Skipped research for {target_label}; the normalized query was empty."
            )
        return f"Skipped research for {target_label}: {reason or 'not started'}."

    async def _should_broaden_research(
        self,
        *,
        message: str,
        subject_type: str,
        subject_name: str,
        packet_context: dict[str, Any],
        reasoning_result: dict[str, Any],
    ) -> bool:
        direct = len(packet_context.get("direct_evidence") or [])
        connected = len(packet_context.get("connected_evidence") or [])
        historical = len(packet_context.get("historical_evidence") or [])
        contradiction = len(packet_context.get("contradiction_evidence") or [])
        coverage = packet_context.get("coverage") or {}
        compact_reasoning = compact_reasoning_result(reasoning_result)
        focus_gap = self._query_focus_gap(
            message=message, packet_context=packet_context
        )

        if focus_gap["should_broaden"]:
            return True

        try:
            result = await asyncio.wait_for(
                call_llm_json(
                    system_prompt=(
                        "Decide whether Prophet should broaden research automatically for the user's current question. "
                        "Return should_research=true when the current packet is thin or off-topic for the user question "
                        "and a real external research pass would likely improve the answer materially. "
                        "Do not require explicit command phrasing."
                    ),
                    user_prompt=json.dumps(
                        {
                            "message": message,
                            "subject_type": subject_type,
                            "subject_name": subject_name,
                            "evidence_counts": {
                                "direct": direct,
                                "connected": connected,
                                "historical": historical,
                                "contradiction": contradiction,
                            },
                            "coverage": {
                                "coverage_score": coverage.get("coverage_score"),
                                "trusted_source_count": coverage.get(
                                    "trusted_source_count"
                                ),
                                "official_source_count": coverage.get(
                                    "official_source_count"
                                ),
                            },
                            "query_focus_gap": focus_gap,
                            "reasoning_result": compact_reasoning,
                        },
                        ensure_ascii=True,
                        default=str,
                    ),
                    schema=AUTO_RESEARCH_SCHEMA,
                    timeout_seconds=4,
                ),
                timeout=4,
            )
            return bool(result.get("should_research"))
        except Exception as exc:
            import logging

            logging.getLogger(__name__).warning(
                "Research-broadening check skipped after LLM failure: %s",
                self._compact_exception(exc),
            )
            return False

    def _query_focus_gap(
        self, *, message: str, packet_context: dict[str, Any]
    ) -> dict[str, Any]:
        stopwords = {
            "a",
            "an",
            "and",
            "are",
            "as",
            "at",
            "be",
            "because",
            "but",
            "by",
            "for",
            "from",
            "have",
            "how",
            "i",
            "if",
            "in",
            "into",
            "is",
            "it",
            "its",
            "me",
            "my",
            "of",
            "on",
            "or",
            "our",
            "please",
            "should",
            "tell",
            "than",
            "that",
            "the",
            "their",
            "them",
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
        tokens = re.findall(r"[A-Za-z0-9][A-Za-z0-9+./-]*", (message or "").lower())
        focus_terms: list[str] = []
        for token in tokens:
            if token in stopwords:
                continue
            if len(token) <= 2:
                continue
            focus_terms.append(token)
        # keep the most specific tail terms for follow-up questions like
        # "what about robotaxi and humanoid robots?"
        focus_terms = list(dict.fromkeys(focus_terms))[-8:]
        evidence_text = " ".join(
            str(item.get("text") or "")
            for layer in (
                packet_context.get("direct_evidence") or [],
                packet_context.get("connected_evidence") or [],
                packet_context.get("historical_evidence") or [],
                packet_context.get("contradiction_evidence") or [],
            )
            for item in layer
        ).lower()
        if not focus_terms:
            return {
                "focus_terms": [],
                "covered_terms": [],
                "missing_terms": [],
                "coverage_ratio": 1.0,
                "should_broaden": False,
            }

        covered_terms = [term for term in focus_terms if term in evidence_text]
        missing_terms = [term for term in focus_terms if term not in evidence_text]
        coverage_ratio = len(covered_terms) / max(len(focus_terms), 1)
        should_broaden = len(missing_terms) >= 2 and coverage_ratio < 0.6
        return {
            "focus_terms": focus_terms,
            "covered_terms": covered_terms,
            "missing_terms": missing_terms,
            "coverage_ratio": coverage_ratio,
            "should_broaden": should_broaden,
        }

    def _auto_research_target_label(
        self, message: str, subject_name: str, subject_type: str
    ) -> str:
        cleaned = " ".join(re.sub(r"\s+", " ", message).strip().split())
        if cleaned and cleaned != subject_name:
            focus_tokens = self._condense_research_focus_tokens(cleaned)
            if subject_type == "portfolio":
                return focus_tokens or "portfolio angle"
            if focus_tokens:
                return f"{subject_name}: {focus_tokens}"
        if subject_type == "portfolio":
            return "portfolio question"
        return subject_name

    def _condense_research_focus_tokens(self, message: str) -> str:
        compact = " ".join((message or "").split()).strip(" .:")
        if not compact:
            return ""
        compact = re.sub(
            r"(?i)^(what do you think about|thoughts on|can you analyze|please analyze|can you research|please research|look at|research|about)\s+",
            "",
            compact,
        ).strip(" .:")
        return self._truncate_research_title(compact, limit=80)

    async def _llm_operating_answer(self, payload: dict[str, object]) -> dict[str, str]:
        query_type = str(payload.get("query_type") or "operating_context")
        deterministic_first = {
            "benchmark",
            "entity_overview",
            "knowledge_status",
            "lessons",
            "performance_attribution",
            "portfolio_state",
            "portfolio_lookahead",
            "research_start",
            "research_status",
            "review_queue",
            "shadow_status",
            "trusted_source_detail",
            "trusted_sources",
        }
        if query_type in deterministic_first:
            deterministic = self._deterministic_operating_answer(payload)
            if deterministic is not None:
                return deterministic
        try:
            result = await asyncio.wait_for(
                call_llm_json(
                    system_prompt=(
                        "You are the Prophet operating agent. "
                        "Answer the user using the provided deterministic operating context only. "
                        "Do not invent facts not present in the payload. "
                        "If source records are present, explicitly name them instead of only reporting counts. "
                        "If shadow or lesson records are present, compare them to the actual portfolio baseline explicitly. "
                        "Be concise and directly useful."
                    ),
                    user_prompt=json.dumps(payload, ensure_ascii=True, default=str),
                    schema=OPERATING_RESPONSE_SCHEMA,
                ),
                timeout=8,
            )
            message = str(result["assistant_message"]).strip()
            if self._looks_like_failed_operating_answer({"assistant_message": message}):
                deterministic = self._deterministic_operating_answer(payload)
                if deterministic is not None:
                    return deterministic
            return {"assistant_message": message}
        except Exception as exc:
            import logging

            logging.getLogger(__name__).warning(
                "Operating answer LLM skipped after failure: %s",
                self._compact_exception(exc),
            )
            deterministic = self._deterministic_operating_answer(payload)
            if deterministic is not None:
                return deterministic
            if query_type in {"conversation", "smalltalk"}:
                return {
                    "assistant_message": (
                        "I’m here. Ask about your portfolio, sources, positions, theses, or what changed."
                    )
                }
            return {
                "assistant_message": "I could not summarize the current operating state cleanly."
            }

    _OPERATING_ANSWER_HANDLERS: dict[str, str] = {
        "trusted_sources": "_operating_answer_trusted_sources",
        "trusted_source_detail": "_operating_answer_trusted_source_detail",
        "benchmark": "_operating_answer_benchmark",
        "shadow_status": "_operating_answer_shadow_status",
        "lessons": "_operating_answer_lessons",
        "review_queue": "_operating_answer_review_queue",
        "portfolio_lookahead": "_operating_answer_portfolio_lookahead",
        "entity_overview": "_operating_answer_entity_overview",
        "knowledge_status": "_operating_answer_knowledge_status",
        "research_status": "_operating_answer_research_status",
        "research_start": "_operating_answer_research_start",
        "performance_attribution": "_operating_answer_performance_attribution",
        "portfolio_state": "_operating_answer_portfolio_state",
    }

    def _deterministic_operating_answer(
        self, payload: dict[str, object]
    ) -> dict[str, str] | None:
        """Route a deterministic operating answer to its query-type handler.

        Each handler owns one query type and returns None when it cannot
        answer deterministically, so an unknown type falls through to None.
        """
        query_type = str(payload.get("query_type") or "operating_context")
        handler_name = self._OPERATING_ANSWER_HANDLERS.get(query_type)
        if handler_name is None:
            return None
        return getattr(self, handler_name)(payload)

    def _operating_answer_trusted_sources(
        self, payload: dict[str, object]
    ) -> dict[str, str] | None:
        trusted = payload.get("trusted_sources") or {}
        count = int(trusted.get("count", 0))
        sources = trusted.get("sources", [])
        if count > 0 and isinstance(sources, list):
            labels = [
                f"{item.get('name')} ({item.get('source_type')})"
                for item in sources[:6]
                if isinstance(item, dict) and item.get("name")
            ]
            if labels:
                return {
                    "assistant_message": (
                        "Your trusted sources right now are: "
                        + "; ".join(labels)
                        + f". There {'is' if count == 1 else 'are'} {count} trusted source{'s' if count != 1 else ''} saved."
                    )
                }
        return {
            "assistant_message": (
                "I checked the current trusted-source catalog. "
                f"There {'is' if count == 1 else 'are'} {count} trusted source{'s' if count != 1 else ''} saved right now."
            )
        }

    def _operating_answer_trusted_source_detail(
        self, payload: dict[str, object]
    ) -> dict[str, str] | None:
        trusted = payload.get("trusted_sources") or {}
        sources = trusted.get("sources", [])
        if (
            isinstance(sources, list)
            and len(sources) == 1
            and isinstance(sources[0], dict)
        ):
            source = sources[0]
            label = f"{source.get('name')} ({source.get('source_type')})"
            recent_items = source.get("recent_items") or []
            recent_titles = [
                str(item.get("title"))
                for item in recent_items[:3]
                if isinstance(item, dict) and item.get("title")
            ]
            detail = (
                "Recent activity: " + "; ".join(recent_titles) + "."
                if recent_titles
                else source.get("description")
                or source.get("url")
                or "No extra detail is stored yet."
            )
            return {"assistant_message": f"That trusted source is {label}. {detail}"}
        if isinstance(sources, list) and sources:
            labels = []
            for item in sources[:6]:
                if not isinstance(item, dict) or not item.get("name"):
                    continue
                recent_items = item.get("recent_items") or []
                recent_hint = ""
                if recent_items and isinstance(recent_items, list):
                    latest = recent_items[0]
                    if isinstance(latest, dict) and latest.get("title"):
                        recent_hint = f" — latest: {latest.get('title')}"
                labels.append(
                    f"{item.get('name')} ({item.get('source_type')}){recent_hint}"
                )
            return {
                "assistant_message": (
                    "There is more than one trusted source saved. Recent activity by source: "
                    + "; ".join(labels)
                    + "."
                )
            }
        return {
            "assistant_message": "There is no trusted source detail to follow up on yet."
        }

    def _operating_answer_benchmark(
        self, payload: dict[str, object]
    ) -> dict[str, str] | None:
        benchmark = str(
            (payload.get("benchmark") or {}).get("default_benchmark_ticker") or "n/a"
        )
        return {"assistant_message": f"Your active benchmark baseline is {benchmark}."}

    def _operating_answer_shadow_status(
        self, payload: dict[str, object]
    ) -> dict[str, str] | None:
        status = payload.get("shadow_status") or {}
        recent = status.get("recent_experiments") or []
        if not isinstance(recent, list) or not recent:
            return {
                "assistant_message": (
                    "There are no shadow runs saved yet. Once a shadow experiment is created or triggered, I can compare it against the real portfolio baseline and summarize what it learned."
                )
            }
        labels: list[str] = []
        for item in recent[:4]:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "Unnamed shadow")
            run_status = str(item.get("run_status") or "unknown")
            alpha = item.get("alpha")
            if isinstance(alpha, (int, float)):
                labels.append(f"{name} ({run_status}, alpha {float(alpha):+.2%})")
            else:
                labels.append(f"{name} ({run_status})")
        return {
            "assistant_message": (
                f"There are {status.get('count', 0)} recent shadow experiments, with {status.get('queued_count', 0)} queued, {status.get('running_count', 0)} running, {status.get('completed_count', 0)} completed, and {status.get('failed_count', 0)} failed. "
                "Recent runs: "
                + "; ".join(labels)
                + f". Shadow-derived lessons on hand: {len(status.get('recent_shadow_lessons') or [])}."
            )
        }

    def _operating_answer_lessons(
        self, payload: dict[str, object]
    ) -> dict[str, str] | None:
        lesson_data = payload.get("lessons") or {}
        recent = lesson_data.get("recent_lessons") or []
        if not isinstance(recent, list) or not recent:
            return {
                "assistant_message": (
                    "There are no stored lessons yet. Decision reviews and completed shadow outcomes create inspectable observations; repeated shadow families are required before a policy lesson can be validated."
                )
            }
        labels: list[str] = []
        for item in recent[:4]:
            if not isinstance(item, dict):
                continue
            prefix = "Shadow" if item.get("from_shadow") else "Lesson"
            maturity = item.get("maturity_status")
            suffix = f" ({maturity})" if item.get("from_shadow") and maturity else ""
            labels.append(f"{prefix}: {item.get('title')}{suffix}")
        return {
            "assistant_message": (
                f"Prophet has {lesson_data.get('count', 0)} stored lessons, including {lesson_data.get('shadow_lesson_count', 0)} from shadow experiments. "
                "Recent lessons: " + "; ".join(labels) + "."
            )
        }

    def _operating_answer_review_queue(
        self, payload: dict[str, object]
    ) -> dict[str, str] | None:
        review = payload.get("review_queue") or {}
        top_items = review.get("top_items") or []
        if not isinstance(top_items, list) or not top_items:
            return {"assistant_message": "The review queue is currently clear."}
        labels: list[str] = []
        for item in top_items[:4]:
            if not isinstance(item, dict):
                continue
            labels.append(f"{item.get('item_label')} ({item.get('item_type')})")
        return {
            "assistant_message": (
                f"There are {review.get('count', 0)} active review items. Top priorities: "
                + "; ".join(labels)
                + "."
            )
        }

    def _operating_answer_portfolio_lookahead(
        self, payload: dict[str, object]
    ) -> dict[str, str] | None:
        lookahead = payload.get("portfolio_lookahead") or {}
        items = lookahead.get("attention_items") or []
        as_of = str(lookahead.get("as_of") or "")
        horizon_end = str(lookahead.get("horizon_end") or "")
        if isinstance(items, list) and items:
            lines: list[str] = []
            heading = "Portfolio lookahead"
            if as_of or horizon_end:
                heading += (
                    "\nWindow: "
                    + (self._format_lookahead_time(as_of) if as_of else "now")
                    + " to "
                    + (
                        self._format_lookahead_time(horizon_end)
                        if horizon_end
                        else "the selected horizon"
                    )
                )
            lines.append(heading)
            for index, item in enumerate(items[:6], start=1):
                if not isinstance(item, dict):
                    continue
                ticker = str(item.get("ticker") or "Portfolio")
                title = str(item.get("title") or item.get("event_type") or "watch item")
                due = str(item.get("due_at") or "")
                why = str(item.get("why_it_matters") or "").strip()
                action = str(item.get("if_it_fires") or "").strip()
                countdown = item.get("countdown_seconds")
                source = str(item.get("source") or "")
                timing = (
                    f"When: {self._format_lookahead_time(due)}"
                    + (
                        f" ({self._format_lookahead_countdown(countdown)} remaining)"
                        if countdown is not None
                        else ""
                    )
                    if due
                    else (
                        "Timing gap: no stored event date yet; resolve the calendar before trusting this as a next-week agenda item."
                        if source == "active_watch_date_missing"
                        else "Timing: no date stored."
                    )
                )
                lines.append(
                    f"\n{index}. {ticker} - {title}\n"
                    f"{timing}"
                    + (f"\nWhy it matters: {why}" if why else "")
                    + self._format_investment_lens(item.get("investment_lens"))
                    + (f"\nIf it fires: {action}" if action else "")
                )
            return {"assistant_message": "\n".join(lines)}
        research = lookahead.get("research") if isinstance(lookahead, dict) else {}
        query = str((research or {}).get("query") or "")
        reason = str((research or {}).get("reason") or "no dated catalysts found")
        return {
            "assistant_message": (
                "I checked the active watcher/deadline and stored-event layer for the lookahead window, but did not find a confirmed dated catalyst yet. "
                + (
                    f"I started a live catalyst scan with query: {query}."
                    if (research or {}).get("started")
                    else f"The next useful step is a live portfolio catalyst scan. Query: {query}. Status: {reason}."
                )
            )
        }

    def _operating_answer_entity_overview(
        self, payload: dict[str, object]
    ) -> dict[str, str] | None:
        overview = payload.get("entity_overview") or {}
        subject_name = str(overview.get("subject_name") or "This tracked name")
        positions = overview.get("positions") or []
        profile_data = overview.get("profile") or {}
        accepted = overview.get("accepted_state") or {}
        if isinstance(positions, list) and positions and isinstance(positions[0], dict):
            primary = positions[0]
            list_type = str(primary.get("list_type") or "tracked")
            quantity = float(primary.get("quantity") or 0.0)
            price = float(primary.get("current_price") or 0.0)
            market_value = float(primary.get("market_value") or 0.0)
            stance = accepted.get("current_stance")
            summary = ""
            if accepted:
                summary = accepted.get("current_thesis_summary") or profile_data.get(
                    "executive_summary"
                )
            if summary and stance and stance != "no_view":
                return {
                    "assistant_message": (
                        f"{subject_name} is currently in your {list_type}. "
                        f"You hold {quantity:g} shares at about ${price:.2f}, worth about ${market_value:.2f}. "
                        f"The current accepted view is {stance}. {summary}"
                    )
                }
            return {
                "assistant_message": (
                    f"{subject_name} is currently in your {list_type}. "
                    f"You hold {quantity:g} shares at about ${price:.2f}, worth about ${market_value:.2f}. "
                    "Prophet does not yet have substantive research coverage saved for it, so the next useful step is to ingest filings, earnings material, source claims, or your own thesis notes."
                )
            }
        return {
            "assistant_message": (
                f"{subject_name} is tracked, but Prophet does not yet have substantive saved research coverage for it. "
                "Add evidence or notes so the profile can move beyond no_view."
            )
        }

    def _operating_answer_knowledge_status(
        self, payload: dict[str, object]
    ) -> dict[str, str] | None:
        status = payload.get("knowledge_status") or {}
        subject_name = str(status.get("subject_name") or "the current subject")
        terms = status.get("query_terms") or []
        direct_active = int(status.get("direct_active_count") or 0)
        direct_deprecated = int(status.get("direct_deprecated_count") or 0)
        direct_matches = status.get("direct_term_matches") or []
        matching_active = status.get("matching_active_nodes") or []
        matching_deprecated = status.get("matching_deprecated_nodes") or []
        missing_terms = status.get("missing_terms") or []
        checked_at = status.get("searched_at")
        match_lines: list[str] = []
        for item in (direct_matches if isinstance(direct_matches, list) else [])[:3]:
            if isinstance(item, dict) and item.get("text"):
                match_lines.append(f"{item.get('type')}: {item.get('text')}")
        if not match_lines:
            for item in (matching_active if isinstance(matching_active, list) else [])[
                :3
            ]:
                if isinstance(item, dict) and item.get("text"):
                    match_lines.append(f"{item.get('type')}: {item.get('text')}")
        if match_lines:
            return {
                "assistant_message": (
                    f"I checked Knowledge for {subject_name}"
                    + (f" at {checked_at}" if checked_at else "")
                    + f". It has {direct_active} active directly linked knowledge nodes"
                    + (
                        f" and {direct_deprecated} deprecated directly linked nodes"
                        if direct_deprecated
                        else ""
                    )
                    + ". Matching active entries include: "
                    + "; ".join(match_lines)
                    + (
                        f". I did not find active matches for: {', '.join(str(term) for term in missing_terms[:4])}"
                        if isinstance(missing_terms, list) and missing_terms
                        else ""
                    )
                    + "."
                )
            }
        deprecated_hint = ""
        if isinstance(matching_deprecated, list) and matching_deprecated:
            deprecated_hint = f" I found {len(matching_deprecated)} deprecated matching node(s), so older material may have been soft-retired."
        term_text = (
            ", ".join(str(term) for term in terms[:6])
            if isinstance(terms, list)
            else ""
        )
        return {
            "assistant_message": (
                f"I checked Knowledge for {subject_name}"
                + (f" at {checked_at}" if checked_at else "")
                + f". It has {direct_active} active directly linked knowledge nodes"
                + (
                    f" and {direct_deprecated} deprecated directly linked nodes"
                    if direct_deprecated
                    else ""
                )
                + (
                    f", but I did not find an active node matching the search terms: {term_text}."
                    if term_text
                    else "."
                )
                + deprecated_hint
            )
        }

    def _operating_answer_research_status(
        self, payload: dict[str, object]
    ) -> dict[str, str] | None:
        status = payload.get("research_status") or {}
        if not status.get("research_provider_configured"):
            return {
                "assistant_message": (
                    "Automation is available, but broader web research is not configured yet because the external research provider is missing. "
                    f"There are {status.get('open_question_count', 0)} open research questions and {status.get('pending_evidence_count', 0)} pending evidence items."
                )
            }
        latest_item = status.get("latest_item") or {}
        if isinstance(latest_item, dict) and latest_item.get("title"):
            processed_text = (
                "It has already been processed into the evidence graph."
                if latest_item.get("is_processed")
                else "It is still waiting on extraction and downstream reasoning."
            )
            subject_name = latest_item.get("subject_name")
            requested_via = latest_item.get("requested_via")
            context_bits = []
            if subject_name:
                context_bits.append(f"subject {subject_name}")
            if requested_via:
                context_bits.append(f"requested from '{requested_via}'")
            context_suffix = f" ({'; '.join(context_bits)})" if context_bits else ""
            return {
                "assistant_message": (
                    "Background research is active. "
                    f"The latest research item is '{latest_item.get('title')}'{context_suffix}. "
                    f"{processed_text} "
                    f"There are {status.get('open_question_count', 0)} open research questions and {status.get('pending_evidence_count', 0)} pending evidence items."
                )
            }
        return {
            "assistant_message": (
                "Research automation is enabled. "
                f"There are {status.get('open_question_count', 0)} open research questions and {status.get('pending_evidence_count', 0)} pending evidence items."
            )
        }

    def _operating_answer_research_start(
        self, payload: dict[str, object]
    ) -> dict[str, str] | None:
        started = payload.get("research_start") or {}
        if started.get("started"):
            loop_detail = started.get("loop_detail") or {}
            subject_name = started.get("focus_label")
            if isinstance(loop_detail, dict):
                subject_name = subject_name or loop_detail.get("subject_name")
            suffix = ""
            if subject_name:
                suffix = f" for {subject_name}"
            return {
                "assistant_message": (
                    "I started a research pass from chat"
                    + suffix
                    + (
                        " and processed the resulting evidence into the operating loop."
                        if started.get("processed")
                        else " and ingested a new evidence item for follow-on extraction."
                    )
                )
            }
        reason = started.get("reason") or "unknown"
        if reason == "research_provider_not_configured":
            return {
                "assistant_message": (
                    "I could not start broader research because the external research provider is not configured yet."
                )
            }
        return {
            "assistant_message": "I attempted to start research, but no useful result came back."
        }

    def _operating_answer_performance_attribution(
        self, payload: dict[str, object]
    ) -> dict[str, str] | None:
        attribution = payload.get("performance_attribution") or {}
        items = attribution.get("items") or []
        if not isinstance(items, list) or not items:
            return {
                "assistant_message": (
                    "I could not calculate a cash-flow-aligned performance attribution from the current holdings and price history. "
                    "Check the Risk report for missing-price or corporate-action exclusions."
                )
            }
        gain = float(attribution.get("gain") or 0.0)
        return_pct = attribution.get("return_pct")
        benchmark_return = attribution.get("benchmark_return_pct")
        active_return = attribution.get("active_return_pct")
        benchmark_ticker = str(attribution.get("benchmark_ticker") or "benchmark")
        period_start = str(attribution.get("period_start") or "")[:10]
        as_of = str(attribution.get("as_of") or "")[:10]
        drags = [
            item
            for item in items
            if isinstance(item, dict) and float(item.get("gain") or 0.0) < 0
        ][:4]
        gains = [
            item
            for item in items
            if isinstance(item, dict) and float(item.get("gain") or 0.0) > 0
        ][-3:]
        gains.reverse()

        def describe(item: dict[str, object]) -> str:
            item_gain = float(item.get("gain") or 0.0)
            contribution = float(item.get("contribution_pct") or 0.0)
            capital_return = item.get("capital_return_pct")
            capital_text = (
                f", {float(capital_return):+.1f}% on capital"
                if isinstance(capital_return, (int, float))
                else ""
            )
            return (
                f"{item.get('ticker')} {item_gain:+,.2f} dollars "
                f"({contribution:+.2f} return points{capital_text})"
            )

        lines = [
            (
                f"From {period_start} through {as_of}, the invested holdings "
                f"{'lost' if gain < 0 else 'gained'} ${abs(gain):,.2f}"
                + (
                    f" ({float(return_pct):+.2f}%)"
                    if isinstance(return_pct, (int, float))
                    else ""
                )
                + "."
            )
        ]
        if isinstance(benchmark_return, (int, float)):
            relative = "."
            if isinstance(active_return, (int, float)):
                relative = (
                    f", so the holdings underperformed by {abs(float(active_return)):.2f} percentage points."
                    if float(active_return) < 0
                    else f", for {float(active_return):+.2f} points of relative performance."
                )
            lines.append(
                f"{benchmark_ticker} returned {float(benchmark_return):+.2f}% over the aligned window{relative}"
            )
        if drags:
            lines.append(
                "Largest drags: " + "; ".join(describe(item) for item in drags) + "."
            )
        if gains:
            lines.append(
                "Offsets: " + "; ".join(describe(item) for item in gains) + "."
            )
        lines.append(
            f"Coverage: {float(attribution.get('coverage_pct') or 0.0):.0f}% "
            f"({attribution.get('covered_positions', 0)}/{attribution.get('total_positions', 0)} securities)."
        )
        unavailable = attribution.get("unavailable_tickers") or []
        if isinstance(unavailable, list) and unavailable:
            lines.append(
                "Excluded pending complete price or corporate-action history: "
                + ", ".join(str(item) for item in unavailable)
                + "."
            )
        lines.append(
            "This is measured price-and-cash-flow attribution, not a claim about why each security moved; catalyst and thesis diagnosis should start with these largest contributors."
        )
        return {"assistant_message": "\n\n".join(lines)}

    def _operating_answer_portfolio_state(
        self, payload: dict[str, object]
    ) -> dict[str, str] | None:
        state = payload.get("portfolio_state") or {}
        top_holdings = state.get("top_holdings") or []
        labels: list[str] = []
        if isinstance(top_holdings, list):
            for item in top_holdings[:3]:
                if not isinstance(item, dict):
                    continue
                labels.append(
                    f"{item.get('ticker')} · {item.get('name')} (${float(item.get('market_value', 0.0)):.2f})"
                )
        return {
            "assistant_message": (
                "I checked the current portfolio state. "
                f"It has {state.get('holdings_count', 0)} holdings, "
                f"${float(state.get('total_market_value', 0.0)):.2f} of holding market value, and "
                f"${float(state.get('remaining_buying_power', 0.0)):.2f} of remaining buying power."
                + (" Top holdings: " + "; ".join(labels) + "." if labels else "")
            )
        }

    @staticmethod
    def _format_investment_lens(value: object) -> str:
        if not isinstance(value, dict) or not value:
            return ""
        parts = [
            str(value.get("expectation_delta") or "").strip(),
            str(value.get("market_reaction") or "").strip(),
            str(value.get("portfolio_transmission") or "").strip(),
        ]
        compact = " ".join(part for part in parts if part)
        best_next = str(value.get("best_next_check") or "").strip()
        output = f"\nInvestment read: {compact}" if compact else ""
        if best_next:
            output += f"\nBest next check: {best_next}"
        return output

    @staticmethod
    def _format_lookahead_time(value: str) -> str:
        if not value:
            return ""
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value
        display = parsed.strftime("%b %-d, %Y %H:%M %Z").strip()
        return f"{display} ({parsed.date().isoformat()})"

    @staticmethod
    def _format_lookahead_countdown(value: Any) -> str:
        try:
            seconds = max(0, int(value))
        except (TypeError, ValueError):
            return "unknown"
        days, remainder = divmod(seconds, 86400)
        hours, remainder = divmod(remainder, 3600)
        minutes, _ = divmod(remainder, 60)
        if days:
            return f"{days}d {hours}h"
        if hours:
            return f"{hours}h {minutes}m"
        return f"{minutes}m"

    def _session_title_from_message(self, message: str) -> str:
        collapsed = " ".join(message.strip().split())
        if not collapsed:
            return "New chat"
        return collapsed[:72]

    async def _subject_name(self, subject_id: UUID, subject_type: str) -> str:
        if subject_type == "portfolio":
            return PORTFOLIO_SUBJECT_NAME
        if subject_type == "theme":
            theme = (
                await self.session.execute(select(Theme).where(Theme.id == subject_id))
            ).scalar_one_or_none()
            return theme.name if theme is not None else str(subject_id)
        if subject_type == "position":
            position = (
                await self.session.execute(
                    select(Position).where(Position.id == subject_id)
                )
            ).scalar_one_or_none()
            if position is None:
                return str(subject_id)
            security = (
                await self.session.execute(
                    select(Security).where(Security.id == position.security_id)
                )
            ).scalar_one_or_none()
            if security is None:
                return str(subject_id)
            entity = (
                await self.session.execute(
                    select(Entity).where(Entity.id == security.entity_id)
                )
            ).scalar_one_or_none()
            return (
                security.ticker
                if entity is None
                else f"{security.ticker} · {entity.name}"
            )
        entity = (
            await self.session.execute(select(Entity).where(Entity.id == subject_id))
        ).scalar_one_or_none()
        if entity is not None:
            security = (
                (
                    await self.session.execute(
                        select(Security)
                        .where(Security.entity_id == entity.id)
                        .order_by(Security.ticker.asc())
                    )
                )
                .scalars()
                .first()
            )
            if security is None:
                return entity.name
            if not entity.name or entity.name.lower() == security.ticker.lower():
                return security.ticker
            return f"{security.ticker} · {entity.name}"
        return str(subject_id)
