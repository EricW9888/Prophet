from __future__ import annotations

import asyncio
import importlib.util
import re
from datetime import UTC, datetime
from typing import Any, Awaitable, Callable
from urllib.parse import parse_qs, urlparse
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from youtube_transcript_api import (
    NoTranscriptFound,
    TranscriptsDisabled,
    VideoUnavailable,
    YouTubeTranscriptApi,
)

from investos.config import settings
from investos.models.evidence import RawEvidence
from investos.models.source import Source
from investos.schemas.evidence import RawEvidenceCreate
from investos.services.ingestion import IngestionService
from investos.services.media_investigation import MediaInvestigationPlanner
from investos.services.media_workspace import MediaIngestionPolicy, media_temp_workspace
from investos.services.research import ResearchService
from investos.services.youtube_channel import (
    YouTubeChannelEnumerator,
    YouTubeChannelError,
)
from investos.services.youtube_channel_review import YouTubeChannelReviewService
from investos.services.youtube_frame_ocr import LocalFrameOCR, LocalYouTubeFrameOCR
from investos.services.youtube_transcription import (
    LocalTranscript,
    LocalTranscriptionError,
    LocalYouTubeTranscriber,
)

ProgressCallback = Callable[[str, str, dict[str, Any] | None], Awaitable[None]]


class YouTubeService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        audio_transcriber: LocalYouTubeTranscriber | None = None,
        channel_enumerator: YouTubeChannelEnumerator | None = None,
        investigation_planner: MediaInvestigationPlanner | None = None,
        research_service: ResearchService | None = None,
        frame_extractor: LocalYouTubeFrameOCR | None = None,
        media_policy: MediaIngestionPolicy | None = None,
    ):
        self.session = session
        self.ingestion = IngestionService(session)
        self.audio_transcriber = audio_transcriber or LocalYouTubeTranscriber()
        self.channel_enumerator = channel_enumerator or YouTubeChannelEnumerator()
        self.investigation_planner = (
            investigation_planner or MediaInvestigationPlanner()
        )
        self.research = research_service or ResearchService(session)
        self.frame_extractor = frame_extractor or LocalYouTubeFrameOCR()
        self.media_policy = media_policy or MediaIngestionPolicy.from_settings()

    async def ingest_video(
        self,
        url: str,
        *,
        title: str | None = None,
        source_id: UUID | None = None,
        progress_callback: ProgressCallback | None = None,
    ) -> dict[str, Any]:
        url_kind = self._classify_url(url)
        video_id = url_kind.get("video_id")
        if not video_id:
            if url_kind.get("kind") == "channel":
                return {
                    "ok": False,
                    "error": (
                        "This is a YouTube channel URL, not a single video. "
                        "Track the channel as a trusted source, then ingest individual video URLs "
                        "with transcripts or paste manual notes/transcript excerpts."
                    ),
                    "url_kind": "channel",
                    "ingest_mode": "source_record_only",
                }
            return {
                "ok": False,
                "error": "Invalid or unsupported YouTube video URL",
                "url_kind": url_kind.get("kind", "invalid"),
            }

        bound_source = None
        if source_id is not None:
            try:
                bound_source = await self._get_or_create_youtube_source(source_id)
            except ValueError as exc:
                return {
                    "ok": False,
                    "error": str(exc),
                    "url_kind": "video",
                    "video_id": video_id,
                }
            existing = await self._find_existing_video(bound_source.id, video_id)
            if existing is not None:
                existing_metadata = existing.metadata_json or {}
                return {
                    "ok": True,
                    "already_ingested": True,
                    "evidence_id": str(existing.id),
                    "video_id": video_id,
                    "source_id": str(bound_source.id),
                    "ingest_mode": existing_metadata.get("ingest_mode"),
                }

        await self._report(
            progress_callback,
            "captions",
            "Checking for an existing YouTube caption transcript.",
            None,
        )
        source = bound_source
        media_asset_id = f"youtube:{video_id}"
        try:
            transcript_list = await asyncio.to_thread(
                YouTubeTranscriptApi().fetch, video_id
            )
            full_text = " ".join([item.text for item in transcript_list])
        except (NoTranscriptFound, TranscriptsDisabled):
            readiness = self.audio_transcriber.readiness()
            if not readiness["available"]:
                reason = (
                    "Local audio transcription is disabled."
                    if not readiness["enabled"]
                    else "Local audio transcription is missing: "
                    + ", ".join(readiness["missing"])
                    + "."
                )
                return {
                    "ok": False,
                    "error": (
                        "No YouTube caption transcript is available. "
                        f"{reason} Paste a manual transcript or configure the optional local fallback."
                    ),
                    "url_kind": "video",
                    "video_id": video_id,
                    "ingest_mode": "caption_unavailable",
                }
            try:
                transcript = await self._transcribe_audio(
                    url=url,
                    video_id=video_id,
                    readiness=readiness,
                    progress_callback=progress_callback,
                    reason="captions_unavailable",
                )
            except LocalTranscriptionError as exc:
                return {
                    "ok": False,
                    "error": str(exc),
                    "url_kind": "video",
                    "video_id": video_id,
                    "ingest_mode": "local_audio_transcription",
                }
            if not title:
                title = (
                    str(transcript.video_metadata.get("title") or "").strip() or None
                )
            assessment = await self._assess_representation(
                transcript=transcript.text,
                representation="audio_transcript",
                title=title,
                source_url=url,
            )
            source = source or await self._get_or_create_youtube_source()
            evidence = await self._ingest_representation(
                source=source,
                url=url,
                title=title or f"YouTube Video: {video_id}",
                video_id=video_id,
                media_asset_id=media_asset_id,
                transcript=transcript,
                assessment=assessment,
                pass_index=1,
                role="primary",
                trigger="captions_unavailable",
            )
            followups = await self._run_non_media_followups(
                assessment=assessment,
                primary_evidence=evidence,
                source=source,
                url=url,
                title=title,
                video_id=video_id,
                media_asset_id=media_asset_id,
                progress_callback=progress_callback,
                next_pass_index=2,
            )
            await self._attach_followup_outcomes(evidence, followups)
            return self._ingestion_result(
                evidence=evidence,
                source=source,
                video_id=video_id,
                ingest_mode="local_audio_transcription",
                transcript_length=len(transcript.text),
                assessment=assessment,
                passes=[
                    self._pass_result(
                        evidence=evidence,
                        representation="audio_transcript",
                        role="primary",
                        assessment=assessment,
                    ),
                    *followups,
                ],
            )
        except VideoUnavailable:
            return {
                "ok": False,
                "error": "The YouTube video is unavailable.",
                "url_kind": "video",
                "video_id": video_id,
            }
        except Exception as exc:
            return {"ok": False, "error": f"Could not retrieve transcript: {exc}"}

        assessment = await self._assess_representation(
            transcript=full_text,
            representation="caption_transcript",
            title=title,
            source_url=url,
        )
        await self._report(
            progress_callback,
            "evidence",
            "Saving the transcript as dated source evidence.",
            {"ingest_mode": "caption_transcript"},
        )
        source = source or await self._get_or_create_youtube_source()
        evidence = await self.ingestion.ingest_text(
            RawEvidenceCreate(
                title=title or f"YouTube Video: {video_id}",
                source_id=source.id,
                source_item_type="video_transcript",
                url=url,
                metadata_json=self._representation_metadata(
                    video_id=video_id,
                    media_asset_id=media_asset_id,
                    ingest_mode="caption_transcript",
                    representation="caption_transcript",
                    assessment=assessment,
                    pass_index=1,
                    role="primary",
                    trigger="captions_first",
                ),
                content=full_text,
            ),
            process_now=True,
        )
        passes = [
            self._pass_result(
                evidence=evidence,
                representation="caption_transcript",
                role="primary",
                assessment=assessment,
            )
        ]
        if "audio_transcription" in (assessment.get("requested_passes") or []):
            audio_result = await self._run_audio_followup(
                source=source,
                url=url,
                title=title,
                video_id=video_id,
                media_asset_id=media_asset_id,
                primary_evidence=evidence,
                primary_assessment=assessment,
                progress_callback=progress_callback,
            )
            passes.append(audio_result)
        passes.extend(
            await self._run_non_media_followups(
                assessment=assessment,
                primary_evidence=evidence,
                source=source,
                url=url,
                title=title,
                video_id=video_id,
                media_asset_id=media_asset_id,
                progress_callback=progress_callback,
                next_pass_index=len(passes) + 1,
            )
        )
        await self._attach_followup_outcomes(evidence, passes[1:])
        return self._ingestion_result(
            evidence=evidence,
            source=source,
            video_id=video_id,
            ingest_mode="caption_transcript",
            transcript_length=len(full_text),
            assessment=assessment,
            passes=passes,
        )

    async def review_tracked_channels(
        self,
        *,
        source_limit: int | None = None,
        video_limit: int | None = None,
        auto_ingest: bool | None = None,
    ) -> dict[str, Any]:
        return await YouTubeChannelReviewService(
            self.session,
            channel_enumerator=self.channel_enumerator,
            ingest_video=self.ingest_video,
        ).review(
            source_limit=source_limit,
            video_limit=video_limit,
            auto_ingest=auto_ingest,
        )

    async def _assess_representation(
        self,
        *,
        transcript: str,
        representation: str,
        title: str | None,
        source_url: str,
        prior_assessment: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await self.investigation_planner.assess(
            transcript=transcript,
            representation=representation,
            title=title,
            source_url=source_url,
            prior_assessment=prior_assessment,
        )

    async def _transcribe_audio(
        self,
        *,
        url: str,
        video_id: str,
        readiness: dict[str, Any],
        progress_callback: ProgressCallback | None,
        reason: str,
    ) -> LocalTranscript:
        await self._report(
            progress_callback,
            "audio_download",
            "Downloading bounded audio into a temporary workspace.",
            {"video_id": video_id, "reason": reason},
        )
        with media_temp_workspace(self.media_policy) as workspace:
            await self._report(
                progress_callback,
                "transcription",
                f"Running local speech-to-text with model {readiness['model']}.",
                {"reason": reason},
            )
            return await self.audio_transcriber.transcribe(
                url=url,
                video_id=video_id,
                workspace=workspace,
                media_policy=self.media_policy,
            )

    async def _ingest_representation(
        self,
        *,
        source: Source,
        url: str,
        title: str,
        video_id: str,
        media_asset_id: str,
        transcript: LocalTranscript,
        assessment: dict[str, Any],
        pass_index: int,
        role: str,
        trigger: str,
        parent_evidence_id: UUID | None = None,
    ) -> RawEvidence:
        return await self.ingestion.ingest_text(
            RawEvidenceCreate(
                title=title,
                source_id=source.id,
                source_item_type=(
                    "video_audio_transcript"
                    if role == "primary"
                    else "video_audio_transcript_supplement"
                ),
                url=url,
                public_time=self._public_time_from_metadata(transcript.video_metadata),
                metadata_json={
                    **self._representation_metadata(
                        video_id=video_id,
                        media_asset_id=media_asset_id,
                        ingest_mode="local_audio_transcription",
                        representation="audio_transcript",
                        assessment=assessment,
                        pass_index=pass_index,
                        role=role,
                        trigger=trigger,
                        parent_evidence_id=parent_evidence_id,
                    ),
                    "transcriber": "openai_whisper_cli",
                    "transcription_model": transcript.model,
                    "transcript_language": transcript.language,
                    "transcript_segments": transcript.segments,
                    "video_metadata": transcript.video_metadata,
                    "raw_media_persisted": False,
                },
                content=transcript.text,
            ),
            process_now=True,
        )

    async def _run_audio_followup(
        self,
        *,
        source: Source,
        url: str,
        title: str | None,
        video_id: str,
        media_asset_id: str,
        primary_evidence: RawEvidence,
        primary_assessment: dict[str, Any],
        progress_callback: ProgressCallback | None,
    ) -> dict[str, Any]:
        readiness = self.audio_transcriber.readiness()
        if not readiness["available"]:
            return {
                "kind": "media",
                "representation": "audio_transcript",
                "role": "supplemental",
                "status": "blocked",
                "reason": (
                    "disabled_by_operator_policy"
                    if not readiness["enabled"]
                    else "missing: " + ", ".join(readiness["missing"])
                ),
            }
        try:
            transcript = await self._transcribe_audio(
                url=url,
                video_id=video_id,
                readiness=readiness,
                progress_callback=progress_callback,
                reason="caption_assessment_gap",
            )
            assessment = await self._assess_representation(
                transcript=transcript.text,
                representation="audio_transcript",
                title=title,
                source_url=url,
                prior_assessment=primary_assessment,
            )
            evidence = await self._ingest_representation(
                source=source,
                url=url,
                title=(title or f"YouTube Video: {video_id}") + " (audio pass)",
                video_id=video_id,
                media_asset_id=media_asset_id,
                transcript=transcript,
                assessment=assessment,
                pass_index=2,
                role="supplemental",
                trigger="caption_assessment_gap",
                parent_evidence_id=primary_evidence.id,
            )
            return self._pass_result(
                evidence=evidence,
                representation="audio_transcript",
                role="supplemental",
                assessment=assessment,
            )
        except Exception as exc:
            return {
                "kind": "media",
                "representation": "audio_transcript",
                "role": "supplemental",
                "status": "failed",
                "reason": str(exc),
            }

    async def _run_non_media_followups(
        self,
        *,
        assessment: dict[str, Any],
        primary_evidence: RawEvidence,
        source: Source,
        url: str,
        title: str | None,
        video_id: str,
        media_asset_id: str,
        progress_callback: ProgressCallback | None,
        next_pass_index: int,
    ) -> list[dict[str, Any]]:
        requested = set(assessment.get("requested_passes") or [])
        outcomes: list[dict[str, Any]] = []
        if "frame_ocr" in requested:
            outcomes.append(
                await self._run_frame_followup(
                    source=source,
                    url=url,
                    title=title,
                    video_id=video_id,
                    media_asset_id=media_asset_id,
                    primary_evidence=primary_evidence,
                    primary_assessment=assessment,
                    progress_callback=progress_callback,
                    pass_index=next_pass_index,
                )
            )
            next_pass_index += 1
        if "external_verification" not in requested:
            return outcomes
        questions = [
            str(item).strip()
            for item in (assessment.get("followup_questions") or [])
            if str(item).strip()
        ]
        if not questions:
            questions = [
                str(item.get("description") or "").strip()
                for item in (assessment.get("unresolved_gaps") or [])
                if isinstance(item, dict) and str(item.get("description") or "").strip()
            ]
        if not questions:
            outcomes.append(
                {
                    "kind": "research",
                    "representation": "external_verification",
                    "role": "supplemental",
                    "status": "blocked",
                    "reason": "no_specific_followup_question",
                }
            )
            return outcomes
        query = questions[0]
        try:
            result = await self.research.run_ad_hoc_request(
                query=query,
                title=f"Verify media claim: {title or video_id}",
                source_item_type="media_followup_research",
                metadata_json={
                    "trigger": "adaptive_media_followup",
                    "media_asset_id": media_asset_id,
                    "parent_evidence_id": str(primary_evidence.id),
                    "media_video_id": video_id,
                    "followup_question": query,
                    "media_pass": {
                        "index": next_pass_index,
                        "role": "supplemental",
                        "parent_evidence_id": str(primary_evidence.id),
                    },
                },
                process_after_ingest=True,
            )
        except Exception as exc:
            outcomes.append(
                {
                    "kind": "research",
                    "representation": "external_verification",
                    "role": "supplemental",
                    "status": "failed",
                    "reason": str(exc),
                    "query": query,
                }
            )
            return outcomes
        outcomes.append(
            {
                "kind": "research",
                "representation": "external_verification",
                "role": "supplemental",
                "status": "completed" if result.started else "blocked",
                "reason": result.reason,
                "evidence_id": str(result.evidence_id) if result.evidence_id else None,
                "query": result.query,
            }
        )
        return outcomes

    async def _run_frame_followup(
        self,
        *,
        source: Source,
        url: str,
        title: str | None,
        video_id: str,
        media_asset_id: str,
        primary_evidence: RawEvidence,
        primary_assessment: dict[str, Any],
        progress_callback: ProgressCallback | None,
        pass_index: int,
    ) -> dict[str, Any]:
        readiness = self.frame_extractor.readiness()
        if not readiness["available"]:
            return {
                "kind": "media",
                "representation": "frame_ocr",
                "role": "supplemental",
                "status": "blocked",
                "reason": (
                    "disabled_by_operator_policy"
                    if not readiness["enabled"]
                    else "missing: " + ", ".join(readiness["missing"])
                ),
            }
        try:
            await self._report(
                progress_callback,
                "frame_ocr",
                "Sampling bounded video frames and reading on-screen text.",
                {
                    "video_id": video_id,
                    "interval_seconds": readiness["interval_seconds"],
                    "max_frames": readiness["max_frames"],
                },
            )
            with media_temp_workspace(self.media_policy) as workspace:
                extraction = await self.frame_extractor.extract(
                    url=url,
                    video_id=video_id,
                    workspace=workspace,
                    media_policy=self.media_policy,
                )
            followup_assessment = await self._assess_representation(
                transcript=extraction.text,
                representation="frame_ocr",
                title=title,
                source_url=url,
                prior_assessment=primary_assessment,
            )
            evidence = await self._ingest_frame_ocr(
                source=source,
                url=url,
                title=(title or f"YouTube Video: {video_id}") + " (frame OCR pass)",
                video_id=video_id,
                media_asset_id=media_asset_id,
                extraction=extraction,
                assessment=followup_assessment,
                pass_index=pass_index,
                parent_evidence_id=primary_evidence.id,
            )
            return self._pass_result(
                evidence=evidence,
                representation="frame_ocr",
                role="supplemental",
                assessment=followup_assessment,
            )
        except Exception as exc:
            return {
                "kind": "media",
                "representation": "frame_ocr",
                "role": "supplemental",
                "status": "failed",
                "reason": str(exc),
            }

    async def _ingest_frame_ocr(
        self,
        *,
        source: Source,
        url: str,
        title: str,
        video_id: str,
        media_asset_id: str,
        extraction: LocalFrameOCR,
        assessment: dict[str, Any],
        pass_index: int,
        parent_evidence_id: UUID,
    ) -> RawEvidence:
        return await self.ingestion.ingest_text(
            RawEvidenceCreate(
                title=title,
                source_id=source.id,
                source_item_type="video_frame_ocr",
                url=url,
                public_time=self._public_time_from_metadata(extraction.video_metadata),
                metadata_json={
                    **self._representation_metadata(
                        video_id=video_id,
                        media_asset_id=media_asset_id,
                        ingest_mode="local_frame_ocr",
                        representation="frame_ocr",
                        assessment=assessment,
                        pass_index=pass_index,
                        role="supplemental",
                        trigger="caption_assessment_gap",
                        parent_evidence_id=parent_evidence_id,
                    ),
                    "ocr_adapter": "tesseract_cli",
                    "frame_interval_seconds": extraction.interval_seconds,
                    "frames": extraction.frames,
                    "video_metadata": extraction.video_metadata,
                    "raw_media_persisted": False,
                },
                content=extraction.text,
            ),
            process_now=True,
        )

    async def _attach_followup_outcomes(
        self, evidence: RawEvidence, outcomes: list[dict[str, Any]]
    ) -> None:
        if not outcomes:
            return
        evidence.metadata_json = {
            **(evidence.metadata_json or {}),
            "followup_outcomes": outcomes,
        }
        await self.session.commit()

    @staticmethod
    def _representation_metadata(
        *,
        video_id: str,
        media_asset_id: str,
        ingest_mode: str,
        representation: str,
        assessment: dict[str, Any],
        pass_index: int,
        role: str,
        trigger: str,
        parent_evidence_id: UUID | None = None,
    ) -> dict[str, Any]:
        return {
            "video_id": video_id,
            "media_asset_id": media_asset_id,
            "content_type": "text/plain",
            "trigger": trigger,
            "ingest_mode": ingest_mode,
            "representation": representation,
            "media_pass": {
                "index": pass_index,
                "role": role,
                "parent_evidence_id": (
                    str(parent_evidence_id) if parent_evidence_id else None
                ),
            },
            "investigation_assessment": assessment,
        }

    @staticmethod
    def _pass_result(
        *,
        evidence: RawEvidence,
        representation: str,
        role: str,
        assessment: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "kind": "evidence",
            "representation": representation,
            "role": role,
            "status": "completed",
            "evidence_id": str(evidence.id),
            "assessment_status": assessment.get("status"),
        }

    @staticmethod
    def _ingestion_result(
        *,
        evidence: RawEvidence,
        source: Source,
        video_id: str,
        ingest_mode: str,
        transcript_length: int,
        assessment: dict[str, Any],
        passes: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return {
            "ok": True,
            "evidence_id": str(evidence.id),
            "transcript_length": transcript_length,
            "video_id": video_id,
            "source_id": str(source.id),
            "ingest_mode": ingest_mode,
            "already_ingested": False,
            "investigation": assessment,
            "passes": passes,
        }

    async def list_channel_videos(
        self, source_id: UUID, *, limit: int = 12
    ) -> dict[str, Any]:
        source = await self._get_or_create_youtube_source(source_id)
        if not source.url:
            raise YouTubeChannelError(
                "The selected YouTube source does not have a channel URL."
            )
        result = await self.channel_enumerator.list_recent(
            channel_url=source.url,
            limit=limit,
        )
        video_ids = [video["video_id"] for video in result["videos"]]
        evidence_rows = []
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
        evidence_by_video_id = {
            str(metadata.get("video_id")): evidence
            for evidence in evidence_rows
            if isinstance((metadata := evidence.metadata_json), dict)
            and metadata.get("video_id")
        }
        videos = []
        for video in result["videos"]:
            existing = evidence_by_video_id.get(video["video_id"])
            videos.append(
                {
                    **video,
                    "already_ingested": existing is not None,
                    "evidence_id": existing.id if existing is not None else None,
                }
            )
        return {
            "source_id": source.id,
            "source_name": source.name,
            "channel_url": result["channel_url"],
            "channel_id": result.get("channel_id"),
            "channel_name": result.get("channel_name"),
            "videos": videos,
        }

    @classmethod
    def media_capabilities(cls) -> dict[str, Any]:
        transcript_available = (
            importlib.util.find_spec("youtube_transcript_api") is not None
        )
        transcriber = LocalYouTubeTranscriber()
        transcription_readiness = transcriber.readiness()
        frame_readiness = LocalYouTubeFrameOCR().readiness()
        channel_readiness = YouTubeChannelEnumerator().readiness()
        media_policy = MediaIngestionPolicy.from_settings()
        return {
            "can_extract_without_transcript": bool(
                transcription_readiness["available"]
            ),
            "current_best_path": (
                "Ingest or track a YouTube source. Prophet reads captions first, assesses what remains unresolved, and runs only available material follow-up passes."
                if transcription_readiness["available"]
                else "Ingest or track YouTube sources for caption-first analysis. Missing material audio or visual coverage is reported honestly unless the optional adapter is configured."
            ),
            "capabilities": [
                {
                    "key": "caption_transcript",
                    "label": "Caption transcript ingestion",
                    "status": "available" if transcript_available else "not_configured",
                    "detail": (
                        "Can ingest an individual video's existing YouTube captions."
                        if transcript_available
                        else "youtube-transcript-api is not installed in the backend environment."
                    ),
                },
                {
                    "key": "adaptive_transcript_assessment",
                    "label": "Adaptive transcript assessment",
                    "status": (
                        "available"
                        if settings.YOUTUBE_ADAPTIVE_INVESTIGATION_ENABLED
                        else "disabled"
                    ),
                    "detail": (
                        "Assesses the actual transcript for material unresolved questions and requests bounded follow-up passes only when warranted."
                        if settings.YOUTUBE_ADAPTIVE_INVESTIGATION_ENABLED
                        else "Disabled by operator policy."
                    ),
                },
                {
                    "key": "channel_source_record",
                    "label": "Channel source tracking",
                    "status": "available",
                    "detail": "Can store a channel as a trusted source and preserve provenance.",
                },
                {
                    "key": "channel_video_enumeration",
                    "label": "Channel video enumeration",
                    "status": (
                        "available"
                        if channel_readiness["available"]
                        else "not_configured"
                    ),
                    "detail": (
                        "Periodically reviews a bounded list of uploads from operator-tracked channels; metadata remains provisional until the video content is fetched."
                        if channel_readiness["available"]
                        else "Install yt-dlp to review recent uploads from tracked channels."
                    ),
                },
                {
                    "key": "audio_transcription",
                    "label": "No-transcript audio transcription",
                    "status": (
                        "available"
                        if transcription_readiness["available"]
                        else (
                            "disabled"
                            if not transcription_readiness["enabled"]
                            else "not_configured"
                        )
                    ),
                    "detail": cls._audio_capability_detail(transcription_readiness),
                },
                {
                    "key": "frame_or_slide_ocr",
                    "label": "Frame/slide OCR",
                    "status": (
                        "available"
                        if frame_readiness["available"]
                        else (
                            "disabled"
                            if not frame_readiness["enabled"]
                            else "not_configured"
                        )
                    ),
                    "detail": cls._frame_ocr_capability_detail(frame_readiness),
                },
                *media_policy.capability_rows(),
            ],
        }

    def _extract_video_id(self, url: str) -> str | None:
        return self._classify_url(url).get("video_id")

    @staticmethod
    def _classify_url(url: str) -> dict[str, str | None]:
        parsed = urlparse((url or "").strip())
        host = (parsed.netloc or "").lower()
        path_parts = [part for part in parsed.path.split("/") if part]
        if host.startswith("www."):
            host = host[4:]

        def valid_video_id(value: str | None) -> str | None:
            if value and re.fullmatch(r"[0-9A-Za-z_-]{11}", value):
                return value
            return None

        if host == "youtu.be":
            video_id = valid_video_id(path_parts[0] if path_parts else None)
            return {"kind": "video" if video_id else "invalid", "video_id": video_id}

        if host == "youtube.com" or host.endswith(".youtube.com"):
            query_video_id = valid_video_id(parse_qs(parsed.query).get("v", [None])[0])
            if query_video_id:
                return {"kind": "video", "video_id": query_video_id}

            if path_parts:
                first = path_parts[0].lower()
                if first in {"shorts", "embed", "live"}:
                    video_id = valid_video_id(
                        path_parts[1] if len(path_parts) > 1 else None
                    )
                    return {
                        "kind": "video" if video_id else "invalid",
                        "video_id": video_id,
                    }
                if (
                    first.startswith("@")
                    or first in {"channel", "c", "user"}
                    or first in {"playlist", "feed", "results"}
                ):
                    return {"kind": "channel", "video_id": None}

        return {"kind": "invalid", "video_id": None}

    async def _get_or_create_youtube_source(
        self, source_id: UUID | None = None
    ) -> Source:
        if source_id is not None:
            source = (
                await self.session.execute(select(Source).where(Source.id == source_id))
            ).scalar_one_or_none()
            if source is None:
                raise ValueError("The selected source no longer exists.")
            if source.source_type != "youtube":
                raise ValueError("The selected source is not a YouTube source.")
            return source
        source_name = "YouTube Research"
        existing = (
            await self.session.execute(select(Source).where(Source.name == source_name))
        ).scalar_one_or_none()
        if existing:
            return existing
        source = Source(
            name=source_name,
            source_type="youtube",
            description="Caption or optional local speech-to-text transcripts from YouTube videos. This connector does not inspect frames, slides, or audio tone.",
            is_trusted=False,
        )
        self.session.add(source)
        await self.session.flush()
        return source

    async def _find_existing_video(
        self, source_id: UUID, video_id: str
    ) -> RawEvidence | None:
        return (
            await self.session.execute(
                select(RawEvidence)
                .where(RawEvidence.source_id == source_id)
                .where(RawEvidence.metadata_json["video_id"].astext == video_id)
                .limit(1)
            )
        ).scalar_one_or_none()

    @staticmethod
    async def _report(
        callback: ProgressCallback | None,
        phase: str,
        message: str,
        detail: dict[str, Any] | None,
    ) -> None:
        if callback is not None:
            await callback(phase, message, detail)

    @staticmethod
    def _audio_capability_detail(readiness: dict[str, Any]) -> str:
        if readiness["available"]:
            return (
                "Captionless videos can use installed yt-dlp, ffmpeg, and the OpenAI Whisper CLI "
                f"with model {readiness['model']}; raw media is deleted after transcript extraction."
            )
        if not readiness["enabled"]:
            return (
                "Disabled by operator policy. Set YOUTUBE_LOCAL_TRANSCRIPTION_ENABLED=true only "
                "after installing yt-dlp, ffmpeg, and the OpenAI Whisper CLI."
            )
        missing = ", ".join(readiness["missing"]) or "required local tools"
        return f"Enabled but not ready; missing: {missing}."

    @staticmethod
    def _frame_ocr_capability_detail(readiness: dict[str, Any]) -> str:
        if readiness["available"]:
            return (
                "Material visual gaps can use installed yt-dlp, ffmpeg, and Tesseract with "
                f"one frame every {readiness['interval_seconds']} seconds, bounded to "
                f"{readiness['max_frames']} frames; raw media is deleted afterward."
            )
        if not readiness["enabled"]:
            return (
                "Disabled by operator policy. Set YOUTUBE_FRAME_OCR_ENABLED=true only "
                "after installing yt-dlp, ffmpeg, and Tesseract."
            )
        missing = ", ".join(readiness["missing"]) or "required local tools"
        return f"Enabled but not ready; missing: {missing}."

    @staticmethod
    def _public_time_from_metadata(metadata: dict[str, Any]) -> datetime | None:
        raw_timestamp = metadata.get("release_timestamp") or metadata.get("timestamp")
        try:
            if raw_timestamp is not None:
                return datetime.fromtimestamp(float(raw_timestamp), tz=UTC)
        except (OSError, TypeError, ValueError):
            pass
        upload_date = str(metadata.get("upload_date") or "").strip()
        try:
            return datetime.strptime(upload_date, "%Y%m%d").replace(tzinfo=UTC)
        except ValueError:
            return None
