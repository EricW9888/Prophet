"""Cheap, model-independent discovery; fetched articles use normal evidence gates."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, uuid4, uuid5

import feedparser
from sqlalchemy import String, cast, func, or_, select
from sqlalchemy import text as sql_text
from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert

from investos.config import settings
from investos.core.dates import parse_publication_datetime
from investos.core.research_providers import normalize_search_results
from investos.core.url_security import fetch_public_text
from investos.models.evidence import RawEvidence, ResearchDiscoveryObservation
from investos.models.news import PublisherFeed
from investos.schemas.evidence import RawEvidenceCreate
from investos.services.ingestion import IngestionService, _html_to_text
from investos.services.source_learning import SourceLearningService

FEED_PROVIDER = "publisher_feed"


def parse_feed(text: str) -> list[dict]:
    # Passing bytes, not a URL, keeps all networking inside the SSRF/size guard.
    parsed = feedparser.parse(
        io.BytesIO(text.encode("utf-8")),
        response_headers={"content-type": "application/xml; charset=utf-8"},
    )
    if not parsed.get("version") or parsed.get("bozo"):
        raise ValueError("Publisher did not return a well-formed supported feed.")
    rows = []
    for rank, entry in enumerate(parsed.entries[:200], 1):
        title = _html_to_text(str(entry.get("title") or "")).strip()
        summary = _html_to_text(str(entry.get("summary") or ""))[:4000]
        rows.append(
            {
                "title": title,
                "url": entry.get("link"),
                "content": summary,
                "published_date": entry.get("published"),
                "updated_date": dict.get(entry, "updated"),
                "feed_rank": rank,
            }
        )
    normalized = normalize_search_results(FEED_PROVIDER, rows)
    by_url = {str(row["url"]).split("#", 1)[0]: row for row in rows}
    for row in normalized:
        original = by_url.get(row["url"], {})
        row["feed_rank"] = original.get("feed_rank", 200)
        row["updated_date"] = original.get("updated_date")
    return normalized


class NewsIntakeService:
    def __init__(self, session):
        self.session = session

    async def ensure_defaults(self):
        await self.session.execute(
            sql_text(
                "SELECT pg_advisory_xact_lock(hashtext('prophet:publisher-feeds'))"
            )
        )
        existing = set(await self.session.scalars(select(PublisherFeed.url)))
        for url in settings.NEWS_FEED_URLS[:20]:
            if url in existing or len(existing) >= 20:
                continue
            await self.session.execute(
                insert(PublisherFeed)
                .values(
                    id=uuid5(NAMESPACE_URL, "prophet:publisher-feed:" + url),
                    url=url,
                    name=urlsplit(url).hostname or "Publisher feed",
                    enabled=True,
                    last_status="not_checked",
                    entry_count=0,
                    new_count=0,
                    created_at=datetime.now(UTC),
                )
                .on_conflict_do_nothing(index_elements=[PublisherFeed.url])
            )
            existing.add(url)
        await self.session.commit()

    async def run(self):
        await self.ensure_defaults()
        now = datetime.now(UTC)
        feeds = list(
            await self.session.scalars(
                select(PublisherFeed)
                .where(
                    PublisherFeed.enabled.is_(True),
                    or_(
                        PublisherFeed.next_poll_at.is_(None),
                        PublisherFeed.next_poll_at <= now,
                    ),
                    or_(
                        PublisherFeed.lease_until.is_(None),
                        PublisherFeed.lease_until <= now,
                    ),
                )
                .order_by(
                    PublisherFeed.next_poll_at.asc().nullsfirst(), PublisherFeed.id
                )
                .limit(20)
                .with_for_update(skip_locked=True)
            )
        )
        for feed in feeds:
            feed.lease_until = now + timedelta(minutes=3)
        await self.session.commit()
        responses = await asyncio.gather(
            *(self._read_feed(feed.url) for feed in feeds), return_exceptions=True
        )
        observed_at = datetime.now(UTC)
        observed = failures = 0
        for feed, response in zip(feeds, responses):
            feed.last_checked_at = datetime.now(UTC)
            feed.next_poll_at = feed.last_checked_at + timedelta(
                seconds=settings.NEWS_INTAKE_POLL_SECONDS
            )
            feed.lease_until = None
            if isinstance(response, Exception):
                failures += 1
                feed.last_status = "error"
                feed.last_error = f"{type(response).__name__}: {str(response)[:300]}"
                feed.new_count = 0
            else:
                feed.new_count = await self._record(
                    feed, response, observed_at=observed_at
                )
                observed += feed.new_count
                feed.entry_count = len(response)
                feed.last_status = "ok"
                feed.last_error = None
                feed.last_success_at = feed.last_checked_at
            await self.session.commit()
        fetched = await self._fetch_pending()
        return {
            "status": "warning" if failures or fetched["articles_failed"] else "ok",
            "feeds_checked": len(feeds),
            "feeds_failed": failures,
            "new_observations": observed,
            **fetched,
        }

    @staticmethod
    async def _read_feed(url):
        text = await fetch_public_text(url, timeout_seconds=12, max_bytes=524288)
        return await asyncio.to_thread(parse_feed, text)

    async def _record(self, feed, rows, *, observed_at=None):
        payloads = []
        now = observed_at or datetime.now(UTC)
        request_id = str(uuid4())
        for item in rows:
            published = parse_publication_datetime(item.get("published_date"))
            outcome = "observed"
            if published is None:
                outcome = "publication_unverified"
            elif (
                not now - timedelta(hours=settings.NEWS_INTAKE_LOOKBACK_HOURS)
                <= published
                <= now
            ):
                outcome = "publication_outside_window"
            version = hashlib.sha256(
                json.dumps(
                    {
                        k: item.get(k)
                        for k in (
                            "title",
                            "url",
                            "content",
                            "published_date",
                            "updated_date",
                        )
                    },
                    sort_keys=True,
                ).encode()
            ).hexdigest()
            observation_id = uuid5(feed.id, version)
            payloads.append(
                dict(
                    id=observation_id,
                    provider=FEED_PROVIDER,
                    request_id=request_id,
                    query=feed.url,
                    effective_query=feed.url,
                    search_title=f"Publisher feed: {feed.name}",
                    result_rank=item["feed_rank"],
                    result_title=item["title"],
                    url=item["url"],
                    snippet=item["content"],
                    content_kind="feed_summary",
                    outcome=outcome,
                    subject_type="portfolio",
                    subject_name="Portfolio",
                    observed_at=now,
                    metadata_json={
                        "feed_id": str(feed.id),
                        "content_hash": version,
                        "published_date": item.get("published_date"),
                        "updated_date": item.get("updated_date"),
                        "publication_basis": "publisher_feed_metadata",
                        "fetch_attempts": 0,
                    },
                )
            )
        if not payloads:
            return 0
        created = (
            await self.session.execute(
                insert(ResearchDiscoveryObservation)
                .values(payloads)
                .on_conflict_do_nothing(
                    index_elements=[ResearchDiscoveryObservation.id]
                )
                .returning(
                    ResearchDiscoveryObservation.id, ResearchDiscoveryObservation.url
                )
            )
        ).all()
        if created:
            # A changed headline is new availability, not a backdated claim.
            # Both insertion and supersession are bounded batch operations.
            await self.session.execute(
                update(ResearchDiscoveryObservation)
                .where(
                    ResearchDiscoveryObservation.provider == FEED_PROVIDER,
                    ResearchDiscoveryObservation.query == feed.url,
                    ResearchDiscoveryObservation.url.in_([row.url for row in created]),
                    ResearchDiscoveryObservation.id.not_in([row.id for row in created]),
                    ResearchDiscoveryObservation.outcome.in_(
                        ["observed", "fetch_failed"]
                    ),
                )
                .values(outcome="superseded_by_feed_update")
            )
        return len(created)

    async def _fetch_pending(self):
        now = datetime.now(UTC)
        stamp = now.isoformat()
        await self.session.execute(
            update(ResearchDiscoveryObservation)
            .where(
                ResearchDiscoveryObservation.provider == FEED_PROVIDER,
                ResearchDiscoveryObservation.outcome == "fetching",
                ResearchDiscoveryObservation.metadata_json["fetch_lease_until"].astext
                < stamp,
            )
            .values(outcome="observed")
        )
        eligible = [
            ResearchDiscoveryObservation.provider == FEED_PROVIDER,
            ResearchDiscoveryObservation.metadata_json["feed_id"].astext.in_(
                select(cast(PublisherFeed.id, String)).where(
                    PublisherFeed.enabled.is_(True)
                )
            ),
            ResearchDiscoveryObservation.outcome.in_(["observed", "fetch_failed"]),
            ResearchDiscoveryObservation.observed_at
            >= now - timedelta(hours=settings.NEWS_INTAKE_LOOKBACK_HOURS),
            or_(
                ResearchDiscoveryObservation.metadata_json["next_fetch_at"].astext.is_(
                    None
                ),
                ResearchDiscoveryObservation.metadata_json["next_fetch_at"].astext
                <= stamp,
            ),
        ]
        batch = settings.NEWS_INTAKE_FETCH_BATCH_SIZE
        base = select(ResearchDiscoveryObservation).where(*eligible)
        # Reserve one slot for the oldest retained lead. Editorial priority
        # cannot indefinitely starve the rest of the backlog.
        priority = (
            list(
                await self.session.scalars(
                    base.order_by(
                        ResearchDiscoveryObservation.result_rank,
                        ResearchDiscoveryObservation.observed_at.desc(),
                        ResearchDiscoveryObservation.id,
                    )
                    .limit(max(0, batch - 1))
                    .with_for_update(skip_locked=True)
                )
            )
            if batch > 1
            else []
        )
        old = list(
            await self.session.scalars(
                base.where(
                    ResearchDiscoveryObservation.id.not_in([o.id for o in priority])
                )
                .order_by(
                    ResearchDiscoveryObservation.observed_at,
                    ResearchDiscoveryObservation.id,
                )
                .limit(batch - len(priority))
                .with_for_update(skip_locked=True)
            )
        )
        selected = priority + old
        tokens = {row.id: str(uuid4()) for row in selected}
        for row in selected:
            row.outcome = "fetching"
            row.metadata_json = {
                **(row.metadata_json or {}),
                "fetch_token": tokens[row.id],
                "fetch_lease_until": (now + timedelta(minutes=3)).isoformat(),
            }
        await self.session.commit()
        ingestion = IngestionService(self.session)
        # Ingestion commits the raw receipt before linking it to discovery. A
        # crash in that gap must reconcile the existing receipt, not ingest twice.
        existing_rows = (
            list(
                await self.session.scalars(
                    select(RawEvidence).where(
                        RawEvidence.metadata_json[
                            "discovery_observation_id"
                        ].astext.in_([str(row.id) for row in selected])
                    )
                )
            )
            if selected
            else []
        )
        existing_by_observation = {
            (row.metadata_json or {}).get("discovery_observation_id"): row
            for row in existing_rows
        }
        pending_network = [
            row for row in selected if str(row.id) not in existing_by_observation
        ]
        network_results = await asyncio.gather(
            *(ingestion.fetch_url_document(row.url) for row in pending_network),
            return_exceptions=True,
        )
        results_by_id = dict(zip((row.id for row in pending_network), network_results))
        fetched = failed = recovered = 0
        for selected_row in selected:
            row = await self.session.scalar(
                select(ResearchDiscoveryObservation)
                .where(
                    ResearchDiscoveryObservation.id == selected_row.id,
                    ResearchDiscoveryObservation.outcome == "fetching",
                    ResearchDiscoveryObservation.metadata_json["fetch_token"].astext
                    == tokens[selected_row.id],
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if row is None:
                continue
            existing = existing_by_observation.get(str(row.id))
            metadata = dict(row.metadata_json or {})
            attempts = int(metadata.get("fetch_attempts") or 0) + 1
            metadata["fetch_attempts"] = attempts
            metadata.pop("fetch_lease_until", None)
            metadata.pop("fetch_token", None)
            result = results_by_id.get(row.id)
            if existing is not None:
                row.evidence_id = existing.id
                row.outcome = "ingested_evidence"
                row.error = None
                metadata["recovered_receipt"] = True
                recovered += 1
            elif isinstance(result, Exception):
                failed += 1
                row.outcome = (
                    "fetch_failed" if attempts < 3 else "fetch_retry_exhausted"
                )
                row.error = f"{type(result).__name__}: {str(result)[:300]}"
                metadata["next_fetch_at"] = (
                    datetime.now(UTC) + timedelta(minutes=15 * attempts)
                ).isoformat()
            else:
                source = await SourceLearningService(
                    self.session
                ).get_or_create_source_for_url(
                    url=row.url,
                    title=row.result_title,
                    preferred_type="news",
                    description="Auto-discovered through a publisher feed; not an operator trust decision.",
                )
                evidence = await ingestion.ingest_text(
                    RawEvidenceCreate(
                        title=row.result_title,
                        source_id=source.source.id,
                        source_item_type="news",
                        url=row.url,
                        public_time=result.public_time,
                        content=result.content[:20000],
                        metadata_json={
                            "trigger": "publisher_feed_intake",
                            "discovery_observation_id": str(row.id),
                            "feed_id": metadata["feed_id"],
                            "feed_published_date": metadata.get("published_date"),
                            "feed_updated_date": metadata.get("updated_date"),
                            "discovered_at": row.observed_at.isoformat(),
                            "fetched_at": datetime.now(UTC).isoformat(),
                            "content_origin": "direct_page",
                            "canonical_source_url": result.canonical_url,
                            "publication_time_source": (
                                "document_metadata" if result.public_time else None
                            ),
                        },
                    ),
                    process_now=False,
                )
                row.evidence_id = evidence.id
                row.outcome = "ingested_evidence"
                row.error = None
                fetched += 1
            row.metadata_json = metadata
            await self.session.commit()
        return {
            "articles_fetched": fetched,
            "articles_failed": failed,
            "articles_recovered": recovered,
        }

    async def recent_context(self, *, window_hours=24):
        """Expose only recently checked publisher discoveries as provisional context."""
        now = datetime.now(UTC)
        fresh_since = now - timedelta(seconds=settings.NEWS_INTAKE_POLL_SECONDS * 2)
        feeds = list(
            await self.session.scalars(
                select(PublisherFeed).where(PublisherFeed.enabled.is_(True))
            )
        )
        fresh = [
            f
            for f in feeds
            if f.last_status == "ok"
            and f.last_success_at
            and f.last_success_at >= fresh_since
        ]
        if not fresh:
            return {"results": [], "checks": []}
        rows = list(
            await self.session.scalars(
                select(ResearchDiscoveryObservation)
                .where(
                    ResearchDiscoveryObservation.provider == FEED_PROVIDER,
                    ResearchDiscoveryObservation.metadata_json["feed_id"].astext.in_(
                        [str(f.id) for f in fresh]
                    ),
                    ResearchDiscoveryObservation.observed_at
                    >= now - timedelta(hours=window_hours),
                    ResearchDiscoveryObservation.outcome.not_in(
                        [
                            "superseded_by_feed_update",
                            "publication_unverified",
                            "publication_outside_window",
                        ]
                    ),
                )
                .order_by(
                    ResearchDiscoveryObservation.observed_at.desc(),
                    ResearchDiscoveryObservation.result_rank,
                    ResearchDiscoveryObservation.id,
                )
                .limit(200)
            )
        )
        by_url = {}
        for row in rows:
            published = parse_publication_datetime(
                (row.metadata_json or {}).get("published_date")
            )
            if (
                published is None
                or not now - timedelta(hours=window_hours) <= published <= now
            ):
                continue
            by_url.setdefault(
                row.url,
                {
                    "title": row.result_title,
                    "url": row.url,
                    "content": row.snippet,
                    "published_date": (row.metadata_json or {}).get("published_date"),
                    "observed_at": row.observed_at.isoformat(),
                    "evidence_id": str(row.evidence_id) if row.evidence_id else None,
                    "content_kind": "feed_summary",
                    "lane": "Publisher news",
                    "score": None,
                    "feed_rank": row.result_rank,
                },
            )
        results = sorted(by_url.values(), key=lambda r: r["feed_rank"])[:12]
        return {
            "results": results,
            "checks": [
                {
                    "lane": "Publisher news",
                    "query": "; ".join(f.name for f in fresh),
                    "status": "provisional" if results else "no_recent_results",
                    "result_count": len(results),
                    "window_hours": window_hours,
                    "coverage": "bounded_publisher_feeds_not_exhaustive",
                    "fresh_feeds": len(fresh),
                    "enabled_feeds": len(feeds),
                }
            ],
        }

    async def status(self, *, offset=0):
        feeds = list(
            await self.session.scalars(
                select(PublisherFeed).order_by(PublisherFeed.name, PublisherFeed.id)
            )
        )
        pending = ResearchDiscoveryObservation.outcome.in_(
            ["observed", "fetching", "fetch_failed"]
        )
        count, oldest = (
            await self.session.execute(
                select(
                    func.count(), func.min(ResearchDiscoveryObservation.observed_at)
                ).where(ResearchDiscoveryObservation.provider == FEED_PROVIDER, pending)
            )
        ).one()
        rows = list(
            await self.session.scalars(
                select(ResearchDiscoveryObservation)
                .where(ResearchDiscoveryObservation.provider == FEED_PROVIDER)
                .order_by(
                    ResearchDiscoveryObservation.observed_at.desc(),
                    ResearchDiscoveryObservation.result_rank,
                    ResearchDiscoveryObservation.id.desc(),
                )
                .offset(offset)
                .limit(20)
            )
        )
        total = await self.session.scalar(
            select(func.count())
            .select_from(ResearchDiscoveryObservation)
            .where(ResearchDiscoveryObservation.provider == FEED_PROVIDER)
        )
        return {
            "enabled": settings.NEWS_INTAKE_ENABLED,
            "pending_count": count,
            "oldest_pending_at": oldest,
            "poll_seconds": settings.NEWS_INTAKE_POLL_SECONDS,
            "fetch_batch_size": settings.NEWS_INTAKE_FETCH_BATCH_SIZE,
            "feeds": [
                {
                    "id": f.id,
                    "name": f.name,
                    "url": f.url,
                    "enabled": f.enabled,
                    "last_checked_at": f.last_checked_at,
                    "last_success_at": f.last_success_at,
                    "status": f.last_status,
                    "error": f.last_error,
                    "entry_count": f.entry_count,
                    "new_count": f.new_count,
                    "next_poll_at": f.next_poll_at,
                }
                for f in feeds
            ],
            "total": total,
            "offset": offset,
            "items": [
                {
                    "id": r.id,
                    "title": r.result_title,
                    "url": r.url,
                    "published_date": (r.metadata_json or {}).get("published_date"),
                    "observed_at": r.observed_at,
                    "outcome": r.outcome,
                    "error": r.error,
                    "evidence_id": r.evidence_id,
                }
                for r in rows
            ],
        }
