from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from investos.config import settings
from investos.core.storage import LocalStorage
from investos.models.entity import Entity
from investos.models.knowledge import Claim, Event, Fact
from investos.models.theme import Theme
from investos.schemas.agent import AgentContextCandidateResponse, AgentTurnRequest
from investos.services.agent import PORTFOLIO_SUBJECT_ID, AgentService
from investos.services.reasoning import ReasoningService
from investos.services.research import ResearchSearchResult, ResearchService
from investos.services.retrieval import RetrievalService
from investos.services.runtime_settings import RuntimeSettings, RuntimeSettingsStore
from tests.test_investor_workflows import source_records, workflow_session


@pytest.fixture(autouse=True)
def isolated_scope_runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "STORAGE_DIR", str(tmp_path))
    runtime = RuntimeSettings()
    runtime.llm.api_key = "synthetic-scope-test"
    monkeypatch.setattr(RuntimeSettingsStore, "load", lambda: runtime)
    storage = LocalStorage(str(tmp_path))
    for module in ("agent", "ingestion", "chat_attachments"):
        monkeypatch.setattr(f"investos.services.{module}.LocalStorage", lambda: storage)


@pytest.mark.parametrize(
    "kind,auto_execute,fresh",
    [("theme", False, True), ("entity", False, True), ("theme", True, False)],
)
async def test_unmatched_question_keeps_scope_through_real_retrieval_and_followup(
    workflow_session, monkeypatch, kind, auto_execute, fresh
):
    session = workflow_session
    raw, source_item = await source_records(session)
    topic = f"Zythera{uuid4().hex[:8]} Brinel Memory HBM qualification partnership"
    fact = Fact(
        source_item_id=source_item.id,
        statement=f"{topic}: the companies announced a qualification collaboration.",
        fact_type="partnership",
        confidence=0.8,
        tier="hard_fact",
        importance="high",
        directness="primary",
        novelty="confirming",
        contradiction_role="neutral",
    )
    session.add(fact)
    await session.flush()
    initial_entities = await session.scalar(select(func.count(Entity.id)))
    initial_themes = await session.scalar(select(func.count(Theme.id)))
    hint = {
        "subject_kind": kind,
        "search_text": topic,
        "reason": "Named research scope",
    }
    intent = AsyncMock(
        side_effect=[
            {
                "route": "analyze",
                "requires_fresh_research": fresh,
                "subject_hint": hint,
            },
            {
                "route": "continue",
                "requires_fresh_research": fresh,
                "subject_hint": {"subject_kind": "none"},
            },
        ]
    )
    monkeypatch.setattr(AgentService, "_classify_turn_intent", intent)
    # Force the actual planner's bounded fallback; do not replace its result.
    monkeypatch.setattr(
        "investos.services.agent.call_llm_json", AsyncMock(side_effect=TimeoutError())
    )
    monkeypatch.setattr(
        AgentService, "_maybe_conversational_handoff", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        AgentService, "_dispatch_dynamic_subagents", AsyncMock(return_value={})
    )
    monkeypatch.setattr(
        AgentService, "_maybe_auto_research_after_gap", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        ReasoningService, "_create_critique", AsyncMock(return_value=None)
    )
    state_update = AsyncMock(
        side_effect=AssertionError("A question cannot promote portfolio state")
    )
    actions = AsyncMock(
        side_effect=AssertionError(
            "A question cannot execute against the context anchor"
        )
    )
    monkeypatch.setattr(ReasoningService, "_update_conclusion_state", state_update)
    monkeypatch.setattr(AgentService, "_execute_actions", actions)
    packets = []

    async def analyze(self, context, **kwargs):
        packets.append(context)
        assert str(fact.id) in [str(row["id"]) for row in context["direct_evidence"]]
        assert context["research_plan"]["question_scope"] == hint
        return {
            "stance": "uncertain",
            "confidence_band": "low",
            "thesis_summary": "A source describes a qualification collaboration.",
            "reasoning": "The source describes collaboration, not realized financial results.",
            "supporting_evidence_ids": [str(fact.id)],
            "contradicting_evidence_ids": [],
            "material_assertions": [],
        }

    monkeypatch.setattr(ReasoningService, "_reason_with_llm", analyze)

    async def unchanged(self, payload, resolved, context, result, lenses):
        return result

    async def answer_plan(self, **kwargs):
        return self._fast_path_orchestration(
            kwargs["payload"], kwargs["reasoning_result"]
        )

    monkeypatch.setattr(AgentService, "_synthesize_and_refine", unchanged)
    monkeypatch.setattr(AgentService, "_plan_agent_actions", answer_plan)
    search = AsyncMock(return_value=ResearchSearchResult(True, "ok", topic, []))
    monkeypatch.setattr(ResearchService, "search", search)
    portfolio_checks = AsyncMock(
        side_effect=AssertionError("Focused research became a holdings recap")
    )
    monkeypatch.setattr(AgentService, "_portfolio_fresh_checks", portfolio_checks)
    service = AgentService(session)
    response = await service.handle_turn(
        AgentTurnRequest(
            message=(
                f"Investigate {topic} and the recent leadership news. Do not change portfolio state."
                if fresh
                else f"Describe what the stored sources establish about {topic}."
            ),
            auto_execute=auto_execute,
        )
    )
    assert (
        response.subject_id == PORTFOLIO_SUBJECT_ID
    )  # Context anchor, not question scope.
    assert response.subject_name == topic
    assert response.research_receipt.question_scope == hint
    assert response.research_receipt.holdings_count is None
    assert response.research_receipt.holding_search_coverage == []
    if fresh:
        assert search.await_args.kwargs["query"] == topic
    assert packets[0]["research_plan"]["coverage_scope"] == "question_specific"
    assert (
        packets[0]["research_plan"]["portfolio_context_role"]
        == "secondary_read_through"
    )
    # Persisted scope survives the real history path and a new service instance.
    listing = await service.list_conversations()
    assert (
        next(
            row
            for row in listing.conversations
            if row.session_id == response.session_id
        ).subject_name
        == topic
    )
    await AgentService(session).handle_turn(
        AgentTurnRequest(
            session_id=response.session_id,
            message="What would disconfirm that?",
            auto_execute=auto_execute,
        )
    )
    assert search.await_count == (2 if fresh else 0)
    if fresh:
        assert search.await_args.kwargs["query"] == topic
    state_update.assert_not_awaited()
    actions.assert_not_awaited()
    portfolio_checks.assert_not_awaited()
    assert await session.scalar(select(func.count(Entity.id))) == initial_entities
    assert await session.scalar(select(func.count(Theme.id))) == initial_themes


async def test_semantic_theme_does_not_narrow_to_an_incidental_held_company(
    monkeypatch,
):
    service = AgentService(None)
    candidate = AgentContextCandidateResponse(
        subject_id=uuid4(),
        subject_type="entity",
        subject_name="EXMPL",
        score=200,
        reason="Held company mentioned in the question",
    )
    monkeypatch.setattr(
        service, "_subject_candidates", AsyncMock(return_value=[candidate])
    )
    monkeypatch.setattr(
        service, "_subject_candidates_from_hint", AsyncMock(return_value=[])
    )
    resolved = await service.resolve_context(
        message="Compare suppliers across the infrastructure chain, including EXMPL",
        subject_id=None,
        subject_type=None,
        subject_hint={
            "subject_kind": "theme",
            "search_text": "Infrastructure supplier competition",
        },
    )
    assert resolved.subject_id == PORTFOLIO_SUBJECT_ID
    assert candidate in resolved.candidates
    assert "supporting context" in resolved.resolution_reason
    monkeypatch.setattr(service, "_subject_name", AsyncMock(return_value="EXMPL"))
    pinned = await service.resolve_context(
        message="Compare suppliers",
        subject_id=candidate.subject_id,
        subject_type="entity",
        subject_hint={"subject_kind": "theme", "search_text": "Infrastructure"},
    )
    assert pinned.subject_id == candidate.subject_id
    assert pinned.resolution_reason == "Using the manually pinned context."


async def test_entity_scope_resolves_a_known_issuer_without_a_security(
    workflow_session,
):
    entity = Entity(name=f"Synthetic issuer {uuid4()}", entity_type="company")
    workflow_session.add(entity)
    await workflow_session.flush()
    resolved = await AgentService(workflow_session).resolve_context(
        message=entity.name,
        subject_id=None,
        subject_type=None,
        subject_hint={"subject_kind": "entity", "search_text": entity.name},
    )
    assert resolved.subject_id == entity.id and resolved.subject_type == "entity"


def test_question_planner_cannot_replace_a_classified_theme_with_holdings_coverage():
    service = AgentService(None)
    hint = {
        "subject_kind": "theme",
        "search_text": "Synthetic supply collaboration",
        "reason": "Comparison question",
    }
    plan = service._normalize_question_research_plan(
        {
            "research_objective": "Compare supplier relationships",
            "retrieval_query": hint["search_text"],
            "research_mode": "focused_external",
            "coverage_scope": "portfolio_wide",
            "portfolio_context_role": "primary_evidence",
        },
        message="Compare suppliers without changing my portfolio.",
        subject_name="Portfolio",
        subject_type="portfolio",
        freshness_requirement={"subject_hint": hint, "requires_fresh_research": True},
    )
    assert plan["question_scope"] == hint
    assert plan["coverage_scope"] == "question_specific"
    assert plan["portfolio_context_role"] == "secondary_read_through"


@pytest.mark.parametrize("node_type", ["fact", "claim", "event"])
async def test_older_topic_evidence_outranks_newer_generic_word_matches(
    workflow_session, node_type
):
    session = workflow_session
    _, source_item = await source_records(session)
    topic = f"Zythera{uuid4().hex[:8]} Brinel memory supply partnership"
    now = datetime.now(UTC)

    def node(statement, created_at):
        if node_type == "event":
            return Event(
                title=statement,
                description=None,
                event_type="partnership",
                created_at=created_at,
            )
        values = dict(
            statement=statement,
            source_item_id=source_item.id,
            confidence=0.8,
            tier="hard_fact",
            importance="high",
            directness="primary",
            novelty="confirming",
            contradiction_role="neutral",
            created_at=created_at,
        )
        return (
            Fact(fact_type="partnership", **values)
            if node_type == "fact"
            else Claim(claim_type="forecast", **values)
        )

    relevant = node(
        f"{topic}: the companies announced a qualification collaboration.",
        now - timedelta(days=30),
    )
    session.add_all(
        [
            relevant,
            *[
                node(f"Unrelated oil supply update {i}", now + timedelta(seconds=i))
                for i in range(8)
            ],
        ]
    )
    await session.flush()
    packet = await RetrievalService(session).retrieve_evidence(
        query=topic,
        subject_id=PORTFOLIO_SUBJECT_ID,
        subject_type="portfolio",
        max_depth=1,
    )
    assert relevant.id in packet.direct_evidence_ids
