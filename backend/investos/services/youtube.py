from __future__ import annotations

import asyncio
import importlib.util
import re
from datetime import UTC, datetime
from typing import Any, Awaitable, Callable
from urllib.parse import parse_qs, urlparse

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from youtube_transcript_api import (
    NoTranscriptFound,
    TranscriptsDisabled,
    VideoUnavailable,
    YouTubeTranscriptApi,
)

from investos.models.source import Source
from investos.schemas.evidence import RawEvidenceCreate
from investos.services.ingestion import IngestionService
from investos.services.media_workspace import (
    MediaIngestionPolicy,
    media_temp_workspace,
)
from investos.services.youtube_transcription import (
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
        media_policy: MediaIngestionPolicy | None = None,
    ):
        self.session = session
        self.ingestion = IngestionService(session)
        self.audio_transcriber = audio_transcriber or LocalYouTubeTranscriber()
        self.media_policy = media_policy or MediaIngestionPolicy.from_settings()

    async def ingest_video(
        self,
        url: str,
        *,
        title: str | None = None,
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

        await self._report(
            progress_callback,
            "captions",
            "Checking for an existing YouTube caption transcript.",
            None,
        )
        ingest_mode = "caption_transcript"
        source_item_type = "video_transcript"
        metadata: dict[str, Any] = {
            "video_id": video_id,
            "content_type": "text/plain",
            "trigger": "manual_youtube_ingest",
            "ingest_mode": ingest_mode,
        }
        public_time = None
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
            await self._report(
                progress_callback,
                "audio_download",
                "Captions are unavailable; downloading bounded audio into a temporary workspace.",
                {"video_id": video_id},
            )
            try:
                with media_temp_workspace(self.media_policy) as workspace:
                    await self._report(
                        progress_callback,
                        "transcription",
                        f"Running local speech-to-text with model {readiness['model']}.",
                        None,
                    )
                    transcript = await self.audio_transcriber.transcribe(
                        url=url,
                        video_id=video_id,
                        workspace=workspace,
                        media_policy=self.media_policy,
                    )
            except LocalTranscriptionError as exc:
                return {
                    "ok": False,
                    "error": str(exc),
                    "url_kind": "video",
                    "video_id": video_id,
                    "ingest_mode": "local_audio_transcription",
                }
            full_text = transcript.text
            ingest_mode = "local_audio_transcription"
            source_item_type = "video_audio_transcript"
            metadata.update(
                {
                    "ingest_mode": ingest_mode,
                    "transcriber": "openai_whisper_cli",
                    "transcription_model": transcript.model,
                    "transcript_language": transcript.language,
                    "transcript_segments": transcript.segments,
                    "video_metadata": transcript.video_metadata,
                    "raw_media_persisted": False,
                }
            )
            public_time = self._public_time_from_metadata(transcript.video_metadata)
            if not title:
                title = (
                    str(transcript.video_metadata.get("title") or "").strip() or None
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

        await self._report(
            progress_callback,
            "evidence",
            "Saving the transcript as dated source evidence.",
            {"ingest_mode": ingest_mode},
        )
        source = await self._get_or_create_youtube_source()
        evidence = await self.ingestion.ingest_text(
            RawEvidenceCreate(
                title=title or f"YouTube Video: {video_id}",
                source_id=source.id,
                source_item_type=source_item_type,
                url=url,
                public_time=public_time,
                metadata_json=metadata,
                content=full_text,
            ),
            process_now=True,
        )

        return {
            "ok": True,
            "evidence_id": str(evidence.id),
            "transcript_length": len(full_text),
            "video_id": video_id,
            "ingest_mode": ingest_mode,
        }

    @classmethod
    def media_capabilities(cls) -> dict[str, Any]:
        transcript_available = (
            importlib.util.find_spec("youtube_transcript_api") is not None
        )
        transcriber = LocalYouTubeTranscriber()
        transcription_readiness = transcriber.readiness()
        ytdlp_available = bool(transcription_readiness["downloader_path"])
        frame_ocr_available = False
        media_policy = MediaIngestionPolicy.from_settings()
        return {
            "can_extract_without_transcript": bool(
                transcription_readiness["available"]
            ),
            "current_best_path": (
                "Ingest an individual video URL. Prophet uses captions first and can fall back to explicitly enabled local audio transcription when all required tools are ready."
                if transcription_readiness["available"]
                else "Ingest individual videos when captions exist, paste manual transcript/notes, or explicitly configure the free local audio fallback for no-caption videos."
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
                    "key": "channel_source_record",
                    "label": "Channel source tracking",
                    "status": "available",
                    "detail": "Can store a channel as a trusted source and preserve provenance.",
                },
                {
                    "key": "channel_video_enumeration",
                    "label": "Channel video enumeration",
                    "status": "unsupported",
                    "detail": (
                        "yt-dlp is installed, but Prophet does not yet enumerate or crawl channel uploads."
                        if ytdlp_available
                        else "Prophet does not yet enumerate or crawl channel uploads; yt-dlp is also not installed."
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
                    "status": "not_configured",
                    "detail": "No frame extraction or OCR pipeline is configured for charts, slides, or on-screen text.",
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

        if host.endswith("youtube.com"):
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

    async def _get_or_create_youtube_source(self) -> Source:
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
            is_trusted=True,
        )
        self.session.add(source)
        await self.session.flush()
        return source

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
