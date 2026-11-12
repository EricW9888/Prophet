"""Publisher discovery must work without inference and preserve availability."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from investos.config import settings
from investos.db import get_session
from investos.main import app
from investos.models.evidence import (
    EvidenceProcessingState,
    RawEvidence,
    ResearchDiscoveryObservation,
)
from investos.models.news import PublisherFeed
from investos.schemas.evidence import RawEvidenceCreate
from investos.services.agent import AgentService
from investos.services.automation import AutomationCoordinator
from investos.services.ingestion import FetchedUrlDocument, IngestionService
from investos.services.news_intake import NewsIntakeService, parse_feed
from investos.services.research import ResearchSearchResult, ResearchService
from investos.workers.extraction import ExtractionWorker
from tests.test_investor_workflows import workflow_session


def item(
    *,
    title="Major unheld supplier changes leadership",
    url="https://example.test/new",
    rank=1,
):
    return {
        "title": title,
        "url": url,
        "content": "Publisher headline summary, not extracted evidence.",
        "published_date": (datetime.now(UTC) - timedelta(hours=1)).isoformat(),
        "updated_date": None,
        "feed_rank": rank,
    }


async def feed(session):
    row = PublisherFeed(
        url=f"https://example.test/{uuid4()}/rss", name="Synthetic publisher"
    )
    session.add(row)
    await session.flush()
    return row


def document(url):
    return FetchedUrlDocument(
        url=url,
        canonical_url=url,
        title="Leadership announcement",
        content="The company announced a leadership review. The outcome remains unconfirmed. This is source text, not a verified portfolio consequence.",
    )


def test_feedparser_handles_rss_and_atom_without_network():
    rss = """<rss version="2.0"><channel><title>News</title><item><title>Supplier update</title><link>https://example.test/a#section</link><pubDate>Wed, 11 Nov 2026 20:00:00 GMT</pubDate><description>Summary</description></item><item><title>Unsafe</title><link>http://127.0.0.1/private</link></item></channel></rss>"""
    rows = parse_feed(rss)
    assert len(rows) == 1
    assert rows[0]["url"] == "https://example.test/a"
    assert rows[0]["feed_rank"] == 1
    atom = """<feed xmlns="http://www.w3.org/2005/Atom"><title>News</title><id>urn:news</id><updated>2026-11-11T20:00:00Z</updated><entry><id>urn:a</id><title>Supplier update</title><link href="https://example.test/a"/><published>2026-11-11T20:00:00Z</published><updated>2026-11-11T21:00:00Z</updated></entry></feed>"""
    assert parse_feed(atom)[0]["updated_date"] == "2026-11-11T21:00:00Z"
    with pytest.raises(ValueError):
        parse_feed("<html><body>Access denied</body></html>")


def test_news_job_never_waits_for_the_model_circuit(monkeypatch):
    def forbidden():
        raise AssertionError("News acquisition cannot depend on inference readiness")

    monkeypatch.setattr(
        "investos.services.automation.ProviderHealthStore.public_status", forbidden
    )
    assert AutomationCoordinator()._provider_defers("news_intake") is False


async def test_observation_versions_are_idempotent_and_never_backdated(
    workflow_session,
):
    session = workflow_session
    subscription = await feed(session)
    service = NewsIntakeService(session)
    first = item()
    stamp = datetime.now(UTC)
    assert await service._record(subscription, [first], observed_at=stamp) == 1
    assert (
        await service._record(
            subscription, [first], observed_at=stamp + timedelta(minutes=1)
        )
        == 0
    )
    changed = {**first, "title": "Supplier confirms interim leadership"}
    assert (
        await service._record(
            subscription, [changed], observed_at=stamp + timedelta(minutes=2)
        )
        == 1
    )
    rows = list(
        await session.scalars(
            select(ResearchDiscoveryObservation)
            .where(ResearchDiscoveryObservation.query == subscription.url)
            .order_by(ResearchDiscoveryObservation.observed_at)
        )
    )
    assert len(rows) == 2
    assert rows[0].outcome == "superseded_by_feed_update"
    assert rows[0].observed_at == stamp
    assert rows[1].observed_at == stamp + timedelta(minutes=2)
    assert rows[1].metadata_json["published_date"] == first["published_date"]


async def test_normal_intake_retains_then_fetches_unheld_headline_without_model_work(
    workflow_session, monkeypatch
):
    session = workflow_session
    monkeypatch.setattr(settings, "NEWS_FEED_URLS", [])
    monkeypatch.setattr(settings, "NEWS_INTAKE_FETCH_BATCH_SIZE", 1)
    subscription = await feed(session)
    monkeypatch.setattr(
        NewsIntakeService, "_read_feed", AsyncMock(return_value=[item()])
    )
    monkeypatch.setattr(
        IngestionService, "fetch_url_document", AsyncMock(side_effect=document)
    )
    no_extraction = AsyncMock(
        side_effect=AssertionError("Acquisition must not await extraction")
    )
    monkeypatch.setattr(ExtractionWorker, "process_evidence", no_extraction)
    result = await NewsIntakeService(session).run()
    assert result["new_observations"] == 1
    assert result["articles_fetched"] == 1
    observation = await session.scalar(
        select(ResearchDiscoveryObservation).where(
            ResearchDiscoveryObservation.query == subscription.url
        )
    )
    evidence = await session.get(RawEvidence, observation.evidence_id)
    assert observation.outcome == "ingested_evidence"
    assert evidence.is_processed is False
    assert evidence.public_time is None
    assert (
        evidence.metadata_json["feed_published_date"]
        == observation.metadata_json["published_date"]
    )
    assert (
        evidence.metadata_json["discovered_at"] == observation.observed_at.isoformat()
    )
    assert await session.scalar(
        select(EvidenceProcessingState.id).where(
            EvidenceProcessingState.raw_evidence_id == evidence.id
        )
    )
    no_extraction.assert_not_awaited()
    monkeypatch.setattr(
        ResearchService,
        "search",
        AsyncMock(return_value=ResearchSearchResult(True, "no_result", "markets", [])),
    )
    context = await AgentService(session)._portfolio_fresh_checks(
        {}, "Important current developments"
    )
    assert context["results"][0]["title"] == "Major unheld supplier changes leadership"
    assert context["results"][0]["content_kind"] == "feed_summary"
    assert context["checks"][0]["fresh_feeds"] == 1
    # Normal reruns neither rediscover nor fetch an unchanged headline.
    again = await NewsIntakeService(session).run()
    assert again["articles_fetched"] == 0


async def test_pausing_feed_preserves_leads_without_fetching_them(
    workflow_session, monkeypatch
):
    session = workflow_session
    subscription = await feed(session)
    service = NewsIntakeService(session)
    await service._record(subscription, [item()])
    subscription.enabled = False
    await session.flush()
    fetcher = AsyncMock(side_effect=AssertionError("Paused feed cannot fetch"))
    monkeypatch.setattr(IngestionService, "fetch_url_document", fetcher)
    assert (await service._fetch_pending())["articles_fetched"] == 0
    assert (await service.status())["pending_count"] >= 1
    fetcher.assert_not_awaited()


async def test_stale_feed_checkpoint_is_not_current_coverage(workflow_session):
    session = workflow_session
    subscription = await feed(session)
    service = NewsIntakeService(session)
    await service._record(subscription, [item()])
    subscription.last_success_at = datetime.now(UTC) - timedelta(hours=2)
    await session.flush()
    assert (await service.recent_context())["results"] == []
    assert (await service.status())["pending_count"] >= 1


async def test_feed_api_paginates_retained_discoveries_and_pause_is_durable(
    workflow_session, monkeypatch
):
    session = workflow_session
    subscription = await feed(session)
    service = NewsIntakeService(session)
    await service._record(
        subscription,
        [item(url=f"https://example.test/story{i}", rank=i + 1) for i in range(25)],
    )

    async def override():
        yield session

    app.dependency_overrides[get_session] = override
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://127.0.0.1:8000",
            headers={"Origin": "http://127.0.0.1:3000"},
        ) as client:
            first = await client.get("/api/news-intake")
            second = await client.get("/api/news-intake?offset=20")
            assert first.status_code == second.status_code == 200
            assert len(first.json()["items"]) == 20
            assert len(second.json()["items"]) == 5
            assert (
                len({r["id"] for r in first.json()["items"] + second.json()["items"]})
                == 25
            )
            assert all("snippet" not in r for r in first.json()["items"])
            paused = await client.patch(
                f"/api/news-intake/feeds/{subscription.id}", json={"enabled": False}
            )
            assert paused.status_code == 200
            monkeypatch.setattr(settings, "NEWS_FEED_URLS", [subscription.url])
            await service.ensure_defaults()
            await session.refresh(subscription)
            assert subscription.enabled is False
            blocked = await client.post(
                "/api/news-intake/feeds",
                json={"name": "Unsafe", "url": "http://127.0.0.1/private"},
            )
            assert blocked.status_code == 400
    finally:
        app.dependency_overrides.pop(get_session, None)


async def test_fetch_failure_keeps_a_retryable_lead_and_no_evidence(
    workflow_session, monkeypatch
):
    session = workflow_session
    subscription = await feed(session)
    service = NewsIntakeService(session)
    await service._record(subscription, [item()])
    monkeypatch.setattr(
        IngestionService,
        "fetch_url_document",
        AsyncMock(side_effect=ValueError("Publisher blocked page")),
    )
    result = await service._fetch_pending()
    row = await session.scalar(
        select(ResearchDiscoveryObservation).where(
            ResearchDiscoveryObservation.query == subscription.url
        )
    )
    assert result["articles_failed"] == 1
    assert row.outcome == "fetch_failed"
    assert row.evidence_id is None
    assert row.metadata_json["fetch_attempts"] == 1
    assert row.metadata_json["next_fetch_at"] > datetime.now(UTC).isoformat()
    assert (await service._fetch_pending())["articles_failed"] == 0


async def test_crash_after_raw_commit_recovers_existing_receipt(
    workflow_session, monkeypatch
):
    session = workflow_session
    subscription = await feed(session)
    service = NewsIntakeService(session)
    await service._record(subscription, [item()])
    row = await session.scalar(
        select(ResearchDiscoveryObservation).where(
            ResearchDiscoveryObservation.query == subscription.url
        )
    )
    evidence = await IngestionService(session).ingest_text(
        RawEvidenceCreate(
            title="Previously committed source",
            source_item_type="news",
            content="Previously fetched and committed source text.",
            metadata_json={"discovery_observation_id": str(row.id)},
        ),
        process_now=False,
    )
    fetcher = AsyncMock(
        side_effect=AssertionError("Committed receipt must be reconciled")
    )
    monkeypatch.setattr(IngestionService, "fetch_url_document", fetcher)
    before = await session.scalar(select(func.count()).select_from(RawEvidence))
    result = await service._fetch_pending()
    assert result["articles_fetched"] == 0
    assert result["articles_recovered"] == 1
    assert row.evidence_id == evidence.id
    assert row.metadata_json["recovered_receipt"] is True
    assert await session.scalar(select(func.count()).select_from(RawEvidence)) == before
    fetcher.assert_not_awaited()


async def test_priority_fetch_reserves_capacity_for_oldest_lead(
    workflow_session, monkeypatch
):
    session = workflow_session
    subscription = await feed(session)
    service = NewsIntakeService(session)
    now = datetime.now(UTC)
    await service._record(
        subscription,
        [
            {
                **item(url="https://example.test/old", rank=90),
                "published_date": (now - timedelta(hours=3)).isoformat(),
            }
        ],
        observed_at=now - timedelta(hours=2),
    )
    await service._record(
        subscription,
        [item(url=f"https://example.test/new{i}", rank=1) for i in range(5)],
        observed_at=now,
    )
    monkeypatch.setattr(settings, "NEWS_INTAKE_FETCH_BATCH_SIZE", 3)
    fetcher = AsyncMock(side_effect=document)
    monkeypatch.setattr(IngestionService, "fetch_url_document", fetcher)
    assert (await service._fetch_pending())["articles_fetched"] == 3
    assert "https://example.test/old" in [a.args[0] for a in fetcher.await_args_list]


async def test_lost_fetch_lease_cannot_write_a_second_workers_result(
    workflow_session, monkeypatch
):
    session = workflow_session
    subscription = await feed(session)
    service = NewsIntakeService(session)
    await service._record(subscription, [item()])

    async def steal_lease(_self, url):
        row = await session.scalar(
            select(ResearchDiscoveryObservation).where(
                ResearchDiscoveryObservation.query == subscription.url
            )
        )
        row.metadata_json = {**row.metadata_json, "fetch_token": "another-worker"}
        await session.commit()
        return document(url)

    monkeypatch.setattr(IngestionService, "fetch_url_document", steal_lease)
    before = await session.scalar(select(func.count()).select_from(RawEvidence))
    assert (await service._fetch_pending())["articles_fetched"] == 0
    assert await session.scalar(select(func.count()).select_from(RawEvidence)) == before
