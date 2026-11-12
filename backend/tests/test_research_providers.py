import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest

from investos.core.research_providers import (
    ProviderSearchResponse,
    configured_research_providers,
    normalize_search_results,
    search_research_provider,
)
from investos.core.url_security import UrlFetchNetworkError
from investos.schemas.integrations import ResearchIntegrationSettingsUpdate
from investos.services.ingestion import FetchedUrlDocument
from investos.services.research import ResearchSearchResult, ResearchService
from investos.services.runtime_settings import (
    ResearchRuntimeSettings,
    RuntimeSettings,
    RuntimeSettingsStore,
)


def _research(**overrides):
    values = {
        "provider_order": ["searxng", "tavily"],
        "searxng_base_url": "http://127.0.0.1:8080",
        "api_key": "tavily-key",
        "tavily_monthly_credit_budget": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["searxng", "tavily"])
async def test_current_news_uses_provider_publication_and_news_filters(provider):
    async def handler(request):
        options = (
            dict(request.url.params)
            if provider == "searxng"
            else json.loads(request.content)
        )
        assert options["time_range"] == "day"
        if provider == "searxng":
            assert options["categories"] == "news"
        else:
            assert options["topic"] == "news"
            assert options["include_published_date"] is True
            assert options["filter_by_published_date"] is True
            assert options["max_age_hours"] == 24
        return httpx.Response(200, json={"results": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await search_research_provider(
            provider=provider,
            client=client,
            query="market developments",
            search_depth="advanced",
            include_raw_content=False,
            searxng_base_url="https://search.example.test",
            news_window_hours=24,
        )


@pytest.mark.asyncio
async def test_undated_stale_or_future_news_does_not_block_fresh_provider_fallback(
    monkeypatch,
):
    now = datetime.now(UTC)
    calls = []
    stale = [
        {"url": "https://example.test/unknown", "published_date": None},
        {
            "url": "https://example.test/old",
            "published_date": (now - timedelta(days=10)).isoformat(),
        },
        {
            "url": "https://example.test/future",
            "published_date": (now + timedelta(days=1)).isoformat(),
        },
    ]
    fresh = {
        "url": "https://example.test/fresh",
        "published_date": (now - timedelta(minutes=10)).strftime(
            "%a, %d %b %Y %H:%M:%S GMT"
        ),
    }

    async def fake_search(**kwargs):
        calls.append(kwargs["provider"])
        assert kwargs["news_window_hours"] == 24
        return ProviderSearchResponse(
            kwargs["provider"],
            "ok",
            kwargs["query"],
            stale if kwargs["provider"] == "searxng" else [fresh],
        )

    monkeypatch.setattr(
        "investos.services.research.RuntimeSettingsStore.load",
        lambda: SimpleNamespace(research=_research()),
    )
    monkeypatch.setattr(
        "investos.services.research.search_research_provider", fake_search
    )
    monkeypatch.setattr(ResearchService, "_append_usage_log", lambda *_: None)
    service = ResearchService(None)
    service._record_discovery_results = AsyncMock(
        side_effect=lambda **kw: {i["url"]: uuid4() for i in kw["results"]}
    )
    service._update_discovery_outcome = AsyncMock()
    result = await service.search(
        query="market developments", title="Current news", news_window_hours=24
    )
    assert calls == ["searxng", "tavily"]
    assert result.results == [fresh]
    assert result.provider_attempts[0]["status"] == "no_recent_results"
    assert [
        c.kwargs["outcome"] for c in service._update_discovery_outcome.await_args_list
    ] == [
        "publication_unverified",
        "publication_outside_window",
        "publication_outside_window",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("fresh_second", [True, False])
async def test_known_first_candidate_does_not_hide_unseen_source(
    monkeypatch, fresh_second
):
    service = ResearchService(None)
    service.session = SimpleNamespace(commit=AsyncMock())
    duplicate = SimpleNamespace(id=uuid4(), url="https://example.test/known")
    service._find_recent_duplicate_research = AsyncMock(
        side_effect=[None, duplicate, None if fresh_second else duplicate]
    )
    service._discover = AsyncMock(
        return_value=ResearchSearchResult(
            True,
            "ok",
            "memory pricing",
            results=[
                {"title": "Known", "url": duplicate.url},
                {"title": "New", "url": "https://example.test/new"},
            ],
        )
    )
    service._update_discovery_outcome = AsyncMock()
    service.ingestion.fetch_url_document = AsyncMock(
        return_value=FetchedUrlDocument(
            url="https://example.test/new",
            canonical_url="https://example.test/new",
            title="New",
            content="New attributable source text.",
        )
    )
    service.source_learning.get_or_create_source_for_url = AsyncMock(
        return_value=SimpleNamespace(
            source=SimpleNamespace(id=uuid4()), inferred_type="web_research"
        )
    )
    created = SimpleNamespace(id=uuid4())
    service.ingestion.ingest_text = AsyncMock(return_value=created)
    monkeypatch.setattr(
        "investos.services.research.RuntimeSettingsStore.load",
        lambda: SimpleNamespace(research=_research(provider_order=["searxng"])),
    )
    monkeypatch.setattr(ResearchService, "_append_usage_log", lambda *_: None)
    monkeypatch.setattr(ResearchService, "_log_research_action", lambda *_a, **_k: None)
    monkeypatch.setattr(
        ResearchService, "_search_query_variants", lambda *_: ["memory pricing"]
    )
    result = await service.run_ad_hoc_request(
        query="memory pricing", title="Memory pricing", process_after_ingest=False
    )
    if fresh_second:
        assert result.started is True
        assert result.evidence_id == created.id
        service.ingestion.fetch_url_document.assert_awaited_once_with(
            "https://example.test/new"
        )
    else:
        assert result.reason == "duplicate_recent_research"
        assert result.evidence_id == duplicate.id
        service.ingestion.fetch_url_document.assert_not_awaited()


def test_legacy_tavily_runtime_settings_gain_free_first_provider_order():
    research = ResearchRuntimeSettings.model_validate(
        {"provider": "tavily", "api_key": "legacy-key"}
    )

    assert research.provider_order == ["searxng", "tavily"]
    assert configured_research_providers(research) == ["tavily"]


def test_normalize_search_results_rejects_non_web_and_duplicate_urls():
    results = normalize_search_results(
        "searxng",
        [
            {"title": "Primary", "url": "https://example.com/report#section"},
            {"title": "Duplicate", "url": "https://example.com/report"},
            {"title": "Local file", "url": "file:///tmp/report"},
            {"title": "Credential URL", "url": "https://user:pass@example.com/"},
            {"title": "Loopback URL", "url": "http://127.0.0.1/private"},
        ],
    )

    assert results == [
        {
            "title": "Primary",
            "url": "https://example.com/report",
            "content": "",
            "raw_content": None,
            "content_kind": "snippet",
            "score": None,
            "published_date": None,
            "discovery_provider": "searxng",
            "engines": [],
        }
    ]


@pytest.mark.asyncio
async def test_searxng_adapter_uses_json_api_and_normalizes_results():
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/search"
        assert request.url.params["q"] == "memory pricing"
        assert request.url.params["format"] == "json"
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "Memory report",
                        "url": "https://example.com/memory",
                        "content": "Search snippet",
                        "engines": ["brave"],
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await search_research_provider(
            provider="searxng",
            client=client,
            query="memory pricing",
            search_depth="basic",
            include_raw_content=False,
            searxng_base_url="http://search.local",
        )

    assert result.status == "ok"
    assert result.results[0]["content_kind"] == "snippet"
    assert result.results[0]["discovery_provider"] == "searxng"


@pytest.mark.asyncio
async def test_provider_adapter_rejects_malformed_result_payload():
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=["not", "a", "result", "object"])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await search_research_provider(
            provider="searxng",
            client=client,
            query="memory pricing",
            search_depth="basic",
            include_raw_content=False,
            searxng_base_url="http://search.local",
        )

    assert result.status == "research_failed"
    assert result.results == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "expected_status"),
    [(429, "rate_limited"), (432, "research_provider_limit_exceeded")],
)
async def test_tavily_adapter_preserves_limit_status_without_counting_credits(
    status_code,
    expected_status,
):
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await search_research_provider(
            provider="tavily",
            client=client,
            query="memory pricing",
            search_depth="advanced",
            include_raw_content=True,
            tavily_api_key="test-key",
        )

    assert result.status == expected_status
    assert result.estimated_credits == 0


@pytest.mark.asyncio
async def test_discovery_stops_after_searxng_success(monkeypatch):
    calls: list[str] = []

    async def fake_search(**kwargs):
        calls.append(kwargs["provider"])
        return ProviderSearchResponse(
            provider=kwargs["provider"],
            status="ok",
            query=kwargs["query"],
            results=[
                {
                    "title": "Source",
                    "url": "https://example.com/source",
                    "content": "snippet",
                    "raw_content": None,
                }
            ],
        )

    monkeypatch.setattr(
        "investos.services.research.RuntimeSettingsStore.load",
        lambda: SimpleNamespace(research=_research()),
    )
    monkeypatch.setattr(
        "investos.services.research.search_research_provider", fake_search
    )
    monkeypatch.setattr(ResearchService, "_append_usage_log", lambda *_args: None)

    result = await ResearchService(None).search(query="memory", title="Memory")

    assert result.reason == "ok"
    assert result.provider == "searxng"
    assert calls == ["searxng"]


@pytest.mark.asyncio
async def test_discovery_exhausts_free_query_variants_before_tavily(monkeypatch):
    calls: list[tuple[str, str]] = []

    async def fake_search(**kwargs):
        provider = kwargs["provider"]
        query = kwargs["query"]
        calls.append((provider, query))
        if provider == "searxng" and query == "second":
            return ProviderSearchResponse(
                provider=provider,
                status="ok",
                query=query,
                results=[
                    {
                        "title": "Source",
                        "url": "https://example.com/source",
                        "content": "snippet",
                        "raw_content": None,
                    }
                ],
            )
        return ProviderSearchResponse(
            provider=provider,
            status="no_result",
            query=query,
        )

    service = ResearchService(None)
    monkeypatch.setattr(
        "investos.services.research.RuntimeSettingsStore.load",
        lambda: SimpleNamespace(research=_research()),
    )
    monkeypatch.setattr(
        "investos.services.research.search_research_provider", fake_search
    )
    monkeypatch.setattr(
        service, "_search_query_variants", lambda _query: ["first", "second"]
    )
    monkeypatch.setattr(ResearchService, "_append_usage_log", lambda *_args: None)

    result = await service.search(query="memory", title="Memory")

    assert result.provider == "searxng"
    assert calls == [("searxng", "first"), ("searxng", "second")]


@pytest.mark.asyncio
async def test_discovery_falls_back_to_tavily_after_empty_searxng(monkeypatch):
    calls: list[str] = []

    async def fake_search(**kwargs):
        provider = kwargs["provider"]
        calls.append(provider)
        if provider == "searxng":
            return ProviderSearchResponse(
                provider=provider,
                status="no_result",
                query=kwargs["query"],
            )
        return ProviderSearchResponse(
            provider=provider,
            status="ok",
            query=kwargs["query"],
            results=[
                {
                    "title": "Fallback source",
                    "url": "https://example.com/fallback",
                    "content": "full extraction",
                    "raw_content": "full extraction",
                }
            ],
            estimated_credits=1,
        )

    monkeypatch.setattr(
        "investos.services.research.RuntimeSettingsStore.load",
        lambda: SimpleNamespace(research=_research()),
    )
    monkeypatch.setattr(
        "investos.services.research.search_research_provider", fake_search
    )
    monkeypatch.setattr(ResearchService, "_append_usage_log", lambda *_args: None)

    result = await ResearchService(None).search(query="memory", title="Memory")

    assert result.provider == "tavily"
    assert calls == ["searxng", "tavily"]
    assert result.provider_attempts[1]["fallback_reason"] == "no_result"


@pytest.mark.asyncio
async def test_discovery_falls_back_to_tavily_after_searxng_failure(monkeypatch):
    calls: list[str] = []

    async def fake_search(**kwargs):
        provider = kwargs["provider"]
        calls.append(provider)
        if provider == "searxng":
            return ProviderSearchResponse(
                provider=provider,
                status="research_failed",
                query=kwargs["query"],
            )
        return ProviderSearchResponse(
            provider=provider,
            status="ok",
            query=kwargs["query"],
            results=[
                {
                    "title": "Fallback source",
                    "url": "https://example.com/fallback",
                    "content": "full extraction",
                    "raw_content": "full extraction",
                }
            ],
            estimated_credits=1,
        )

    monkeypatch.setattr(
        "investos.services.research.RuntimeSettingsStore.load",
        lambda: SimpleNamespace(research=_research()),
    )
    monkeypatch.setattr(
        "investos.services.research.search_research_provider", fake_search
    )
    monkeypatch.setattr(ResearchService, "_append_usage_log", lambda *_args: None)

    result = await ResearchService(None).search(query="memory", title="Memory")

    assert result.provider == "tavily"
    assert calls == ["searxng", "tavily"]
    assert result.provider_attempts[1]["fallback_reason"] == "research_failed"


@pytest.mark.asyncio
async def test_tavily_budget_blocks_metered_fallback(monkeypatch):
    calls: list[str] = []

    async def fake_search(**kwargs):
        calls.append(kwargs["provider"])
        return ProviderSearchResponse(
            provider="searxng",
            status="no_result",
            query=kwargs["query"],
        )

    monkeypatch.setattr(
        "investos.services.research.RuntimeSettingsStore.load",
        lambda: SimpleNamespace(research=_research(tavily_monthly_credit_budget=5)),
    )
    monkeypatch.setattr(
        "investos.services.research.search_research_provider", fake_search
    )
    monkeypatch.setattr(ResearchService, "_append_usage_log", lambda *_args: None)
    monkeypatch.setattr(
        ResearchService, "_tavily_credits_used_this_month", lambda *_args: 5
    )

    result = await ResearchService(None).search(query="memory", title="Memory")

    assert result.reason == "research_provider_budget_exhausted"
    assert calls == ["searxng"]


@pytest.mark.asyncio
async def test_searxng_snippet_is_not_ingested_when_source_page_cannot_be_fetched(
    monkeypatch,
):
    service = ResearchService(None)
    service._find_recent_duplicate_research = AsyncMock(return_value=None)
    service._discover = AsyncMock(
        return_value=ResearchSearchResult(
            searched=True,
            reason="ok",
            query="memory",
            provider="searxng",
            results=[
                {
                    "title": "Blocked page",
                    "url": "https://example.com/blocked",
                    "content": "A search-engine snippet is not evidence.",
                    "raw_content": None,
                }
            ],
        )
    )
    service.ingestion.fetch_url_document = AsyncMock(
        side_effect=UrlFetchNetworkError("blocked")
    )
    service.ingestion.ingest_text = AsyncMock(
        side_effect=AssertionError("snippet must not be ingested")
    )
    monkeypatch.setattr(
        "investos.services.research.RuntimeSettingsStore.load",
        lambda: SimpleNamespace(
            research=_research(
                provider_order=["searxng"],
                api_key=None,
            )
        ),
    )
    monkeypatch.setattr(ResearchService, "_append_usage_log", lambda *_args: None)
    monkeypatch.setattr(
        ResearchService, "_log_research_action", lambda *_args, **_kwargs: None
    )

    result = await service.run_ad_hoc_request(
        query="memory",
        title="Memory",
        process_after_ingest=False,
    )

    assert result.started is False
    assert result.reason == "no_fetchable_source"
    service.ingestion.ingest_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_ingestion_exhausts_searxng_variants_before_tavily(monkeypatch):
    calls: list[tuple[str, str]] = []

    async def fake_search(**kwargs):
        provider = kwargs["provider"]
        query = kwargs["query"]
        calls.append((provider, query))
        assert provider == "searxng"
        suffix = "blocked" if query == "first" else "source"
        return ProviderSearchResponse(
            provider=provider,
            status="ok",
            query=query,
            results=[
                {
                    "title": "Source",
                    "url": f"https://example.com/{suffix}",
                    "content": "snippet",
                    "raw_content": None,
                }
            ],
        )

    async def fetch_document(url):
        if url.endswith("/blocked"):
            raise UrlFetchNetworkError("blocked")
        return FetchedUrlDocument(
            url=url,
            canonical_url=url,
            title="Source",
            content="Full source-page text.",
        )

    service = ResearchService(None)
    service.session = SimpleNamespace(commit=AsyncMock())
    service._record_discovery_results = AsyncMock(return_value={})
    service._find_recent_duplicate_research = AsyncMock(return_value=None)
    service.ingestion.fetch_url_document = AsyncMock(side_effect=fetch_document)
    service.ingestion.ingest_text = AsyncMock(return_value=SimpleNamespace(id=uuid4()))
    service.source_learning.get_or_create_source_for_url = AsyncMock(
        return_value=SimpleNamespace(
            source=SimpleNamespace(id=uuid4()),
            inferred_type="web_research",
        )
    )
    monkeypatch.setattr(
        "investos.services.research.RuntimeSettingsStore.load",
        lambda: SimpleNamespace(research=_research()),
    )
    monkeypatch.setattr(
        "investos.services.research.search_research_provider", fake_search
    )
    monkeypatch.setattr(
        service, "_search_query_variants", lambda _query: ["first", "second"]
    )
    monkeypatch.setattr(ResearchService, "_append_usage_log", lambda *_args: None)
    monkeypatch.setattr(
        ResearchService, "_log_research_action", lambda *_args, **_kwargs: None
    )

    result = await service.run_ad_hoc_request(
        query="memory",
        title="Memory",
        process_after_ingest=False,
    )

    assert result.started is True
    assert result.query == "second"
    assert calls == [("searxng", "first"), ("searxng", "second")]


@pytest.mark.asyncio
async def test_discovery_observation_preserves_snippet_without_evidence_promotion():
    added = []

    class FakeSession:
        def add(self, value):
            value.id = uuid4()
            added.append(value)

        async def flush(self):
            pass

    service = ResearchService(FakeSession())

    ids = await service._record_discovery_results(
        provider="searxng",
        request_id="request-1",
        input_query="memory pricing",
        effective_query="memory pricing filing",
        title="Memory pricing",
        results=[
            {
                "title": "Memory report",
                "url": "https://example.com/report",
                "content": "Search-result context that has not been fetched.",
                "content_kind": "snippet",
                "engines": ["brave"],
            }
        ],
        metadata={
            "subject_type": "position",
            "subject_id": "abc",
            "subject_name": "MU",
        },
    )

    observation = added[0]
    assert ids["https://example.com/report"] == observation.id
    assert observation.snippet == "Search-result context that has not been fetched."
    assert observation.content_kind == "snippet"
    assert observation.outcome == "observed"
    assert observation.evidence_id is None
    assert observation.subject_name == "MU"


@pytest.mark.asyncio
async def test_discovery_observation_collapses_duplicate_urls_within_one_response():
    added = []

    class FakeSession:
        def add(self, value):
            value.id = uuid4()
            added.append(value)

        async def flush(self):
            pass

    service = ResearchService(FakeSession())

    ids = await service._record_discovery_results(
        provider="searxng",
        request_id="request-1",
        input_query="memory pricing",
        effective_query="memory pricing",
        title="Memory pricing",
        results=[
            {"title": "First", "url": "https://example.com/report"},
            {"title": "Duplicate", "url": "https://example.com/report"},
        ],
        metadata={},
    )

    assert len(added) == 1
    assert list(ids) == ["https://example.com/report"]


@pytest.mark.asyncio
async def test_direct_source_page_is_ingested_with_discovery_provenance(monkeypatch):
    service = ResearchService(None)
    evidence_id = uuid4()
    source_id = uuid4()
    service.session = SimpleNamespace(commit=AsyncMock())
    service._find_recent_duplicate_research = AsyncMock(return_value=None)
    service._discover = AsyncMock(
        return_value=ResearchSearchResult(
            searched=True,
            reason="ok",
            query="memory pricing",
            provider="searxng",
            provider_attempts=[
                {
                    "provider": "searxng",
                    "status": "ok",
                    "fallback_reason": None,
                }
            ],
            results=[
                {
                    "title": "Memory report",
                    "url": "https://example.com/report",
                    "content": "Search snippet",
                    "raw_content": None,
                }
            ],
        )
    )
    service.ingestion.fetch_url_document = AsyncMock(
        return_value=FetchedUrlDocument(
            url="https://example.com/report",
            canonical_url="https://example.com/canonical-report",
            title="Memory report",
            content="Full source-page text.",
        )
    )
    service.source_learning.get_or_create_source_for_url = AsyncMock(
        return_value=SimpleNamespace(
            source=SimpleNamespace(id=source_id),
            inferred_type="web_research",
        )
    )
    service.ingestion.ingest_text = AsyncMock(
        return_value=SimpleNamespace(id=evidence_id)
    )
    monkeypatch.setattr(
        "investos.services.research.RuntimeSettingsStore.load",
        lambda: SimpleNamespace(
            research=_research(provider_order=["searxng"], api_key=None)
        ),
    )
    monkeypatch.setattr(ResearchService, "_append_usage_log", lambda *_args: None)
    monkeypatch.setattr(
        ResearchService, "_log_research_action", lambda *_args, **_kwargs: None
    )

    result = await service.run_ad_hoc_request(
        query="memory pricing",
        title="Memory pricing",
        process_after_ingest=False,
    )

    payload = service.ingestion.ingest_text.await_args.args[0]
    assert result.started is True
    assert payload.content == "Full source-page text."
    assert payload.metadata_json["content_origin"] == "direct_page"
    assert payload.metadata_json["discovery_provider"] == "searxng"
    assert payload.metadata_json["canonical_source_url"] == (
        "https://example.com/canonical-report"
    )
    assert payload.metadata_json["discovery_result_rank"] == 1
    assert payload.metadata_json["provider_attempts"][0]["status"] == "ok"


def test_runtime_validation_rejects_invalid_searxng_endpoint():
    runtime = RuntimeSettings(
        research=ResearchRuntimeSettings(
            searxng_base_url="file:///tmp/searxng",
        )
    )

    with pytest.raises(ValueError, match="SearXNG base URL"):
        RuntimeSettingsStore._validate(runtime)


def test_research_update_prefers_explicit_order_and_can_clear_budget():
    current = ResearchRuntimeSettings(
        provider_order=["searxng", "tavily"],
        tavily_monthly_credit_budget=100,
    )

    updated = RuntimeSettingsStore._update_research(
        current,
        ResearchIntegrationSettingsUpdate(
            provider="searxng",
            provider_order=["tavily", "searxng"],
            tavily_monthly_credit_budget=None,
        ),
    )

    assert updated.provider_order == ["tavily", "searxng"]
    assert updated.tavily_monthly_credit_budget is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("searxng_base_url", "expected_ready"),
    [("", False), ("http://127.0.0.1:8080", True)],
)
async def test_tavily_usage_failure_only_reports_ready_with_free_provider(
    monkeypatch,
    searxng_base_url,
    expected_ready,
):
    class FailingClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return None

        async def get(self, *_args, **_kwargs):
            raise httpx.ConnectError("unavailable")

    monkeypatch.setattr(
        "investos.services.research.RuntimeSettingsStore.load",
        lambda: SimpleNamespace(research=_research(searxng_base_url=searxng_base_url)),
    )
    monkeypatch.setattr(
        "investos.services.research.httpx.AsyncClient",
        lambda **_kwargs: FailingClient(),
    )
    monkeypatch.setattr(ResearchService, "recent_request_log", lambda **_kwargs: [])

    snapshot = await ResearchService.current_usage_snapshot()

    assert snapshot["ready"] is expected_ready
