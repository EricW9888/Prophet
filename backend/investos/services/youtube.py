from __future__ import annotations

import importlib.util
import re
import shutil
from typing import Any
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
from investos.services.media_workspace import MediaIngestionPolicy


class YouTubeService:
    def __init__(self, session: AsyncSession):
        self.session = session
        self.ingestion = IngestionService(session)

    async def ingest_video(
        self, url: str, *, title: str | None = None
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

        try:
            transcript_list = YouTubeTranscriptApi().fetch(video_id)
            full_text = " ".join([item.text for item in transcript_list])
        except (NoTranscriptFound, TranscriptsDisabled):
            return {
                "ok": False,
                "error": (
                    "No transcript is available for this video. Paste a manual transcript, "
                    "notes, or use a future audio/video transcription connector before treating "
                    "it as evidence."
                ),
                "url_kind": "video",
                "video_id": video_id,
                "ingest_mode": "transcript_only",
            }
        except VideoUnavailable:
            return {
                "ok": False,
                "error": "The YouTube video is unavailable.",
                "url_kind": "video",
                "video_id": video_id,
            }
        except Exception as exc:
            return {"ok": False, "error": f"Could not retrieve transcript: {exc}"}

        source = await self._get_or_create_youtube_source()
        evidence = await self.ingestion.ingest_text(
            RawEvidenceCreate(
                title=title or f"YouTube Video: {video_id}",
                source_id=source.id,
                source_item_type="video_transcript",
                url=url,
                metadata_json={
                    "video_id": video_id,
                    "content_type": "text/plain",
                    "trigger": "manual_youtube_ingest",
                    "ingest_mode": "transcript_only",
                },
                content=full_text,
            ),
            process_now=True,
        )

        return {
            "ok": True,
            "evidence_id": str(evidence.id),
            "transcript_length": len(full_text),
            "video_id": video_id,
            "ingest_mode": "transcript_only",
        }

    @classmethod
    def media_capabilities(cls) -> dict[str, Any]:
        transcript_available = (
            importlib.util.find_spec("youtube_transcript_api") is not None
        )
        ytdlp_available = (
            importlib.util.find_spec("yt_dlp") is not None
            or shutil.which("yt-dlp") is not None
            or shutil.which("youtube-dl") is not None
        )
        # No speech-to-text or visual/OCR provider is wired into Prophet yet.
        # Keep this explicit so "video ingestion" cannot be confused with
        # caption-transcript ingestion.
        audio_transcription_available = False
        frame_ocr_available = False
        media_policy = MediaIngestionPolicy.from_settings()
        return {
            "can_extract_without_transcript": bool(
                ytdlp_available and audio_transcription_available
            ),
            "current_best_path": (
                "Track the channel as a source, ingest individual video URLs when captions exist, "
                "or paste manual transcript/notes for no-caption videos."
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
                    "status": "available" if ytdlp_available else "not_configured",
                    "detail": (
                        "A downloader/enumerator is available, but Prophet still needs review policy before background channel crawling."
                        if ytdlp_available
                        else "yt-dlp/youtube-dl is not installed, so Prophet cannot enumerate channel videos automatically."
                    ),
                },
                {
                    "key": "audio_transcription",
                    "label": "No-transcript audio transcription",
                    "status": "not_configured",
                    "detail": "No speech-to-text provider is configured, so no-caption videos are not automatically extractable.",
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
            description="Transcript text from YouTube videos. This connector does not inspect frames, slides, or audio tone.",
            is_trusted=True,
        )
        self.session.add(source)
        await self.session.flush()
        return source
