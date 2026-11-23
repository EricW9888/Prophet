"""Publisher discovery must work without inference and preserve availability."""

from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from youtube_transcript_api import TranscriptsDisabled

from investos.config import Settings, settings
from investos.core.prompting import compact_fresh_research_context
from investos.core.url_security import PublicDocument, UrlFetchNetworkError
from investos.db import get_session
from investos.main import app
from investos.models.coverage import CoverageMap, QuestionHistory, Resolution
from investos.models.entity import Entity
from investos.models.evidence import (
    EvidenceProcessingState,
    RawEvidence,
    ResearchDiscoveryObservation,
)
from investos.models.news import PublisherFeed
from investos.schemas.evidence import RawEvidenceCreate
from investos.services.agent import AgentService
from investos.services.automation import AutomationCoordinator, JobTelemetry
from investos.services.ingestion import FetchedUrlDocument, IngestionService
from investos.services.news_intake import NewsIntakeService, parse_feed
from investos.services.question_lifecycle import QuestionLifecycleService
from investos.services.research import (
    ResearchRunResult,
    ResearchSearchResult,
    ResearchService,
)
from investos.services.source import SourceService
from investos.services.youtube import YouTubeService
from investos.workers.extraction import ExtractionWorker
from tests.test_extraction_retention import extraction_payload
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
    updated_only = parse_feed(
        atom.replace("<published>2026-11-11T20:00:00Z</published>", "")
    )[0]
    assert updated_only["published_date"] is None
    assert updated_only["updated_date"] == "2026-11-11T21:00:00Z"
    with pytest.raises(ValueError):
        parse_feed("<html><body>Access denied</body></html>")


def test_news_job_never_waits_for_the_model_circuit(monkeypatch):
    def forbidden():
        raise AssertionError("News acquisition cannot depend on inference readiness")

    monkeypatch.setattr(
        "investos.services.automation.ProviderHealthStore.public_status", forbidden
    )
    assert AutomationCoordinator()._provider_defers("news_intake") is False


async def test_feed_aliases_reuse_exact_documents_but_keep_corrections_and_origins(
    workflow_session, monkeypatch
):
    session = workflow_session
    monkeypatch.setattr(settings, "NEWS_FEED_URLS", [])
    monkeypatch.setattr(settings, "NEWS_INTAKE_FETCH_BATCH_SIZE", 4)
    first, second = await feed(session), await feed(session)
    service = NewsIntakeService(session)
    canonical = f"https://example.test/{uuid4()}/filing"
    primary = document(canonical)
    lead, alias, revision = (f"{canonical}?receipt={i}" for i in range(3))
    syndicated = canonical.replace("example.test", "publisher.test")
    documents = {
        lead: replace(primary, url=lead),
        alias: replace(primary, url=alias),
        revision: replace(
            primary, url=revision, content="The board issued a correction."
        ),
        syndicated: replace(primary, url=syndicated),
    }
    monkeypatch.setattr(
        IngestionService,
        "fetch_url_document",
        AsyncMock(side_effect=documents.__getitem__),
    )
    await service._record(first, [item(url=lead), item(url=revision, rank=4)])
    await service._record(
        second, [item(url=alias, rank=2), item(url=syndicated, rank=3)]
    )
    result = await service._fetch_pending()

    observations = {
        row.url: row
        for row in await session.scalars(
            select(ResearchDiscoveryObservation).where(
                ResearchDiscoveryObservation.query.in_([first.url, second.url])
            )
        )
    }
    assert result["articles_fetched"] == 3
    assert result["articles_reused"] == 1
    assert len(observations) == 4
    assert len({row.evidence_id for row in observations.values()}) == 3
    assert observations[lead].evidence_id == observations[alias].evidence_id
    assert observations[alias].metadata_json["reused_receipt"] is True
    for row in observations.values():
        assert row.outcome == "ingested_evidence"
        assert row.metadata_json["canonical_source_url"] == canonical
        assert (
            datetime.fromisoformat(row.metadata_json["fetched_at"]) >= row.observed_at
        )


@pytest.mark.parametrize("same_poll", [True, False])
async def test_overlapping_feed_views_share_one_network_fetch(
    workflow_session, monkeypatch, same_poll
):
    service = NewsIntakeService(workflow_session)
    first, second = await feed(workflow_session), await feed(workflow_session)
    entry = item()
    monkeypatch.setattr(settings, "NEWS_INTAKE_FETCH_BATCH_SIZE", 3)
    fetcher = AsyncMock(side_effect=document)
    monkeypatch.setattr(IngestionService, "fetch_url_document", fetcher)
    await service._record(first, [entry])
    if not same_poll:
        assert (await service._fetch_pending())["articles_fetched"] == 1
    await service._record(second, [entry])
    result = await service._fetch_pending()
    assert result["articles_fetched"] == int(same_poll)
    assert result["articles_reused"] == 1
    fetcher.assert_awaited_once_with(entry["url"])
    rows = list(await workflow_session.scalars(select(ResearchDiscoveryObservation)))
    assert len(rows) == 2
    assert len({row.evidence_id for row in rows}) == 1
    reused = next(row for row in rows if row.metadata_json.get("reused_receipt"))
    assert reused.metadata_json["fetch_attempts"] == 0
    assert reused.metadata_json["linked_at"]
    assert not reused.metadata_json.get("fetched_at")

    await service._record(
        second, [{**entry, "title": "The publisher issues a correction"}]
    )
    await service._fetch_pending()
    assert fetcher.await_count == 2


async def test_feed_intake_retains_qualifiers_beyond_the_old_prefix(
    workflow_session, monkeypatch
):
    session = workflow_session
    monkeypatch.setattr(settings, "NEWS_FEED_URLS", [])
    monkeypatch.setattr(settings, "INGESTION_MAX_NOTE_CHARS", 512)
    subscription = await feed(session)
    article = (
        "Retained source context. " * 1200
        + "Correction: approval has not been granted."
    )
    monkeypatch.setattr(
        NewsIntakeService, "_read_feed", AsyncMock(return_value=[item()])
    )
    monkeypatch.setattr(
        IngestionService,
        "fetch_url_document",
        AsyncMock(
            return_value=FetchedUrlDocument(
                url="https://example.test/new",
                canonical_url="https://example.test/new",
                title="Synthetic update",
                content=article,
            )
        ),
    )
    await NewsIntakeService(session).run()
    observation = await session.scalar(
        select(ResearchDiscoveryObservation).where(
            ResearchDiscoveryObservation.query == subscription.url
        )
    )
    evidence = await session.get(RawEvidence, observation.evidence_id)
    stored = await IngestionService(session).storage.get_object(
        evidence.raw_content_ref
    )
    assert stored.decode() == article
    with pytest.raises(ValueError, match="note exceeds"):
        await IngestionService(session).ingest_text(RawEvidenceCreate(content=article))


async def test_oversize_source_leaves_receipt_and_other_lead_is_captured(
    workflow_session, monkeypatch
):
    session = workflow_session
    monkeypatch.setattr(settings, "INGESTION_MAX_UPLOAD_MB", 1)
    monkeypatch.setattr(settings, "NEWS_INTAKE_FETCH_BATCH_SIZE", 2)
    subscription = await feed(session)
    oversized = "https://example.test/oversized"
    valid = "https://example.test/valid"
    service = NewsIntakeService(session)
    await service._record(subscription, [item(url=oversized), item(url=valid, rank=2)])

    async def fetched_html(_ingestion, url, **_kwargs):
        html = (
            "<p>"
            + ("x" * (1024 * 1024 + 1) if url == oversized else "Valid filing")
            + "</p>"
        )

        return PublicDocument(url, html.encode(), "text/html", "utf-8")

    monkeypatch.setattr(IngestionService, "_fetch_public_document", fetched_html)
    result = await service._fetch_pending()
    assert result["articles_failed"] == result["articles_fetched"] == 1
    observations = {
        row.url: row
        for row in await session.scalars(
            select(ResearchDiscoveryObservation).where(
                ResearchDiscoveryObservation.query == subscription.url
            )
        )
    }
    assert observations[oversized].outcome == "fetch_size_limit"
    assert observations[oversized].evidence_id is None
    assert observations[valid].outcome == "ingested_evidence"
    assert observations[valid].evidence_id is not None


@pytest.mark.parametrize("quote_matches", [True, False])
async def test_retained_disclosure_reaches_question_while_search_is_limited(
    workflow_session, monkeypatch, quote_matches
):
    session = workflow_session
    monkeypatch.setattr(settings, "NEWS_FEED_URLS", [])
    subscription = await feed(session)
    issuer = Entity(
        name=f"Frostbeam Fabrication {uuid4().hex[:8]}", entity_type="company"
    )
    session.add(issuer)
    await session.flush()
    coverage = CoverageMap(
        subject_type="entity", subject_id=issuer.id, evidence_class_coverage_json={}
    )
    session.add(coverage)
    lead = await IngestionService(session).ingest_text(
        RawEvidenceCreate(
            title="Unverified plan to suspend a production line",
            source_item_type="news",
            url="https://example.test/earlier-report",
            content="A media report describes a possible suspension. No effective date has been confirmed.",
        ),
        process_now=False,
    )
    question = await QuestionLifecycleService(session).ensure_question(
        coverage,
        {
            "question_text": "When does the production line suspension take effect?",
            "urgency": 5,
            "mechanism": "production capacity",
            "time_horizon": "next quarter",
            "decision_relevance": "whether to revise supplier delivery expectations",
        },
        [],
        lead_evidence_id=lead.id,
    )
    search = AsyncMock(
        return_value=ResearchRunResult(False, "research_provider_limit_exceeded")
    )
    monkeypatch.setattr(ResearchService, "_search_and_ingest", search)
    monkeypatch.setattr(
        ResearchService, "_log_research_action", staticmethod(lambda **kwargs: None)
    )
    await ResearchService(session).run_targeted_question(question)
    paid_search_retry = question.next_review_at
    assert question.status == "awaiting_provider"
    assert question.research_attempt_count == 0

    article_url = "https://example.test/operating-update"
    quote = f"{issuer.name} will suspend production on the eastern line effective January 15."
    source_text = f"Issuer operating disclosure. {quote} The timing of the restart remains under review."
    updated_at = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    atom = f"""<feed xmlns="http://www.w3.org/2005/Atom"><title>Issuer filings</title>
      <id>urn:supplier-filings</id><updated>{updated_at}</updated><entry><id>urn:filing-1</id>
      <title>Operating update</title><link href="{article_url}"/><updated>{updated_at}</updated>
      </entry></feed>"""
    monkeypatch.setattr(
        "investos.services.news_intake.fetch_public_text", AsyncMock(return_value=atom)
    )
    monkeypatch.setattr(
        IngestionService,
        "fetch_url_document",
        AsyncMock(
            return_value=FetchedUrlDocument(
                url=article_url,
                canonical_url=article_url,
                title="Operating update",
                content=source_text,
            )
        ),
    )
    intake = await NewsIntakeService(session).run()
    assert intake["new_observations"] == intake["articles_fetched"] == 1
    observation = await session.scalar(
        select(ResearchDiscoveryObservation).where(
            ResearchDiscoveryObservation.query == subscription.url,
        )
    )
    assert observation.metadata_json["published_date"] is None
    assert observation.metadata_json["intake_time_basis"] == "publisher_feed_updated"
    fetched = await session.get(RawEvidence, observation.evidence_id)
    assert fetched.public_time is None

    monkeypatch.setattr(
        ResearchService,
        "search",
        AsyncMock(
            return_value=ResearchSearchResult(
                False, "research_provider_limit_exceeded", "markets", []
            )
        ),
    )
    context = compact_fresh_research_context(
        await AgentService(session)._portfolio_fresh_checks(
            {}, "What important developments should I know about?"
        )
    )
    assert context["status"] == "ok"
    assert context["searched"]
    assert context["checks"][0]["fresh_feeds"] == 1
    assert len(context["results"]) == 1
    candidate = context["results"][0]
    assert candidate["url"] == article_url
    assert candidate["published_date"] is None
    assert candidate["updated_date"] == updated_at
    assert candidate["time_basis"] == "publisher_feed_updated"
    assert candidate["observed_at"] == observation.observed_at.isoformat()
    assert candidate["evidence_id"] == str(fetched.id)

    payload = extraction_payload(issuer.name)
    payload["facts"] = [
        {**payload["facts"][0], "statement": quote, "supporting_quote": quote}
    ]
    for kind in ("claims", "events", "fundamental_metrics", "market_setup_signals"):
        payload[kind] = []
    monkeypatch.setattr(
        "investos.workers.extraction.call_llm_json", AsyncMock(return_value=payload)
    )
    for service in (
        "CoverageWorker.audit_subject_coverage",
        "SourceLearningService.learn_from_source",
        "OperatingLoopService.refresh_subject",
        "WatcherService.evaluate_new_evidence",
    ):
        monkeypatch.setattr(
            f"investos.workers.extraction.{service}", AsyncMock(return_value={})
        )
    await ExtractionWorker(session).process_evidence(fetched.id)
    assert fetched.is_processed

    assessment = AsyncMock(
        side_effect=[
            TimeoutError("Inference is temporarily unavailable"),
            {
                "answered": True,
                "outcome": "answered",
                "summary": "The filing was submitted January 14 and the suspension takes effect January 15.",
                "remaining_uncertainty": None,
                "supporting_quote": (
                    quote if quote_matches else "The filing says February 12."
                ),
            },
        ]
    )
    monkeypatch.setattr("investos.services.research.call_llm_json", assessment)

    @asynccontextmanager
    async def same_session():
        yield session

    monkeypatch.setattr(
        "investos.services.automation.async_session_maker", same_session
    )
    coordinator = AutomationCoordinator()
    coordinator.telemetry["question_resolution"] = JobTelemetry(
        "question_resolution", 3600
    )
    monkeypatch.setattr(coordinator, "_log_job_action", lambda **kwargs: None)
    await coordinator._run_question_resolution()
    assert coordinator.telemetry["question_resolution"].last_status == "deferred"
    assert question.status == "awaiting_provider"
    await coordinator._run_question_resolution()
    await coordinator._run_question_resolution()
    assert assessment.await_count == 2
    search.assert_awaited_once()
    audit = await session.scalar(
        select(QuestionHistory).where(
            QuestionHistory.question_id == question.id,
            QuestionHistory.event_type == "retained_source_assessed",
        )
    )
    assert audit.detail_json["evidence_id"] == str(fetched.id)
    solution = await session.scalar(
        select(Resolution).where(Resolution.unresolved_question_id == question.id)
    )
    if quote_matches:
        assert question.status == "answered"
        assert solution.resolving_evidence_ids == [fetched.id]
        assert solution.summary == quote
        assert audit.detail_json["assessment_summary"] == quote
        assert audit.detail_json["source_passage"]["exact"] == quote
    else:
        assert question.status == "awaiting_provider"
        assert question.next_review_at == paid_search_retry
        assert solution is None
        assert audit.detail_json["outcome"] == "unbound_source_answer"
    assert question.research_attempt_count == question.resolution_attempt_count == 0


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


async def test_feed_updates_recover_legacy_unqualified_observations(workflow_session):
    session = workflow_session
    subscription = await feed(session)
    service = NewsIntakeService(session)
    observed_at = datetime.now(UTC)
    update_only = {
        **item(),
        "published_date": None,
        "updated_date": (observed_at - timedelta(minutes=1)).isoformat(),
    }
    assert (
        await service._record(subscription, [update_only], observed_at=observed_at) == 1
    )
    observation = await session.scalar(
        select(ResearchDiscoveryObservation).where(
            ResearchDiscoveryObservation.query == subscription.url,
        )
    )
    observation.outcome = "publication_unverified"
    observation.metadata_json = {**observation.metadata_json, "intake_time_basis": None}
    await session.flush()
    assert (
        await service._record(
            subscription, [update_only], observed_at=observed_at + timedelta(minutes=1)
        )
        == 0
    )
    await session.refresh(observation)
    assert observation.outcome == "observed"
    assert observation.observed_at == observed_at
    assert observation.metadata_json["published_date"] is None
    assert observation.metadata_json["intake_time_basis"] == "publisher_feed_updated"


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
    status = await service.status()
    assert status["pending_count"] == 0
    assert status["oldest_pending_at"] is None
    assert status["total"] == 1
    fetcher.assert_not_awaited()


async def test_autonomous_feed_captures_captions_and_retains_unreadable_video(
    workflow_session, monkeypatch
):
    monkeypatch.setattr(settings, "NEWS_FEED_URLS", [])
    monkeypatch.setattr(settings, "NEWS_INTAKE_FETCH_BATCH_SIZE", 2)
    subscription = await feed(workflow_session)
    available = "https://youtu.be/CaseAudio01"
    missing = "https://www.youtube.com/live/CaseAudio02"
    monkeypatch.setattr(
        NewsIntakeService,
        "_read_feed",
        AsyncMock(return_value=[item(url=available), item(url=missing, rank=2)]),
    )

    async def captions(video_id):
        if video_id == "CaseAudio02":
            raise TranscriptsDisabled(video_id)
        return [SimpleNamespace(text="The supplier has paused shipments.")]

    monkeypatch.setattr(YouTubeService, "_read_captions", staticmethod(captions))
    no_html = AsyncMock(
        side_effect=AssertionError("Do not read the watch page as a transcript")
    )
    monkeypatch.setattr(IngestionService, "_fetch_public_document", no_html)
    service = NewsIntakeService(workflow_session)
    run = await service.run()
    assert run["articles_fetched"] == run["articles_failed"] == 1
    rows = {
        row.url: row
        for row in await workflow_session.scalars(
            select(ResearchDiscoveryObservation).where(
                ResearchDiscoveryObservation.query == subscription.url
            )
        )
    }
    evidence = await workflow_session.get(RawEvidence, rows[available].evidence_id)
    assert evidence.source_item_type == "video_transcript"
    assert evidence.metadata_json["representation"] == "caption_transcript"
    assert evidence.metadata_json["canonical_source_url"] == (
        "https://www.youtube.com/watch?v=CaseAudio01"
    )
    assert not evidence.is_processed
    detail = await SourceService(workflow_session).get_evidence_detail(evidence.id)
    assert detail["origin_kind"] == "automation"
    assert detail["origin_label"] == "Publisher feed intake"
    assert rows[missing].outcome == "fetch_content_unavailable"
    assert rows[missing].evidence_id is None
    assert "No public YouTube captions" in rows[missing].error
    assert (await service._fetch_pending())["articles_failed"] == 0
    no_html.assert_not_awaited()


async def test_expired_capture_is_retained_outside_the_queue(workflow_session):
    service = NewsIntakeService(workflow_session)
    subscription = await feed(workflow_session)
    expired = datetime.now(UTC) - timedelta(
        hours=settings.NEWS_INTAKE_LOOKBACK_HOURS + 1
    )
    entry = item()
    entry["published_date"] = expired.isoformat()
    await service._record(subscription, [entry], observed_at=expired)
    await service._record(subscription, [item(url="https://example.test/recent")])
    status = await service.status()
    assert status["pending_count"] == 1
    assert status["oldest_pending_at"] > expired
    assert status["total"] == 2


async def test_stale_feed_checkpoint_is_not_current_coverage(workflow_session):
    session = workflow_session
    subscription = await feed(session)
    service = NewsIntakeService(session)
    await service._record(subscription, [item()])
    subscription.last_success_at = datetime.now(UTC) - timedelta(hours=2)
    await session.flush()
    context = await service.recent_context()
    assert context["results"] == []
    assert context["checks"][0]["status"] == "no_fresh_feed_checks"
    assert context["checks"][0]["fresh_feeds"] == 0
    assert context["checks"][0]["enabled_feeds"] == 1
    assert (await service.status())["pending_count"] >= 1


async def test_feed_backfill_stays_in_history_without_displacing_current_leads(
    workflow_session,
):
    service = NewsIntakeService(workflow_session)
    subscription = await feed(workflow_session)
    now = datetime.now(UTC)
    fresh = item(url="https://example.test/current")
    await service._record(subscription, [fresh], observed_at=now - timedelta(minutes=1))
    archive_date = (now - timedelta(days=90)).isoformat()
    await service._record(
        subscription,
        [
            {
                **item(url=f"https://example.test/archive/{i}"),
                "published_date": archive_date,
            }
            for i in range(25)
        ],
        observed_at=now,
    )
    current = await service.status(current_only=True)
    assert current["total"] == 26
    assert [row["url"] for row in current["items"]] == [fresh["url"]]
    assert len((await service.status())["items"]) == 20
    general = SourceService(workflow_session)
    assert len(await general.list_discovery_observations()) == 26
    assert (
        await general.list_discovery_observations(exclude_provider="publisher_feed")
        == []
    )


async def test_official_feeds_are_durable_defaults(workflow_session, monkeypatch):
    service = NewsIntakeService(workflow_session)
    monkeypatch.setattr(
        settings, "NEWS_FEED_URLS", Settings.model_construct().NEWS_FEED_URLS
    )
    await service.ensure_defaults()
    feeds = list(await workflow_session.scalars(select(PublisherFeed)))
    assert {row.url for row in feeds} == set(settings.NEWS_FEED_URLS)
    official = [row for row in feeds if row.url.startswith("https://www.sec.gov/")]
    assert len(official) == 4
    assert any("type=8-K" in row.url for row in official)
    assert any("type=6-K" in row.url for row in official)
    assert any(row.url.startswith("https://www.federalreserve.gov/") for row in feeds)
    assert any(row.url.startswith("https://www.youtube.com/feeds/") for row in feeds)
    assert sum(row.url.startswith("https://www.bls.gov/feed/") for row in feeds) == 2
    official[0].enabled = False
    await workflow_session.flush()
    await service.ensure_defaults()
    assert len(list(await workflow_session.scalars(select(PublisherFeed)))) == len(
        feeds
    )
    assert official[0].enabled is False


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


@pytest.mark.parametrize("http_status", [401, 503])
async def test_fetch_failure_distinguishes_access_requirements_from_retryable_leads(
    workflow_session, monkeypatch, http_status
):
    session = workflow_session
    subscription = await feed(session)
    service = NewsIntakeService(session)
    entry = item()
    await service._record(subscription, [entry])
    fetcher = AsyncMock(
        side_effect=UrlFetchNetworkError(
            "Synthetic publisher failure", status_code=http_status
        )
    )
    monkeypatch.setattr(IngestionService, "fetch_url_document", fetcher)
    result = await service._fetch_pending()
    row = await session.scalar(
        select(ResearchDiscoveryObservation).where(
            ResearchDiscoveryObservation.query == subscription.url
        )
    )
    assert result["articles_failed"] == 1
    assert row.evidence_id is None
    assert row.metadata_json["fetch_attempts"] == 1
    if http_status == 401:
        assert row.outcome == "fetch_access_required"
        assert "next_fetch_at" not in row.metadata_json
    else:
        assert row.outcome == "fetch_failed"
        assert row.metadata_json["next_fetch_at"] > datetime.now(UTC).isoformat()
    assert await service._record(subscription, [entry]) == 0
    assert (await service._fetch_pending())["articles_failed"] == 0
    fetcher.assert_awaited_once()


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


async def test_new_source_gets_priority_without_dropping_revisions(
    workflow_session, monkeypatch
):
    service = NewsIntakeService(workflow_session)
    subscription = await feed(workflow_session)
    fetcher = AsyncMock(side_effect=document)
    monkeypatch.setattr(IngestionService, "fetch_url_document", fetcher)
    monkeypatch.setattr(settings, "NEWS_INTAKE_FETCH_BATCH_SIZE", 2)
    live_page = item(url="https://example.test/live")
    await service._record(subscription, [live_page])
    await service._fetch_pending()
    now = datetime.now(UTC)
    await service._record(
        subscription,
        [item(url="https://example.test/older", rank=90)],
        observed_at=now - timedelta(minutes=15),
    )
    await service._record(
        subscription,
        [
            {**live_page, "title": "New live page update"},
            item(url="https://example.test/first-report", rank=18),
        ],
    )
    fetcher.reset_mock()
    await service._fetch_pending()
    assert {call.args[0] for call in fetcher.await_args_list} == {
        "https://example.test/older",
        "https://example.test/first-report",
    }
    await service._fetch_pending()
    assert fetcher.await_args.args[0] == live_page["url"]


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
