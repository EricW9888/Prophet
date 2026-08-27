from __future__ import annotations

from typing import Any, Awaitable, Callable
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from investos.config import settings
from investos.models.evidence import RawEvidence, ResearchDiscoveryObservation
from investos.models.source import Source
from investos.services.youtube_channel import YouTubeChannelEnumerator

VideoIngestor = Callable[..., Awaitable[dict[str, Any]]]


class YouTubeChannelReviewService:
    """Review explicitly tracked channels without treating metadata as evidence."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        channel_enumerator: YouTubeChannelEnumerator,
        ingest_video: VideoIngestor,
    ) -> None:
        self.session = session
        self.channel_enumerator = channel_enumerator
        self.ingest_video = ingest_video

    async def review(
        self,
        *,
        source_limit: int | None = None,
        video_limit: int | None = None,
        auto_ingest: bool | None = None,
    ) -> dict[str, Any]:
        readiness = self.channel_enumerator.readiness()
        if not readiness["available"]:
            return {
                "status": "waiting_for_config",
                "reason": "missing: " + ", ".join(readiness["missing"]),
                "sources_reviewed": 0,
                "uploads_observed": 0,
                "uploads_ingested": 0,
            }
        resolved_source_limit = max(
            1,
            min(
                int(source_limit or settings.YOUTUBE_CHANNEL_REVIEW_SOURCE_LIMIT),
                100,
            ),
        )
        resolved_video_limit = max(
            1,
            min(
                int(video_limit or settings.YOUTUBE_CHANNEL_REVIEW_VIDEO_LIMIT),
                50,
            ),
        )
        should_ingest = (
            settings.YOUTUBE_CHANNEL_AUTO_INGEST_ENABLED
            if auto_ingest is None
            else bool(auto_ingest)
        )
        sources = (
            (
                await self.session.execute(
                    select(Source)
                    .where(Source.source_type == "youtube")
                    .where(Source.trust_origin == "operator")
                    .where(Source.is_trusted.is_(True))
                    .where(Source.url.is_not(None))
                    .order_by(Source.updated_at.desc())
                    .limit(resolved_source_limit)
                )
            )
            .scalars()
            .all()
        )
        totals = {
            "status": "ok",
            "sources_reviewed": 0,
            "uploads_observed": 0,
            "uploads_ingested": 0,
            "uploads_already_known": 0,
            "uploads_failed": 0,
            "source_errors": 0,
            "details": [],
        }
        for source in sources:
            try:
                result = await self.channel_enumerator.list_recent(
                    channel_url=str(source.url),
                    limit=resolved_video_limit,
                )
                detail = await self._review_channel_result(
                    source=source,
                    result=result,
                    auto_ingest=should_ingest,
                )
            except Exception as exc:
                detail = {
                    "source_id": str(source.id),
                    "source_name": source.name,
                    "status": "error",
                    "error": str(exc),
                    "observed": 0,
                    "ingested": 0,
                    "already_known": 0,
                    "failed": 0,
                }
                totals["source_errors"] += 1
            totals["sources_reviewed"] += 1
            totals["uploads_observed"] += detail["observed"]
            totals["uploads_ingested"] += detail["ingested"]
            totals["uploads_already_known"] += detail["already_known"]
            totals["uploads_failed"] += detail["failed"]
            totals["details"].append(detail)
        await self.session.commit()
        return totals

    async def _review_channel_result(
        self,
        *,
        source: Source,
        result: dict[str, Any],
        auto_ingest: bool,
    ) -> dict[str, Any]:
        videos = list(result.get("videos") or [])
        urls = [str(video.get("url") or "").strip() for video in videos]
        urls = [url for url in urls if url]
        observations_by_url: dict[str, ResearchDiscoveryObservation] = {}
        known_evidence_video_ids: set[str] = set()
        if urls:
            observation_rows = (
                (
                    await self.session.execute(
                        select(ResearchDiscoveryObservation)
                        .where(
                            ResearchDiscoveryObservation.provider == "youtube_channel"
                        )
                        .where(
                            ResearchDiscoveryObservation.query
                            == f"youtube_channel:{source.id}"
                        )
                        .where(ResearchDiscoveryObservation.url.in_(urls))
                    )
                )
                .scalars()
                .all()
            )
            observations_by_url = {row.url: row for row in observation_rows}
        video_ids = [str(video.get("video_id") or "") for video in videos]
        video_ids = [video_id for video_id in video_ids if video_id]
        if video_ids:
            evidence_rows = (
                (
                    await self.session.execute(
                        select(RawEvidence)
                        .where(RawEvidence.source_id == source.id)
                        .where(
                            RawEvidence.metadata_json["video_id"].astext.in_(video_ids)
                        )
                    )
                )
                .scalars()
                .all()
            )
            known_evidence_video_ids = {
                str(metadata.get("video_id"))
                for evidence in evidence_rows
                if isinstance((metadata := evidence.metadata_json), dict)
                and metadata.get("video_id")
            }

        detail = {
            "source_id": str(source.id),
            "source_name": source.name,
            "status": "ok",
            "observed": 0,
            "ingested": 0,
            "already_known": 0,
            "failed": 0,
        }
        ingest_budget = max(
            0, int(settings.YOUTUBE_CHANNEL_AUTO_INGEST_LIMIT_PER_SOURCE)
        )
        for rank, video in enumerate(videos, start=1):
            video_id = str(video.get("video_id") or "").strip()
            video_url = str(video.get("url") or "").strip()
            if not video_id or not video_url:
                continue
            existing_observation = observations_by_url.get(video_url)
            if video_id in known_evidence_video_ids or (
                existing_observation is not None
                and existing_observation.outcome == "ingested"
            ):
                detail["already_known"] += 1
                continue
            observation = existing_observation
            if observation is None:
                observation = self._new_observation(
                    source=source,
                    result=result,
                    video=video,
                    rank=rank,
                )
                self.session.add(observation)
                await self.session.flush()
                detail["observed"] += 1
            if not auto_ingest or ingest_budget <= 0:
                detail["already_known"] += int(existing_observation is not None)
                continue
            ingest_budget -= 1
            observation.outcome = "observed"
            observation.error = None
            try:
                ingest_result = await self.ingest_video(
                    video_url,
                    title=str(video.get("title") or "").strip() or None,
                    source_id=source.id,
                )
            except Exception as exc:
                observation.outcome = "fetch_failed"
                observation.error = str(exc)
                detail["failed"] += 1
                continue
            if ingest_result.get("ok"):
                observation.outcome = "ingested"
                evidence_id = ingest_result.get("evidence_id")
                observation.evidence_id = (
                    UUID(str(evidence_id)) if evidence_id else None
                )
                observation.metadata_json = {
                    **(observation.metadata_json or {}),
                    "evidence_status": "ingested",
                    "ingest_mode": ingest_result.get("ingest_mode"),
                }
                detail["ingested"] += 1
            else:
                observation.outcome = "fetch_failed"
                observation.error = str(ingest_result.get("error") or "ingest failed")
                detail["failed"] += 1
        return detail

    @staticmethod
    def _new_observation(
        *,
        source: Source,
        result: dict[str, Any],
        video: dict[str, Any],
        rank: int,
    ) -> ResearchDiscoveryObservation:
        video_url = str(video.get("url") or "").strip()
        return ResearchDiscoveryObservation(
            provider="youtube_channel",
            query=f"youtube_channel:{source.id}",
            effective_query=str(result.get("channel_url") or source.url),
            search_title=f"Tracked YouTube channel: {source.name}",
            result_rank=rank,
            result_title=str(video.get("title") or video_url),
            url=video_url,
            snippet=None,
            content_kind="channel_upload_metadata",
            outcome="observed",
            subject_type="source",
            subject_id=str(source.id),
            subject_name=source.name,
            metadata_json={
                "video_id": str(video.get("video_id") or ""),
                "published_at": video.get("published_at"),
                "duration_seconds": video.get("duration_seconds"),
                "view_count": video.get("view_count"),
                "channel_id": result.get("channel_id"),
                "channel_name": result.get("channel_name"),
                "evidence_status": "not_yet_fetched",
            },
        )
