import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import httpx
import pytest

from investos.core.prompting import compact_evidence_nodes, compact_packet_context
from investos.models.coverage import CoverageMap, UnresolvedQuestion
from investos.schemas.agent import AgentConversationEntryResponse, AgentTurnResponse
from investos.schemas.graph import GraphConnectionResponse
from investos.schemas.integrations import ResearchUsageSnapshot
from investos.services.agent import AGENT_RESPONSE_SCHEMA, AgentService
from investos.services.graph import GraphService
from investos.services.ingestion import _extract_canonical_url
from investos.services.operating_state import OperatingStateService
from investos.services.portfolio_lookahead import PortfolioLookaheadService
from investos.services.portfolio_peers import PortfolioPeerContextService
from investos.services.pruning import PruningService
from investos.services.reasoning import REASONING_SCHEMA, ReasoningService
from investos.services.reasoning_trace import ReasoningTraceService
from investos.services.research import ResearchService
from investos.services.retrieval import RetrievalService
from investos.services.review import ReviewService
from investos.workers.coverage import CoverageWorker
from investos.workers.extraction import ExtractionWorker


class _GraphDetailProbe(GraphService):
    def __init__(self):
        pass

    async def _load_node(self, node_type, node_id):
        return SimpleNamespace(
            id=node_id,
            statement="A source-backed graph fact",
            tier="high",
            created_at=datetime(2026, 7, 17, tzinfo=timezone.utc),
        )

    async def _citations_for_node(self, node_type, node):
        return []

    async def _connections_for_node(self, *, node_type, node_id):
        return []

    async def _portfolio_attachment_summary(self, **kwargs):
        return {}

    async def _node_layer(self, node_type, node):
        return "knowledge"

    async def _node_is_autonomous(self, node_type, node):
        return False

    async def _node_properties(self, node_type, node_id, node, attachment):
        return {"direct_portfolio_link": False}

    def _calculate_relevance(self, node_type, node, attachment):
        return 0.5

    def _generate_relevance_reasoning(self, *args, **kwargs):
        return "Stored as a source-backed fact."


@pytest.mark.asyncio
async def test_graph_node_detail_still_assembles_after_search_extension():
    result = await _GraphDetailProbe().get_node_detail(
        node_type="fact", node_id=uuid4()
    )

    assert result is not None
    assert result.label == "A source-backed graph fact"
    assert result.properties["direct_portfolio_link"] is False


@pytest.mark.asyncio
async def test_historical_episode_graph_detail_explains_channel_and_dates():
    episode = SimpleNamespace(
        episode_type="capex_cycle",
        name="Telecom buildout",
        description="Capacity expanded ahead of realized demand.",
        dominant_channel="Capital intensity outran end demand.",
        notes="Equity timing lagged infrastructure deployment.",
        start_time=datetime(1997, 1, 1, tzinfo=timezone.utc),
        end_time=datetime(2002, 12, 31, tzinfo=timezone.utc),
        affected_sectors=["technology"],
        affected_themes=["telecom"],
    )
    service = GraphService.__new__(GraphService)

    properties = await service._node_properties(
        "historical_episode", uuid4(), episode, {}
    )

    assert service._node_label("historical_episode", episode) == "Telecom buildout"
    assert service._node_subtitle("historical_episode", episode) == (
        "capex_cycle · 1997-2002"
    )
    assert properties["dominant_channel"] == ("Capital intensity outran end demand.")
    assert "Dominant channel" in service._node_body("historical_episode", episode)


@pytest.mark.asyncio
async def test_graph_attachment_follows_one_stored_hop_to_live_company():
    signal_id = uuid4()
    company_id = uuid4()
    query_result = MagicMock()
    query_result.scalars.return_value.all.return_value = [
        SimpleNamespace(
            source_type="market_setup_signal",
            source_id=signal_id,
            target_type="entity",
            target_id=company_id,
            relationship_type="pattern_affects",
        )
    ]
    service = GraphService.__new__(GraphService)
    service.session = SimpleNamespace(execute=AsyncMock(return_value=query_result))

    route = await service._one_hop_portfolio_connections(
        direct_connections=[
            GraphConnectionResponse(
                edge_id=uuid4(),
                direction="incoming",
                relationship_type="rhymes_with",
                confidence=0.7,
                node_id=signal_id,
                node_type="market_setup_signal",
                label="HBM capacity reallocation pattern",
            )
        ],
        tracked_position_ids=set(),
        tracked_entity_ids={company_id},
        tracked_position_labels={},
        tracked_entity_labels={company_id: "MEMB"},
    )

    assert route["linked_companies"] == ["MEMB"]
    assert route["portfolio_connection_count"] == 1
    assert route["intermediary_labels"] == ["HBM capacity reallocation pattern"]
    assert "pattern affects MEMB" in route["path_phrases"][0]


def test_fallback_coverage_questions_are_generic_not_ticker_templates():
    questions = CoverageWorker._subject_specific_questions(
        subject_name="MEMX · Tradr 2X Long MEMA Daily ETF",
        fact_count=0,
        claim_count=0,
        contradiction_count=0,
    )
    text = " ".join(str(item["question_text"]) for item in questions).lower()
    assert "operating, financial, competitive, valuation, or timing metric" in text
    assert "source would prove or disprove" in text
    assert "mema trade" not in text
    assert "post-spin balance-sheet" not in text


def test_html_canonical_origin_uses_declared_url_and_resolves_relative_paths():
    html = '<html><head><link rel="canonical" href="/original/report"></head></html>'

    assert _extract_canonical_url(html, "https://copy.example/story") == (
        "https://copy.example/original/report"
    )


def test_html_canonical_origin_falls_back_to_open_graph_then_requested_url():
    open_graph = '<meta property="og:url" content="https://origin.example/report">'

    assert _extract_canonical_url(open_graph, "https://copy.example/story") == (
        "https://origin.example/report"
    )
    assert _extract_canonical_url("<html></html>", "https://copy.example/story") == (
        "https://copy.example/story"
    )


def test_html_canonical_origin_rejects_non_http_declared_urls():
    base = "https://copy.example/story"

    assert (
        _extract_canonical_url(
            '<link rel="canonical" href="javascript:alert(1)">', base
        )
        == base
    )


class _CoverageRowsResult:
    def __init__(self, rows):
        self.rows = rows

    def scalars(self):
        return self

    def all(self):
        return list(self.rows)


class _SequenceResult(_CoverageRowsResult):
    def __init__(self, rows=None, scalar=None):
        super().__init__(rows or [])
        self.scalar = scalar

    def scalar_one(self):
        return self.scalar


class _SequenceSession:
    def __init__(self, results):
        self.results = list(results)

    async def execute(self, _statement):
        return self.results.pop(0)


class _CoverageReconcileSession:
    def __init__(self, rows):
        self.rows = rows
        self.added = []

    async def execute(self, _statement):
        return _CoverageRowsResult(self.rows)

    def add(self, item):
        self.added.append(item)


class _CoverageFallbackSession:
    def __init__(self):
        self.added = []
        self.committed = False

    async def execute(self, statement):
        sql = str(statement).lower()
        if "unresolved_questions" in sql or "missing_evidence_classes" in sql:
            raise AssertionError(
                "Fallback coverage audit must not rewrite durable gap state"
            )
        return _CoverageRowsResult([])

    def add(self, item):
        self.added.append(item)

    async def commit(self):
        self.committed = True


@pytest.mark.asyncio
async def test_coverage_question_refresh_reconciles_without_destroying_state():
    coverage_map_id = uuid4()
    created_at = datetime(2026, 6, 20, tzinfo=timezone.utc)
    answered = UnresolvedQuestion(
        coverage_map_id=coverage_map_id,
        question_text="What evidence confirms the demand mechanism?",
        urgency=2,
        status="answered",
        created_at=created_at,
    )
    investigating = UnresolvedQuestion(
        coverage_map_id=coverage_map_id,
        question_text="What would falsify the margin thesis?",
        urgency=2,
        status="investigating",
        created_at=created_at,
    )
    duplicate = UnresolvedQuestion(
        coverage_map_id=coverage_map_id,
        question_text="What would falsify the margin thesis?",
        urgency=1,
        status="open",
        created_at=created_at + timedelta(minutes=1),
    )
    stale = UnresolvedQuestion(
        coverage_map_id=coverage_map_id,
        question_text="Which old generic metric matters?",
        urgency=1,
        status="open",
        created_at=created_at,
    )
    session = _CoverageReconcileSession([answered, investigating, duplicate, stale])

    await CoverageWorker(session)._reconcile_unresolved_questions(
        coverage_map_id,
        [
            {
                "question_text": "What evidence confirms the demand mechanism?",
                "urgency": 4,
            },
            {
                "question_text": "What would falsify the margin thesis?",
                "urgency": 5,
            },
            {
                "question_text": "What primary source quantifies committed capacity?",
                "urgency": 4,
            },
        ],
    )

    assert answered.status == "answered"
    assert answered.urgency == 4
    assert investigating.status == "investigating"
    assert investigating.urgency == 5
    assert duplicate.status == "obsolete"
    assert stale.status == "obsolete"
    assert len(session.added) == 1
    assert (
        session.added[0].question_text
        == "What primary source quantifies committed capacity?"
    )


@pytest.mark.asyncio
async def test_coverage_provider_failure_does_not_materialize_fallback_questions(
    monkeypatch,
):
    coverage_map = CoverageMap(
        id=uuid4(),
        subject_id=uuid4(),
        subject_type="entity",
        evidence_class_coverage_json={},
    )
    session = _CoverageFallbackSession()

    async def ensure_coverage_map(_self, **_kwargs):
        return coverage_map

    async def fail_llm(**_kwargs):
        raise TimeoutError("provider unavailable")

    monkeypatch.setattr(
        "investos.workers.coverage.CanonicalStateService.ensure_coverage_map",
        ensure_coverage_map,
    )
    monkeypatch.setattr("investos.workers.coverage.call_llm_json", fail_llm)

    result = await CoverageWorker(session).audit_subject_coverage(
        coverage_map.subject_id,
        "entity",
        "Memory Beta Inc.",
    )

    assert result is coverage_map
    assert session.committed
    assert not any(isinstance(item, UnresolvedQuestion) for item in session.added)


@pytest.mark.asyncio
async def test_research_status_reports_total_open_count_not_preview_length():
    preview = [
        SimpleNamespace(question_text=f"Question {index}", urgency=5 - index)
        for index in range(3)
    ]
    session = _SequenceSession(
        [
            _SequenceResult(rows=[]),
            _SequenceResult(rows=preview),
            _SequenceResult(scalar=1599),
            _SequenceResult(rows=[]),
        ]
    )

    payload = await OperatingStateService(session).research_status_payload()

    assert payload["open_question_count"] == 1599
    assert len(payload["open_questions"]) == 3


@pytest.mark.asyncio
async def test_research_usage_accepts_current_tavily_key_usage_object(monkeypatch):
    class FakeResponse:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "key": {"usage": 466, "limit": None, "search_usage": 466},
                "account": {
                    "current_plan": "Researcher",
                    "plan_usage": 466,
                    "plan_limit": 1000,
                },
            }

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, *args, **kwargs):
            return FakeResponse()

    monkeypatch.setattr(
        "investos.services.research.RuntimeSettingsStore.load",
        lambda: SimpleNamespace(
            research=SimpleNamespace(
                provider_order=["searxng", "tavily"],
                searxng_base_url="",
                api_key="test-key",
            )
        ),
    )
    monkeypatch.setattr(
        "investos.services.research.httpx.AsyncClient",
        lambda **kwargs: FakeClient(),
    )
    monkeypatch.setattr(ResearchService, "recent_request_log", lambda: [])

    snapshot = await ResearchService.current_usage_snapshot()
    validated = ResearchUsageSnapshot.model_validate(snapshot)

    assert validated.key == {
        "usage": 466,
        "limit": None,
        "search_usage": 466,
    }
    assert validated.account["current_plan"] == "Researcher"


def test_timeout_fallback_is_honest_not_canned_analysis():
    result = ReasoningService(None)._timeout_fallback_reasoning(
        {
            "query_text": "how does mema look in the long run? can it hit 26% cagr?",
            "subject_name": "MEMA · Memory Alpha Corp.",
            "direct_evidence": [],
            "connected_evidence": [],
            "historical_evidence": [],
            "contradiction_evidence": [],
            "gap_flags": ["no_coverage_map"],
            "portfolio_context": {
                "top_holdings": [
                    {"ticker": "MEMA"},
                    {"ticker": "MEMB"},
                ],
            },
        }
    )
    reasoning = result["reasoning"].lower()
    assert "current read" in reasoning
    assert "stored packet is not strong enough" in reasoning
    assert "evidence state" in reasoning
    assert "mema, memb" in reasoning
    assert "best next check" in reasoning
    assert "falsifiable mechanism test" in reasoning
    assert "could not complete the live structured analyst pass" not in reasoning
    assert "local packet" not in reasoning
    assert "starting valuation" not in reasoning
    assert "normalized cycle earnings" not in reasoning
    assert result["what_would_strengthen"]
    assert result["what_would_falsify"]


def test_unconfigured_nvidia_fallback_provenance_is_not_a_model_claim(monkeypatch):
    monkeypatch.setattr(
        "investos.services.reasoning.RuntimeSettingsStore.load",
        lambda: SimpleNamespace(
            llm=SimpleNamespace(provider="nvidia_nim", api_key=None),
        ),
    )
    service = ReasoningService(None)
    result = service._fallback_reasoning(
        {
            "query_text": "why have i been losing money lately?",
            "subject_name": "Portfolio",
            "direct_evidence": [],
            "connected_evidence": [],
            "historical_evidence": [],
            "contradiction_evidence": [],
            "portfolio_context": {},
        }
    )

    assert result["fallback_reason"] == "nvidia_nim_unconfigured"
    assert service._model_used_label(result) == "fallback:nvidia_nim_unconfigured"


def test_loss_fallback_uses_cached_measured_attribution_without_inventing_cause():
    result = ReasoningService(None)._fallback_reasoning(
        {
            "query_text": "why have i been losing money lately?",
            "subject_name": "Portfolio",
            "direct_evidence": [],
            "connected_evidence": [],
            "historical_evidence": [],
            "contradiction_evidence": [],
            "portfolio_context": {
                "top_holdings": [{"ticker": "MEMA"}, {"ticker": "MEMB"}],
                "performance_attribution": {
                    "period_start": "2026-06-23T00:00:00Z",
                    "as_of": "2026-07-14T00:00:00Z",
                    "gain": -753.63,
                    "return_pct": -6.67,
                    "benchmark_ticker": "SPY",
                    "benchmark_return_pct": 2.54,
                    "active_return_pct": -9.21,
                    "items": [
                        {"ticker": "MEMX", "gain": -243.25},
                        {"ticker": "MEMA", "gain": -195.80},
                    ],
                },
            },
        }
    )

    reasoning = result["reasoning"]
    assert "lost $753.63" in reasoning
    assert "MEMX" in reasoning
    assert "SPY returned +2.54%" in reasoning
    assert "what moved the book, not why" in reasoning
    assert "Missing attribution inputs" not in reasoning


def test_configured_nvidia_failure_labels_unauthorized(monkeypatch):
    monkeypatch.setattr(
        "investos.services.reasoning.RuntimeSettingsStore.load",
        lambda: SimpleNamespace(
            llm=SimpleNamespace(provider="nvidia_nim", api_key="configured"),
        ),
    )

    assert (
        ReasoningService(None)._fallback_reason_label(
            RuntimeError("HTTP 401 Unauthorized")
        )
        == "nvidia_nim_unauthorized"
    )


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (httpx.ConnectError("connection reset"), "nvidia_nim_connection_error"),
        (httpx.ReadTimeout("read timed out"), "nvidia_nim_timeout"),
        (
            ValueError("No valid JSON object found in response from NVIDIA NIM"),
            "nvidia_nim_invalid_response",
        ),
    ],
)
def test_configured_nvidia_failure_labels_operational_category(
    monkeypatch, exc, expected
):
    monkeypatch.setattr(
        "investos.services.reasoning.RuntimeSettingsStore.load",
        lambda: SimpleNamespace(
            llm=SimpleNamespace(provider="nvidia_nim", api_key="configured"),
        ),
    )

    assert ReasoningService(None)._fallback_reason_label(exc) == expected


def test_failure_category_is_provider_agnostic(monkeypatch):
    monkeypatch.setattr(
        "investos.services.reasoning.RuntimeSettingsStore.load",
        lambda: SimpleNamespace(
            llm=SimpleNamespace(provider="codex_cli", api_key=None),
        ),
    )

    assert (
        ReasoningService(None)._fallback_reason_label(
            httpx.ConnectError("connection reset")
        )
        == "codex_cli_connection_error"
    )


def test_cached_fallback_output_is_never_reused():
    service = ReasoningService(None)
    assert not service._is_reusable_cached_output(
        {
            "is_fallback": True,
            "critique_text": "Stored evidence is thin.",
        }
    )
    assert service._is_reusable_cached_output(
        {
            "is_fallback": False,
            "critique_text": "The model answer needs stronger contradiction handling.",
        }
    )


def test_reasoning_trace_corrects_legacy_fallback_provenance():
    assert (
        ReasoningTraceService.effective_model_used(
            "nvidia_nim",
            {"is_fallback": True},
        )
        == "fallback:nvidia_nim_unavailable"
    )
    assert (
        ReasoningTraceService.effective_model_used(
            "nvidia_nim",
            {"is_fallback": False},
        )
        == "nvidia_nim"
    )
    assert (
        ReasoningTraceService.effective_model_used(
            "fallback:nvidia_nim_timeout",
            {"is_fallback": True, "fallback_reason": "nvidia_nim_timeout"},
        )
        == "fallback:nvidia_nim_timeout"
    )


@pytest.mark.asyncio
async def test_critique_skips_fallback_without_calling_provider(monkeypatch):
    async def fail_if_called(**_kwargs):
        raise AssertionError("fallback critique must not call a provider")

    monkeypatch.setattr("investos.services.reasoning.call_llm_json", fail_if_called)
    service = ReasoningService(SimpleNamespace(add=lambda _item: None))

    await service._create_critique(
        SimpleNamespace(id=uuid4()),
        {},
        {"is_fallback": True, "fallback_reason": "nvidia_nim_unconfigured"},
    )


def test_timeout_fallback_surfaces_matching_active_watch_context():
    result = ReasoningService(None)._timeout_fallback_reasoning(
        {
            "query_text": "i mean memb earnings report on wednesday?",
            "subject_name": "MEMB · Memory Beta Inc.",
            "direct_evidence": [],
            "connected_evidence": [],
            "historical_evidence": [],
            "contradiction_evidence": [],
            "portfolio_context": {
                "top_holdings": [{"ticker": "MEMA"}, {"ticker": "MEMB"}],
                "subject_watchers": [
                    {
                        "ticker": "MEMB",
                        "condition_type": "earnings_release",
                        "objective": "Capture earnings reaction and guidance update; given 19.8% portfolio weight, material impact expected",
                        "adjustment_plan": "If guidance beats and affirms HBM supply constraint, hold. If miss or weak guide, consider trimming given MEMA correlation",
                    }
                ],
            },
        }
    )

    reasoning = result["reasoning"].lower()
    assert "active watch context already stored" in reasoning
    assert "memb earnings release" in reasoning
    assert "hbm supply constraint" in reasoning
    assert "mema correlation" in reasoning


def test_portfolio_lookahead_intent_catches_next_week_attention_prompt():
    assert PortfolioLookaheadService.looks_like_lookahead_request(
        "anything i should look forward/pay attention to the next week?"
    )


def test_fallback_fresh_context_requirement_is_generic_outage_gate():
    service = AgentService.__new__(AgentService)

    assert service._fallback_fresh_context_requirement(
        "anything happen with memory beta lately?"
    )["required"]
    assert service._fallback_fresh_context_requirement(
        "what about the anthropic deal?"
    )["required"]
    assert service._fallback_fresh_context_requirement(
        "memory beta strategic agreement with anthropic"
    )["required"]
    assert not service._fallback_fresh_context_requirement(
        "how does mema look in the long run?"
    )["required"]
    assert not service._fallback_fresh_context_requirement(
        "test NAND supply discipline and normalized margins"
    )["required"]

    query = service._fresh_research_query(
        message="what about the anthropic deal?",
        subject_name="MEMB · Memory Beta Inc.",
        subject_type="entity",
    )
    latest_query = service._fresh_research_query(
        message="anything happen with memory beta lately?",
        subject_name="MEMB · Memory Beta Inc.",
        subject_type="entity",
    )
    assert "MEMB · Memory Beta Inc." in query
    assert "anthropic deal" in query.lower()
    assert "investor setup" in query.lower()
    assert "expectation delta" in query.lower()
    assert "market reaction" in query.lower()
    assert "portfolio read-through" in query.lower()
    assert "investor setup" in latest_query.lower()
    assert "market reaction" in latest_query.lower()


@pytest.mark.asyncio
async def test_fresh_research_query_planner_generates_event_specific_setup_query(
    monkeypatch,
):
    service = AgentService.__new__(AgentService)

    async def fake_call_llm_json(
        *,
        system_prompt,
        user_prompt,
        schema,
        timeout_seconds=None,
        provider_override=None,
        on_chunk=None,
    ):
        payload = json.loads(user_prompt)
        assert payload["subject_name"] == "Chip Systems Inc."
        assert "fixed checklist" in system_prompt
        return {
            "query": "Chip Systems earnings AI revenue guidance investor hurdle price reaction estimate revisions",
            "information_needs": [
                "pre-event investor hurdle for AI revenue and guidance",
                "actual result versus expected overperformance",
                "post-print price and estimate revision reaction",
            ],
            "reason": "Chip Systems-style event reads need setup versus actual outcome, not raw beat/miss.",
        }

    monkeypatch.setattr("investos.services.agent.call_llm_json", fake_call_llm_json)

    plan = await service._fresh_research_query_plan(
        message="did Chip Systems underperform expected overperformance?",
        subject_name="Chip Systems Inc.",
        subject_type="entity",
        fallback_query="Chip Systems Inc. earnings investor setup versus actual result",
    )

    assert plan["planner_fallback"] is False
    assert "Chip Systems Inc." in plan["query"]
    assert "investor hurdle" in plan["query"]
    assert "expected overperformance" in " ".join(plan["information_needs"])
    assert "raw beat/miss" in plan["reason"]


@pytest.mark.asyncio
async def test_fresh_research_query_planner_rejects_placeholder_query(monkeypatch):
    service = AgentService.__new__(AgentService)

    async def fake_call_llm_json(
        *,
        system_prompt,
        user_prompt,
        schema,
        timeout_seconds=None,
        provider_override=None,
        on_chunk=None,
    ):
        return {
            "query": "query",
            "information_needs": [],
            "reason": "Bad placeholder response from provider.",
        }

    monkeypatch.setattr("investos.services.agent.call_llm_json", fake_call_llm_json)

    plan = await service._fresh_research_query_plan(
        message="whats been going on lately why did i lose so much money",
        subject_name="Portfolio",
        subject_type="portfolio",
        fallback_query=service._fresh_research_query(
            message="whats been going on lately why did i lose so much money",
            subject_name="Portfolio",
            subject_type="portfolio",
            packet_context={
                "portfolio_context": {
                    "top_holdings": [{"ticker": "MEMA"}, {"ticker": "MEMB"}]
                }
            },
        ),
        packet_context={
            "portfolio_context": {
                "top_holdings": [{"ticker": "MEMA"}, {"ticker": "MEMB"}]
            }
        },
    )

    assert plan["query"] != "query"
    assert "MEMA" in plan["query"]
    assert "MEMB" in plan["query"]
    assert "investor setup" in plan["query"].lower()
    assert "market reaction" in plan["query"].lower()


def test_current_event_query_augmentation_skips_non_event_analysis():
    service = AgentService.__new__(AgentService)

    long_run = service._fresh_research_query(
        message="how does mema look in the long run? can it hit 26% cagr?",
        subject_name="MEMA · Memory Alpha Corp.",
        subject_type="entity",
    )
    earnings = service._fresh_research_query(
        message="what happened with the earnings report?",
        subject_name="Chip Systems Inc.",
        subject_type="entity",
    )

    assert "consensus expectations" not in long_run.lower()
    assert "investor setup" in earnings.lower()
    assert "expectation delta" in earnings.lower()
    assert "market reaction" in earnings.lower()


def test_portfolio_fresh_research_query_uses_actual_holding_terms():
    service = AgentService.__new__(AgentService)

    query = service._fresh_research_query(
        message="whats been going on lately why did i lose so much money",
        subject_name="Portfolio",
        subject_type="portfolio",
        packet_context={
            "portfolio_context": {
                "top_holdings": [
                    {"ticker": "AUTO"},
                    {"ticker": "MEMB"},
                    {"ticker": "MEMA"},
                    {"ticker": "OPTC"},
                ],
            }
        },
    )

    assert "AUTO" in query
    assert "MEMB" in query
    assert "MEMA" in query
    assert "OPTC" in query
    assert "investor setup" in query.lower()


def test_timeout_orchestration_explains_no_actions_created():
    service = AgentService.__new__(AgentService)
    orchestration = service._timeout_orchestration(
        {
            "thesis_summary": "Portfolio loss likely needs fresh setup and price evidence.",
            "reasoning": "Memory exposure and high-beta holdings can amplify drawdowns.",
        }
    )

    assert "follow-up action planner timed out" in orchestration["rationale_summary"]
    assert "did not create" in orchestration["rationale_summary"]
    assert orchestration["shadow_experiment"]["should_create"] is False


def test_compact_packet_context_carries_fresh_research_separately_from_evidence():
    compact = compact_packet_context(
        {
            "query_text": "what about the deal?",
            "subject_type": "entity",
            "subject_id": uuid4(),
            "subject_name": "MEMB · Memory Beta Inc.",
            "fresh_research_context": {
                "required": True,
                "reason": "Current-event question.",
                "searched": True,
                "status": "ok",
                "query": "Memory Beta strategic agreement",
                "checked_at": "2026-06-23T12:00:00+00:00",
                "results": [
                    {
                        "title": "Memory Beta announces strategic agreement",
                        "url": "https://example.com/memory beta",
                        "content": "Memory Beta announced a customer agreement with terms still undisclosed.",
                        "published_date": "2026-06-23",
                    }
                ],
            },
            "direct_evidence": [],
        }
    )

    assert compact["direct_evidence_count"] == 0
    fresh = compact["fresh_research_context"]
    assert fresh["required"] is True
    assert fresh["status"] == "ok"
    assert fresh["results"][0]["url"] == "https://example.com/memory beta"


def test_fresh_research_merge_keeps_source_snippets_out_of_main_answer():
    service = AgentService.__new__(AgentService)
    message = service._merge_fresh_research_context_into_answer(
        "Current read.",
        {
            "status": "ok",
            "query": "Memory Beta strategic agreement",
            "results": [
                {
                    "title": "Memory Beta announces strategic agreement",
                    "url": "https://example.com/memory beta",
                    "content": "Memory Beta announced an agreement; terms and committed capacity remain the key missing details.",
                }
            ],
        },
    )

    assert "Fresh source check:" in message
    assert "Memory Beta announces strategic agreement" in message
    assert "Fresh source read:" not in message
    assert "terms and committed capacity" not in message
    assert "accepted thesis" not in message
    assert "pre-ingestion snippets" in message


def test_reasoning_prompt_requires_fresh_context_for_current_event_claims():
    prompt = ReasoningService(None)._analysis_system_prompt().lower()
    assert "fresh_research_context" in prompt
    assert "current-event claim is unverified" in prompt
    assert "ephemeral fresh-search snippets" in prompt
    assert "prior investor expectations" in prompt
    assert "price reaction" in prompt
    assert "historical_analogy_lenses" in prompt
    assert "what rhymes" in prompt
    assert "dominant-channel test" in prompt
    assert "source-dated financial and competitive metrics" in prompt
    assert "debt/leverage" in prompt
    assert "open ontology" in prompt


def test_extraction_prompt_preserves_source_dated_open_metric_ontology():
    prompt = ExtractionWorker._structured_extraction_system_prompt().lower()

    assert "metric name, value" in prompt
    assert "fiscal period or as-of date" in prompt
    assert "balance-sheet debt/leverage" in prompt
    assert "interest coverage" in prompt
    assert "peer comparisons" in prompt
    assert "illustrative ontology, not a closed checklist" in prompt


@pytest.mark.asyncio
async def test_turn_classifier_fast_paths_portfolio_lookahead():
    service = AgentService.__new__(AgentService)
    result = await service._classify_turn_intent(
        "anything i should look forward/pay attention to the next week?"
    )

    assert result["route"] == "operate"
    assert "lookahead" in result["reason"].lower()


def test_portfolio_lookahead_answer_surfaces_dated_mu_catalyst():
    service = AgentService.__new__(AgentService)
    result = service._deterministic_operating_answer(
        {
            "query_type": "portfolio_lookahead",
            "portfolio_lookahead": {
                "as_of": "2026-06-22T19:00:00+00:00",
                "horizon_end": "2026-06-29T19:00:00+00:00",
                "attention_items": [
                    {
                        "source": "active_watch",
                        "ticker": "MEMB",
                        "title": "Memory Beta earnings report",
                        "event_type": "earnings_release",
                        "due_at": "2026-06-24T20:00:00+00:00",
                        "countdown_seconds": 176400,
                        "portfolio_weight_pct": 19.8,
                        "why_it_matters": (
                            "MEMB is a large memory holding; earnings guidance updates HBM/NAND pricing, "
                            "which also affects MEMA memory exposure."
                        ),
                        "investment_lens": {
                            "expectation_delta": "Compare the pre-event investor hurdle against reported HBM demand.",
                            "market_reaction": "Check price reaction and analyst estimate revisions.",
                            "portfolio_transmission": "Map the result through MEMB and MEMA memory exposure.",
                            "best_next_check": "Use the official release/transcript and market reaction before changing sizing.",
                        },
                    }
                ],
            },
        }
    )

    message = result["assistant_message"].lower()
    assert "memb" in message
    assert "memory beta earnings report" in message
    assert "2026-06-24" in message
    assert "hbm/nand pricing" in message
    assert "mema memory exposure" in message
    assert "investment read" in message
    assert "pre-event investor hurdle" in message
    assert "analyst estimate revisions" in message
    assert "best next check" in message


def test_portfolio_lookahead_generated_event_lens_checks_expected_overperformance():
    lens = PortfolioLookaheadService._event_investment_lens(
        ticker="CHPX",
        event_type="earnings_release",
        title="Chip Systems earnings report",
        description="AI infrastructure earnings print",
        objective="Compare AI revenue and guidance against the event setup.",
        adjustment_plan="",
        weight_pct=0,
    )

    assert lens is not None
    assert "pre-event investor expectations" in lens["expectation_delta"]
    assert "absolute growth is not enough" in lens["expectation_delta"]
    assert "required overperformance" in lens["expectation_delta"]


def test_portfolio_lookahead_surfaces_undated_earnings_watch_as_missing_calendar():
    service = PortfolioLookaheadService.__new__(PortfolioLookaheadService)
    now = datetime(2026, 6, 23, 12, tzinfo=timezone.utc)
    items = service._attention_items(
        positions=[
            {
                "ticker": "MEMB",
                "list_type": "holding",
                "weight_pct": 19.8,
                "market_value": 2500,
            }
        ],
        scheduled_events=[],
        watchers=[
            {
                "ticker": "MEMB",
                "condition_type": "earnings_release",
                "objective": "HBM demand confirmation and capex execution",
                "adjustment_plan": "Weak guidance would pressure MEMB and correlated MEMA memory exposure.",
                "deadline": None,
                "countdown_seconds": None,
            }
        ],
        now=now,
        horizon=now,
    )

    assert len(items) == 1
    item = items[0]
    assert item["source"] == "active_watch_date_missing"
    assert item["ticker"] == "MEMB"
    assert item["date_status"] == "missing"
    assert "no stored event date" in item["why_it_matters"].lower()
    assert "hbm demand confirmation" in item["why_it_matters"].lower()
    assert "mema memory exposure" in item["if_it_fires"].lower()
    assert item["investment_lens"] is not None
    assert "expectation" in item["investment_lens"]["expectation_delta"].lower()
    assert "price action" in item["investment_lens"]["market_reaction"].lower()


def test_portfolio_lookahead_active_watch_why_uses_stored_objective():
    service = PortfolioLookaheadService.__new__(PortfolioLookaheadService)
    now = datetime(2026, 6, 23, 12, tzinfo=timezone.utc)
    deadline = now + timedelta(days=1)

    items = service._attention_items(
        positions=[
            {
                "ticker": "MEMB",
                "list_type": "holding",
                "weight_pct": 19.8,
                "market_value": 2500,
            }
        ],
        scheduled_events=[],
        watchers=[
            {
                "ticker": "MEMB",
                "condition_type": "earnings_release",
                "objective": "HBM revenue confirmation and NAND pricing read-through to MEMA.",
                "adjustment_plan": "Trim if guide misses memory-cycle expectations.",
                "deadline": deadline,
                "countdown_seconds": int((deadline - now).total_seconds()),
            }
        ],
        now=now,
        horizon=now + timedelta(days=10),
    )

    assert len(items) == 1
    assert "hbm revenue confirmation" in items[0]["why_it_matters"].lower()
    assert "mema" in items[0]["why_it_matters"].lower()


def test_portfolio_lookahead_countdown_sort_keeps_zero_as_immediate():
    assert PortfolioLookaheadService._countdown_sort_value(0) == 0
    assert PortfolioLookaheadService._countdown_sort_value(None) > 0


def test_portfolio_lookahead_diversifies_undated_watchers_across_tickers():
    service = PortfolioLookaheadService.__new__(PortfolioLookaheadService)
    now = datetime(2026, 6, 23, 12, tzinfo=timezone.utc)
    items = service._attention_items(
        positions=[
            {
                "ticker": "MEMA",
                "list_type": "holding",
                "weight_pct": 23.0,
                "market_value": 2800,
            },
            {
                "ticker": "MEMB",
                "list_type": "holding",
                "weight_pct": 19.8,
                "market_value": 2400,
            },
        ],
        scheduled_events=[],
        watchers=[
            {
                "ticker": "MEMA",
                "condition_type": "earnings_release",
                "objective": "First MEMA earnings watch",
                "adjustment_plan": "Resolve MEMA margin baseline.",
                "deadline": None,
                "countdown_seconds": None,
            },
            {
                "ticker": "MEMA",
                "condition_type": "earnings_release",
                "objective": "Second MEMA earnings watch",
                "adjustment_plan": "Resolve MEMA standalone guidance.",
                "deadline": None,
                "countdown_seconds": None,
            },
            {
                "ticker": "MEMB",
                "condition_type": "earnings_release",
                "objective": "MEMB earnings watch",
                "adjustment_plan": "Check HBM and NAND pricing read-through.",
                "deadline": None,
                "countdown_seconds": None,
            },
        ],
        now=now,
        horizon=now,
    )

    assert [item["ticker"] for item in items[:2]] == ["MEMA", "MEMB"]
    assert items[0]["related_watch_count"] == 2
    assert "related active watches" in items[0]["title"]


def test_portfolio_lookahead_calendar_date_parser_accepts_month_date_within_horizon():
    now = datetime(2026, 6, 23, 12, tzinfo=timezone.utc)
    horizon = now + timedelta(days=10)

    parsed = PortfolioLookaheadService._extract_calendar_date(
        "Memory Beta Inc. reports earnings on June 24, 2026 after market close.",
        now=now,
        horizon=horizon,
    )

    assert parsed is not None
    assert parsed.date().isoformat() == "2026-06-24"
    assert parsed.hour == 20


def test_portfolio_lookahead_calendar_date_parser_rejects_out_of_window_dates():
    now = datetime(2026, 6, 23, 12, tzinfo=timezone.utc)
    horizon = now + timedelta(days=10)

    assert (
        PortfolioLookaheadService._extract_calendar_date(
            "Memory Beta Inc. reports earnings on August 7, 2026.",
            now=now,
            horizon=horizon,
        )
        is None
    )


def test_portfolio_lookahead_calendar_resolution_candidates_group_duplicate_watches():
    service = PortfolioLookaheadService.__new__(PortfolioLookaheadService)
    sndk_first = uuid4()
    sndk_second = uuid4()
    mu_watch = uuid4()

    candidates = service._calendar_resolution_candidates(
        positions=[
            {"ticker": "MEMA", "name": "Memory Alpha Corp.", "weight_pct": 25.0},
            {"ticker": "MEMB", "name": "Memory Beta Inc.", "weight_pct": 19.8},
        ],
        watchers=[
            {
                "id": sndk_first,
                "ticker": "MEMA",
                "condition_type": "earnings_release",
                "objective": "MEMA first standalone earnings release",
                "adjustment_plan": "Resolve NAND margin baseline.",
                "deadline": None,
            },
            {
                "id": sndk_second,
                "ticker": "MEMA",
                "condition_type": "earnings_release",
                "objective": "MEMA earnings guidance update",
                "adjustment_plan": "Resolve post-spin balance sheet risk.",
                "deadline": None,
            },
            {
                "id": mu_watch,
                "ticker": "MEMB",
                "condition_type": "earnings_release",
                "objective": "MEMB earnings read-through to memory pricing",
                "adjustment_plan": "Use HBM/NAND guide as memory-cycle check.",
                "deadline": None,
            },
        ],
    )

    assert [item["ticker"] for item in candidates] == ["MEMA", "MEMB"]
    assert candidates[0]["name"] == "Memory Alpha Corp."
    assert candidates[0]["watcher_ids"] == [str(sndk_first), str(sndk_second)]
    assert "MEMA first standalone earnings release" in candidates[0]["watch_titles"]
    assert candidates[1]["watcher_ids"] == [str(mu_watch)]


def test_portfolio_lookahead_calendar_search_query_uses_generic_event_terms():
    now = datetime(2026, 6, 23, 12, tzinfo=timezone.utc)
    horizon = now + timedelta(days=10)

    query = PortfolioLookaheadService._calendar_search_query(
        {
            "ticker": "MEMB",
            "name": "Memory Beta Inc.",
            "event_type": "earnings_release",
        },
        now=now,
        horizon=horizon,
    )

    assert "MEMB" in query
    assert "Memory Beta Inc." in query
    assert "next earnings date investor relations" in query
    assert "2026-06-23 to 2026-07-03" in query


def test_portfolio_lookahead_scan_query_requests_event_expectation_delta():
    now = datetime(2026, 6, 23, 12, tzinfo=timezone.utc)
    horizon = now + timedelta(days=10)

    query = PortfolioLookaheadService._scan_query(
        positions=[
            {"ticker": "MEMB", "list_type": "holding"},
            {"ticker": "MEMA", "list_type": "holding"},
        ],
        now=now,
        horizon=horizon,
    ).lower()

    assert "memb mema" in query
    assert "upcoming earnings calendar" in query
    assert "investor setup" in query
    assert "actual result" in query
    assert "expectation delta" in query
    assert "market reaction" in query


def test_compact_packet_context_promotes_source_feedback_context():
    packet = compact_packet_context(
        {
            "query_text": "does this source help the MEMA thesis?",
            "subject_type": "entity",
            "subject_id": "11111111-1111-1111-1111-111111111111",
            "portfolio_context": {
                "source_feedback": {
                    "counts": {"useful": 1, "not_useful": 2},
                    "recent": [
                        {
                            "rating": "not_useful",
                            "source_name": "Generic Market Blog",
                            "source_type": "web",
                            "title": "Memory prices again",
                            "note": "No direct NAND/HBM mechanism.",
                            "context": "source_workspace",
                            "flagged_at": "2026-06-20T12:00:00+00:00",
                        }
                    ],
                }
            },
        }
    )
    feedback = packet["source_feedback_context"]
    assert feedback["counts"] == {"useful": 1, "not_useful": 2}
    assert feedback["recent"][0]["rating"] == "not_useful"
    assert "down-rank" in feedback["instruction"].lower()


def test_compact_evidence_nodes_preserves_source_feedback():
    compacted = compact_evidence_nodes(
        [
            {
                "id": "11111111-1111-1111-1111-111111111111",
                "type": "claim",
                "tier": "credible_interpretation",
                "importance": "high",
                "text": "HBM demand changes could affect memory supply allocation and NAND pricing.",
                "created_at": "2026-06-20T12:00:00+00:00",
                "source": {
                    "name": "Generic Market Blog",
                    "type": "web",
                    "is_trusted": False,
                    "evidence_title": "Memory prices again",
                    "quality": {
                        "quality_score": 0.35,
                        "originality_score": 0.2,
                        "timing_usefulness": 0.25,
                        "evidence_count": 3,
                        "notes": "User feedback suggests this source repeats adjacent memory chatter.",
                        "last_evaluated": "2026-06-20T12:00:00+00:00",
                    },
                    "trust_profile": {
                        "factual_reliability": "low",
                        "noise_ratio": "noisy",
                        "trust_trajectory": "degrading",
                        "correction_quality": "slow_corrects",
                    },
                    "feedback": {
                        "rating": "not_useful",
                        "note": "No direct NAND/HBM mechanism.",
                        "flagged_at": "2026-06-20T12:00:00+00:00",
                    },
                },
            }
        ]
    )
    source = compacted[0]["source"]
    assert source["name"] == "Generic Market Blog"
    assert source["quality"]["quality_score"] == 0.35
    assert source["trust_profile"]["noise_ratio"] == "noisy"
    assert source["feedback"]["rating"] == "not_useful"
    assert "No direct NAND/HBM mechanism" in source["feedback"]["note"]


def test_retrieval_source_context_includes_quality_profiles():
    evidence = SimpleNamespace(
        title="Memory prices again",
        url="https://example.com/memory",
        metadata_json={
            "user_feedback": {
                "rating": "not_useful",
                "note": "No direct NAND/HBM mechanism.",
                "context": "source_workspace",
                "flagged_at": "2026-06-20T12:00:00+00:00",
            }
        },
    )
    source = SimpleNamespace(
        name="Generic Market Blog",
        source_type="web_research",
        is_trusted=False,
    )
    quality = SimpleNamespace(
        quality_score=0.35,
        originality_score=0.2,
        timing_usefulness=0.25,
        evidence_count=3,
        notes="User feedback suggests this source repeats adjacent memory chatter.",
        last_evaluated=datetime(2026, 6, 20, 12, tzinfo=timezone.utc),
    )
    trust = SimpleNamespace(
        factual_reliability="low",
        noise_ratio="noisy",
        trust_trajectory="degrading",
        correction_quality="slow_corrects",
    )
    value = SimpleNamespace(
        idea_generation_value="low",
        timing_value="low",
        portfolio_relevance_value="low",
        specificity="vague",
        originality="repeater",
    )

    context = RetrievalService._source_context_from_evidence(
        evidence,
        source,
        quality_segment=quality,
        trust_profile=trust,
        value_profile=value,
    )

    assert context["is_trusted"] is False
    assert context["quality"]["quality_score"] == 0.35
    assert context["trust_profile"]["trust_trajectory"] == "degrading"
    assert context["value_profile"]["originality"] == "repeater"
    assert context["feedback"]["rating"] == "not_useful"


@pytest.mark.asyncio
async def test_reasoning_retries_with_reduced_context_before_fallback(monkeypatch):
    prompts: list[dict] = []

    async def fake_call_llm_json(
        *, system_prompt, user_prompt, schema, timeout_seconds=None, on_chunk=None
    ):
        payload = json.loads(user_prompt)
        prompts.append(payload)
        if len(prompts) == 1:
            raise TimeoutError("full packet timed out")
        return {
            "stance": "uncertain",
            "confidence_band": "low",
            "thesis_summary": "MEMA needs source-backed NAND cycle evidence before a thesis can be accepted.",
            "reasoning": "Reduced packet still identifies the core uncertainty without pretending stronger evidence exists.",
            "what_would_falsify": ["NAND pricing weakens while MEMA margins compress."],
            "what_would_strengthen": [
                "Primary source evidence of durable NAND pricing recovery."
            ],
            "supporting_evidence_ids": ["1", "2", "3", "4", "5", "6"],
            "contradicting_evidence_ids": [],
            "bull_case": None,
            "bear_case": None,
            "active_contradictions": [],
            "critique_text": "Reduced-context recovery succeeded.",
        }

    monkeypatch.setattr("investos.services.reasoning.call_llm_json", fake_call_llm_json)
    service = ReasoningService(None)

    result = await service._reason_with_llm(
        {
            "query_text": "how does mema look in the long run?",
            "subject_type": "entity",
            "subject_id": "11111111-1111-1111-1111-111111111111",
            "subject_name": "MEMA · Memory Alpha Corp.",
            "direct_evidence_count": 10,
            "connected_evidence_count": 8,
            "historical_evidence_count": 4,
            "contradiction_evidence_count": 2,
            "lesson_count": 4,
            "direct_evidence": [
                {"id": str(index), "text": f"direct {index}"} for index in range(10)
            ],
            "connected_evidence": [
                {"id": str(index), "text": f"connected {index}"} for index in range(8)
            ],
            "historical_evidence": [
                {"id": str(index), "text": f"historical {index}"} for index in range(4)
            ],
            "contradiction_evidence": [
                {"id": str(index), "text": f"contradiction {index}"}
                for index in range(2)
            ],
            "lessons": [
                {"id": str(index), "summary": f"lesson {index}"} for index in range(4)
            ],
            "portfolio_context": {
                "top_holdings": [{"ticker": "MEMA"}, {"ticker": "MEMB"}],
                "source_feedback": {
                    "counts": {"useful": 0, "not_useful": 1},
                    "recent": [
                        {
                            "rating": "not_useful",
                            "source_name": "Generic Market Blog",
                            "title": "Memory price opinion",
                            "note": "No direct mechanism.",
                        }
                    ],
                },
            },
        },
        system_prompt="Analyze.",
    )

    assert len(prompts) == 2
    assert prompts[1]["analysis_mode"] == "reduced_context_recovery"
    assert len(prompts[1]["direct_evidence"]) == 3
    assert prompts[1]["counts"]["direct_evidence"] == 10
    assert result["recovery_mode"] == "reduced_context"
    assert result["supporting_evidence_ids"] == ["1", "2", "3", "4", "5"]
    assert result["source_feedback_influence"]["counts"]["not_useful"] == 1
    assert "Generic Market Blog" in result["source_feedback_influence"]["summary"]
    assert not result.get("is_fallback")


@pytest.mark.asyncio
async def test_reasoning_tries_ready_alternate_provider_before_local_fallback(
    monkeypatch,
):
    calls: list[tuple[str, dict]] = []

    async def fake_available(primary_provider=None):
        assert primary_provider in {"ollama", "nvidia_nim", "codex-cli", "codex_cli"}
        return ["ollama"]

    async def fake_call_llm_json(
        *,
        system_prompt,
        user_prompt,
        schema,
        timeout_seconds=None,
        provider_override=None,
        on_chunk=None,
    ):
        payload = json.loads(user_prompt)
        calls.append((provider_override or "primary", payload))
        if provider_override != "ollama":
            raise TimeoutError("primary provider unavailable")
        return {
            "stance": "uncertain",
            "confidence_band": "low",
            "thesis_summary": "Alternate provider produced a structured low-confidence view.",
            "reasoning": "The alternate provider received the same reduced evidence frame and returned JSON.",
            "what_would_falsify": ["Direct evidence contradicts the mechanism."],
            "what_would_strengthen": ["Source-backed evidence confirms the mechanism."],
            "supporting_evidence_ids": ["1", "2", "3", "4", "5", "6"],
            "contradicting_evidence_ids": [],
            "bull_case": None,
            "bear_case": None,
            "active_contradictions": [],
            "critique_text": "Alternate-provider recovery succeeded.",
        }

    monkeypatch.setattr(
        "investos.services.reasoning.available_llm_json_recovery_providers",
        fake_available,
    )
    monkeypatch.setattr("investos.services.reasoning.call_llm_json", fake_call_llm_json)

    result = await ReasoningService(None)._reason_with_llm(
        {
            "query_text": "how does mema look in the long run?",
            "subject_type": "entity",
            "subject_id": "11111111-1111-1111-1111-111111111111",
            "subject_name": "MEMA · Memory Alpha Corp.",
            "direct_evidence_count": 2,
            "connected_evidence_count": 2,
            "historical_evidence_count": 1,
            "contradiction_evidence_count": 0,
            "direct_evidence": [{"id": "1", "text": "direct"}],
            "connected_evidence": [{"id": "2", "text": "connected"}],
            "historical_evidence": [{"id": "3", "text": "historical"}],
            "contradiction_evidence": [],
            "lessons": [],
            "portfolio_context": {
                "top_holdings": [{"ticker": "MEMA"}, {"ticker": "MEMB"}]
            },
        },
        system_prompt="Analyze.",
    )

    assert [provider for provider, _ in calls] == ["primary", "primary", "ollama"]
    assert calls[-1][1]["analysis_mode"] == "reduced_context_recovery"
    assert result["recovery_mode"] == "alternate_provider:ollama"
    assert result["supporting_evidence_ids"] == ["1", "2", "3", "4", "5"]
    assert not result.get("is_fallback")


def test_thin_packet_prompt_avoids_pretend_expert_persona():
    prompt = ReasoningService(None)._thin_packet_system_prompt().lower()
    assert "expert thesis" not in prompt
    assert "must not provide a generic response" not in prompt
    assert "powerful, dot-connecting investment analyst" not in prompt
    assert "without pretending" in prompt
    assert "historical_analogy_lenses" in prompt
    assert "what breaks the analogy" in prompt
    assert "dominant-channel check" in prompt


def test_thin_packet_timeout_respects_configured_interactive_bound(monkeypatch):
    monkeypatch.setattr(
        "investos.services.reasoning.settings.LLM_TIMEOUT_SECONDS",
        120,
    )
    service = ReasoningService(None)
    assert service._interactive_timeout_seconds() == 120
    assert service._interactive_timeout_seconds(thin_packet=True) == 90


def test_analysis_lens_prompts_reject_persona_roleplay():
    dispatch = AgentService._analysis_lens_dispatch_prompt().lower()
    synthesis = AgentService._analysis_lens_synthesis_prompt().lower()

    assert "do not create personas" in dispatch
    assert "not a closed ontology" in dispatch
    assert "do not treat examples as complete" in dispatch
    assert "job titles" in dispatch
    assert "role-play" in dispatch
    assert "historical_analogy_lenses" in dispatch
    assert "causal-channel checks" in dispatch
    assert "systemic risk quant" not in dispatch
    assert "strategic synthesizer" not in synthesis
    assert "resolve contradictions" in synthesis


@pytest.mark.asyncio
async def test_blind_review_persists_only_supported_critique_run_fields(monkeypatch):
    class ReviewSession:
        def __init__(self):
            self.added = []

        def add(self, item):
            self.added.append(item)

    async def fake_call_llm_json(**_kwargs):
        return {
            "candidate_stance": "uncertain",
            "confidence_band": "low",
            "independent_summary": "The packet leaves the causal claim unresolved.",
            "material_assertions": [],
            "assumptions": [],
            "alternative_hypotheses": [],
            "issues_found": ["Independent lineage is thin."],
            "severity": "major",
        }

    session = ReviewSession()
    service = ReasoningService(session)
    monkeypatch.setattr("investos.services.reasoning.call_llm_json", fake_call_llm_json)
    monkeypatch.setattr(
        "investos.services.reasoning.settings.REASONING_INDEPENDENT_REVIEW_ENABLED",
        True,
    )
    monkeypatch.setattr(service, "_active_provider_unconfigured_label", lambda: None)
    monkeypatch.setattr(service, "_provider_label", lambda: "test-provider")

    review = await service._create_critique(
        SimpleNamespace(id=uuid4()),
        {"direct_evidence": []},
        {"stance": "neutral", "is_fallback": False},
    )

    assert review["candidate_stance"] == "uncertain"
    assert len(session.added) == 1
    assert (
        session.added[0].critique_text
        == "The packet leaves the causal claim unresolved."
    )


def test_analysis_lens_budget_is_runtime_configured(monkeypatch):
    monkeypatch.setattr(
        "investos.services.agent.settings.AGENT_DYNAMIC_ANALYSIS_LENS_LIMIT",
        6,
    )
    assert "up to 6" in AgentService._analysis_lens_dispatch_prompt()

    monkeypatch.setattr(
        "investos.services.agent.settings.AGENT_DYNAMIC_ANALYSIS_LENS_LIMIT",
        999,
    )
    assert "up to 8" in AgentService._analysis_lens_dispatch_prompt()


def test_chat_response_schema_exposes_historical_analogy_lenses():
    assert "historical_analogy_lenses" in AgentTurnResponse.model_fields
    assert "historical_analogy_lenses" in AgentConversationEntryResponse.model_fields


def test_agent_compacts_historical_analogy_lenses_for_answer_metadata():
    lenses = AgentService._compact_historical_analogy_lenses(
        {
            "historical_analogy_lenses": [
                {
                    "name": "Dot-com bust (1999-2001)",
                    "period": "1999-2001",
                    "lens_use_policy": "Seed, not checklist: expand or discard based on evidence.",
                    "current_application_prompt": "Apply the driver only after testing current evidence and exposure.",
                    "what_rhymes": "AI-capex buildout overlaps through capex supercycle.",
                    "dominant_channel_test": "Test committed capacity against realized demand and margins.",
                    "where_analogy_breaks": "Break the analogy if supply discipline and profitability are materially different.",
                    "portfolio_transmission": "Map the channel to MEMB and MEMA before using it.",
                    "best_next_check": "Compare capacity additions with demand and utilization.",
                    "investor_questions": [
                        "Which current data point confirms the driver?"
                    ],
                }
            ]
        }
    )

    assert lenses
    assert lenses[0]["name"] == "Dot-com bust (1999-2001)"
    assert lenses[0]["lens_use_policy"].startswith("Seed, not checklist")
    assert "current evidence" in lenses[0]["current_application_prompt"]
    assert "committed capacity" in lenses[0]["dominant_channel_test"]
    assert "MEMB and MEMA" in lenses[0]["portfolio_transmission"]
    assert lenses[0]["investor_questions"][0].startswith("Which current")


def test_agent_action_schema_uses_rationale_summary_not_chain_of_thought():
    properties = AGENT_RESPONSE_SCHEMA["properties"]
    assert "rationale_summary" in properties
    assert "rationale_summary" in AGENT_RESPONSE_SCHEMA["required"]
    assert "chain_of_thought" not in properties
    assert "chain_of_thought" not in AGENT_RESPONSE_SCHEMA["required"]


def test_reasoning_schema_active_watchers_are_strict_objects():
    watcher_items = REASONING_SCHEMA["properties"]["active_watchers"]["items"]
    assert "active_watchers" in REASONING_SCHEMA["required"]
    assert watcher_items["type"] == "object"
    assert watcher_items["additionalProperties"] is False
    assert set(watcher_items["required"]) == set(watcher_items["properties"])
    assert "enum" not in watcher_items["properties"]["condition_type"]
    assert (
        "precise open-ended catalyst"
        in watcher_items["properties"]["condition_type"]["description"]
    )
    assert watcher_items["properties"]["threshold"]["type"] == ["number", "null"]
    assert watcher_items["properties"]["deadline_hours"]["type"] == ["number", "null"]


def test_reasoning_prompt_treats_watchers_as_durable_signal_rules():
    prompt = ReasoningService(None)._analysis_system_prompt()

    assert "durable alert rules" in prompt
    assert "absolute quoted price" in prompt
    assert "not already represented" in prompt
    assert "do not force catalysts" in prompt.lower()
    assert "thesis-contradiction tests" in prompt


def test_graph_portfolio_mechanism_uses_stored_channel_not_keyword_glossary():
    result = GraphService._portfolio_channel_from_text(
        "HBM demand weakness could spill into NAND pricing and Memory Beta margin guidance",
        ["MEMB", "MEMA"],
        context_texts=[
            "Proposed mechanism: Capacity shifts from HBM to DDR5, changing contracted supply and margin assumptions. "
            "Next test: Compare management allocation guidance with contract volumes."
        ],
    )

    mechanism = result["mechanism"]
    assert "MEMB, MEMA" in mechanism
    assert "Capacity shifts from HBM to DDR5" in mechanism
    assert "stored relationship makes" not in mechanism.lower()
    assert "management allocation guidance" in result["next_test"]


def test_graph_portfolio_mechanism_reads_context_before_generic_fallback():
    result = GraphService._portfolio_channel_from_text(
        "2026 revolver credit facility",
        ["MEMB", "MEMA"],
        context_texts=[
            "Investment relevance: HBM demand and NAND pricing affect memory-cycle cash flow. "
            "Next test: Compare the stored thesis with current margin guidance.",
        ],
    )

    mechanism = result["mechanism"].lower()
    assert "memb, mema" in mechanism
    assert "hbm demand and nand pricing affect memory-cycle cash flow" in mechanism
    assert "exact driver is not yet explicit" not in mechanism


def test_graph_portfolio_mechanism_does_not_invent_debt_channel_from_keywords():
    result = GraphService._portfolio_channel_from_text(
        "Debt maturity wall and weaker interest coverage could pressure refinancing",
        ["MRVL"],
    )

    mechanism = result["mechanism"].lower()
    assert "mrvl" in mechanism
    assert "exact driver is not yet explicit" in mechanism
    assert "refinancing risk" not in mechanism


def test_retrieval_terms_handle_slashes_and_hyphens_without_regex_crash():
    terms = RetrievalService._meaningful_terms(
        "high bandwidth flash HBM3E/NAND cost-down"
    )
    assert "bandwidth" in terms
    assert "hbm3e/nand" in terms
    assert "cost-down" in terms


def test_conversation_message_kind_keeps_chat_and_artifacts_separate():
    service = AgentService.__new__(AgentService)
    chat_evidence = SimpleNamespace(title="Assistant turn: how does MEMA look?")
    chat_metadata = {"role": "assistant", "origin": "agent_chat"}
    assert (
        service._conversation_message_kind(
            chat_evidence, chat_metadata, content="answer"
        )
        == "chat"
    )
    assert (
        service._conversation_display_role(chat_metadata, is_artifact=False)
        == "assistant"
    )

    artifact_evidence = SimpleNamespace(title="Autonomous reflection cycle for MEMA")
    artifact_metadata = {"role": "assistant", "origin": "agent_reflection"}
    assert service._is_background_conversation_memory(
        artifact_evidence,
        artifact_metadata,
        content="MEMA accepted state: no_view / very_low",
    )
    assert (
        service._conversation_message_kind(
            artifact_evidence,
            artifact_metadata,
            content="MEMA accepted state: no_view / very_low",
            is_artifact=True,
        )
        == "system_artifact"
    )
    assert (
        service._conversation_display_role(artifact_metadata, is_artifact=True)
        == "system"
    )


def test_smalltalk_is_not_a_broad_subject_search():
    service = AgentService.__new__(AgentService)
    assert service._broad_subject_match_query("hi") is None
    assert service._broad_subject_match_query("thanks") is None
    assert service._broad_subject_match_query("about war in iran") == "war in iran"


def test_fallback_turn_intent_is_only_provider_outage_safety_net():
    service = AgentService.__new__(AgentService)
    assert service._fallback_turn_intent("hi")["route"] == "conversation"
    assert service._fallback_turn_intent("hbf?")["route"] == "clarify"
    assert service._fallback_turn_intent("thoughts?")["route"] == "continue"
    assert service._fallback_turn_intent("show trusted sources")["route"] == "operate"
    assert (
        service._fallback_turn_intent("did it catch latest transactions?")["route"]
        == "operate"
    )
    assert (
        service._fallback_turn_intent("did you store mema and hbf in knowledge?")[
            "route"
        ]
        == "operate"
    )
    assert (
        service._fallback_turn_intent("how does mema look in the long run?")["route"]
        == "analyze"
    )
    assert (
        service._fallback_turn_intent(
            "check whether portfolio risk is too concentrated in memory"
        )["route"]
        == "analyze"
    )
    assert (
        service._fallback_turn_intent("what sources support the mema thesis?")["route"]
        == "analyze"
    )


def test_auto_research_follow_up_is_operational_not_chat_polling():
    service = AgentService.__new__(AgentService)
    message = service._merge_research_follow_up_into_answer(
        "Local packet is thin.",
        processed=False,
    )
    assert "Activity/Research status" in message
    assert "will not change the accepted thesis" in message
    assert "Ask again in a moment" not in message


def test_agent_compact_exception_keeps_type_when_message_is_empty():
    assert AgentService._compact_exception(TimeoutError()) == "TimeoutError"
    assert AgentService._compact_exception(RuntimeError("boom")) == "RuntimeError: boom"


@pytest.mark.asyncio
async def test_auto_research_plan_rejects_low_information_prompts():
    service = AgentService.__new__(AgentService)
    plan = await service._auto_research_plan(
        message="hi",
        subject_type="theme",
        subject_name="US higher education enrollment trends",
        packet_context={
            "direct_evidence": [],
            "connected_evidence": [],
            "historical_evidence": [],
            "contradiction_evidence": [],
            "gap_flags": ["missing_contradiction_evidence"],
        },
        reasoning_result={
            "is_fallback": True,
            "stance": "no_view",
            "confidence_band": "very_low",
        },
    )
    assert plan["should_research"] is False
    assert "low-information" in plan["reason"].lower()


@pytest.mark.asyncio
async def test_auto_research_planner_tries_alternate_provider_before_generic_fallback(
    monkeypatch,
):
    service = AgentService.__new__(AgentService)
    calls: list[str] = []

    async def fake_available(primary_provider=None):
        return ["ollama"]

    async def fake_call_llm_json(
        *,
        system_prompt,
        user_prompt,
        schema,
        timeout_seconds=None,
        provider_override=None,
        on_chunk=None,
    ):
        calls.append(provider_override or "primary")
        if provider_override != "ollama":
            raise TimeoutError("primary planner unavailable")
        payload = json.loads(user_prompt)
        assert payload["subject_name"] == "MEMA · Memory Alpha Corp."
        return {
            "should_research": True,
            "query": "MEMA Memory Alpha NAND pricing HBM demand earnings guidance",
            "target_label": "MEMA NAND/HBM mechanism check",
            "information_needs": [
                "direct NAND pricing and inventory evidence",
                "HBM demand spillover evidence for memory suppliers",
            ],
            "reason": "Primary provider failed, but a ready alternate provider can still plan the information need.",
        }

    monkeypatch.setattr(
        "investos.services.agent.available_llm_json_recovery_providers", fake_available
    )
    monkeypatch.setattr("investos.services.agent.call_llm_json", fake_call_llm_json)

    plan = await service._auto_research_plan(
        message="how does mema look in the long run? can it hit 26% cagr?",
        subject_type="entity",
        subject_name="MEMA · Memory Alpha Corp.",
        packet_context={
            "direct_evidence": [],
            "connected_evidence": [
                {"text": "MEMB and MEMA share NAND memory exposure."}
            ],
            "historical_evidence": [],
            "contradiction_evidence": [],
            "gap_flags": ["no_coverage_map"],
            "portfolio_context": {
                "top_holdings": [{"ticker": "MEMA"}, {"ticker": "MEMB"}]
            },
        },
        reasoning_result={
            "is_fallback": True,
            "stance": "no_view",
            "confidence_band": "very_low",
        },
    )

    assert calls == ["primary", "ollama"]
    assert plan["should_research"] is True
    assert plan["planner_recovery_provider"] == "ollama"
    assert "broader external evidence" not in plan["information_needs"]
    assert "NAND pricing" in plan["query"]


def test_pruning_guardrails_retain_by_default_and_review_large_batches():
    safe = {
        "node_id": "11111111-1111-1111-1111-111111111111",
        "node_type": "fact",
        "category": "duplicate",
        "confidence": 0.91,
        "reason": "Near-identical to newer fact.",
    }
    weak = {
        "node_id": "22222222-2222-2222-2222-222222222222",
        "node_type": "claim",
        "category": "trivial",
        "confidence": 0.5,
        "reason": "Seems unimportant.",
    }
    approved, rejected_count, review_required, detail = (
        PruningService._screen_prune_candidates(
            [safe, weak],
            active_node_count=50,
        )
    )
    assert approved == [safe]
    assert rejected_count == 1
    assert review_required is False
    assert detail == ""

    many_safe = [safe | {"node_id": str(index).zfill(32)} for index in range(8)]
    approved, rejected_count, review_required, detail = (
        PruningService._screen_prune_candidates(
            many_safe,
            active_node_count=100,
        )
    )
    assert approved == []
    assert rejected_count == len(many_safe)
    assert review_required is True
    assert "review required" in detail.lower()


def test_deterministic_knowledge_status_reports_actual_graph_state():
    service = AgentService.__new__(AgentService)
    result = service._deterministic_operating_answer(
        {
            "query_type": "knowledge_status",
            "knowledge_status": {
                "subject_name": "MEMA · Memory Alpha Corp.",
                "query_terms": ["mema", "hbf"],
                "direct_active_count": 2,
                "direct_deprecated_count": 1,
                "direct_term_matches": [
                    {
                        "type": "fact",
                        "text": "MEMA has no direct HBF node yet; HBF demand is a research gap.",
                    }
                ],
                "matching_active_nodes": [],
                "matching_deprecated_nodes": [],
                "searched_at": "2026-06-18T00:36:00+00:00",
            },
        }
    )
    assert result is not None
    message = result["assistant_message"]
    assert "I checked Knowledge for MEMA" in message
    assert "2 active directly linked knowledge nodes" in message
    assert "HBF demand is a research gap" in message


def test_portfolio_peer_context_connects_holdings_by_stored_knowledge_terms():
    exposures = PortfolioPeerContextService.score_descriptors(
        [
            {
                "entity_id": "11111111-1111-1111-1111-111111111111",
                "ticker": "AAA",
                "name": "Alpha Storage",
                "weight_pct": 24.0,
                "sector": None,
                "industry": None,
                "name_terms": ["aaa", "alpha", "storage"],
                "knowledge_terms": [
                    "alpha",
                    "storage",
                    "nand",
                    "flash",
                    "enterprise",
                    "ssd",
                ],
            },
            {
                "entity_id": "22222222-2222-2222-2222-222222222222",
                "ticker": "BBB",
                "name": "Beta Memory",
                "weight_pct": 15.0,
                "sector": None,
                "industry": None,
                "name_terms": ["bbb", "beta", "memory"],
                "knowledge_terms": [
                    "beta",
                    "memory",
                    "nand",
                    "flash",
                    "enterprise",
                    "ssd",
                    "hbm",
                ],
            },
            {
                "entity_id": "33333333-3333-3333-3333-333333333333",
                "ticker": "CCC",
                "name": "Clinical Care",
                "weight_pct": 10.0,
                "sector": None,
                "industry": None,
                "name_terms": ["ccc", "clinical", "care"],
                "knowledge_terms": ["clinical", "care", "reimbursement"],
            },
        ],
        limit=5,
    )
    assert len(exposures) == 1
    exposure = exposures[0]
    assert exposure["source_ticker"] == "AAA"
    assert exposure["target_ticker"] == "BBB"
    assert {"nand", "flash", "enterprise", "ssd"} <= set(exposure["shared_terms"])
    assert "stored knowledge overlaps" in exposure["reason"]


def test_auto_research_target_label_preserves_question_phrase():
    service = AgentService.__new__(AgentService)
    label = service._auto_research_target_label(
        "how does mema look in the long run? can it hit 26% cagr?",
        "MEMA · Memory Alpha Corp.",
        "entity",
    )
    assert label.startswith("MEMA · Memory Alpha Corp.: how does mema look")
    assert "run / can / hit / cagr" not in label


def test_review_label_key_matches_theme_and_security_duplicates():
    assert ReviewService._label_key("MEMX · Tradr 2X Long MEMA Daily ETF") == (
        ReviewService._label_key("MEMX Tradr 2X Long MEMA Daily ETF")
    )
    assert ReviewService._label_key("Tradr 2X Long MEMA Daily ETF") == (
        ReviewService._label_key("tradr 2x long mema daily etf")
    )
