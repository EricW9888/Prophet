from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from youtube_transcript_api import TranscriptsDisabled

from investos.services import youtube as youtube_module
from investos.services.media_workspace import MediaIngestionPolicy
from investos.services.youtube import YouTubeService
from investos.services.youtube_transcription import LocalTranscript


def test_youtube_url_parser_accepts_video_shapes():
    service = YouTubeService(SimpleNamespace())

    assert (
        service._extract_video_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        == "dQw4w9WgXcQ"
    )
    assert (
        service._extract_video_id("https://youtu.be/dQw4w9WgXcQ?t=10") == "dQw4w9WgXcQ"
    )
    assert (
        service._extract_video_id("https://www.youtube.com/shorts/dQw4w9WgXcQ")
        == "dQw4w9WgXcQ"
    )
    assert (
        service._extract_video_id("https://www.youtube.com/embed/dQw4w9WgXcQ")
        == "dQw4w9WgXcQ"
    )


def test_youtube_url_parser_rejects_channel_as_video():
    service = YouTubeService(SimpleNamespace())

    assert service._extract_video_id("https://www.youtube.com/@ExampleFinance") is None
    assert YouTubeService._classify_url("https://www.youtube.com/@ExampleFinance") == {
        "kind": "channel",
        "video_id": None,
    }


def test_youtube_url_parser_rejects_lookalike_host():
    service = YouTubeService(SimpleNamespace())

    assert (
        service._extract_video_id("https://evilyoutube.com/watch?v=dQw4w9WgXcQ") is None
    )


def test_youtube_media_capabilities_report_disabled_local_fallback(monkeypatch):
    monkeypatch.setattr(
        youtube_module.LocalYouTubeTranscriber,
        "readiness",
        lambda _self: {
            "enabled": False,
            "available": False,
            "downloader_path": None,
            "transcriber_path": None,
            "ffmpeg_path": None,
            "missing": ["yt-dlp", "whisper", "ffmpeg"],
            "model": "base",
        },
    )
    result = YouTubeService.media_capabilities()

    assert result["can_extract_without_transcript"] is False
    assert "local audio fallback" in result["current_best_path"]
    statuses = {item["key"]: item["status"] for item in result["capabilities"]}
    assert statuses["caption_transcript"] == "available"
    assert statuses["channel_source_record"] == "available"
    assert statuses["channel_video_enumeration"] == "not_configured"
    assert statuses["audio_transcription"] == "disabled"
    assert statuses["frame_or_slide_ocr"] == "not_configured"
    assert statuses["temporary_workspace_cleanup"] == "available"
    assert statuses["raw_media_persistence"] == "disabled"


@pytest.mark.asyncio
async def test_youtube_channel_ingest_returns_actionable_source_record_message():
    result = await YouTubeService(SimpleNamespace()).ingest_video(
        "https://www.youtube.com/@ExampleFinance"
    )

    assert result["ok"] is False
    assert result["url_kind"] == "channel"
    assert result["ingest_mode"] == "source_record_only"
    assert "individual video URLs" in result["error"]


@pytest.mark.asyncio
async def test_captioned_video_bypasses_local_audio_fallback(monkeypatch):
    class UnexpectedTranscriber:
        def readiness(self):
            raise AssertionError("captioned videos must not inspect audio readiness")

    service = YouTubeService(
        SimpleNamespace(), audio_transcriber=UnexpectedTranscriber()
    )
    service.ingestion = SimpleNamespace(
        ingest_text=AsyncMock(return_value=SimpleNamespace(id=uuid4()))
    )
    service._get_or_create_youtube_source = AsyncMock(
        return_value=SimpleNamespace(id=uuid4())
    )
    monkeypatch.setattr(
        youtube_module.YouTubeTranscriptApi,
        "fetch",
        lambda _self, _video_id: [
            SimpleNamespace(text="Memory pricing"),
            SimpleNamespace(text="improved."),
        ],
    )

    result = await service.ingest_video("https://www.youtube.com/watch?v=dQw4w9WgXcQ")

    payload = service.ingestion.ingest_text.await_args.args[0]
    assert result["ok"] is True
    assert result["ingest_mode"] == "caption_transcript"
    assert payload.content == "Memory pricing improved."
    assert payload.metadata_json["ingest_mode"] == "caption_transcript"
    assert payload.source_item_type == "video_transcript"


@pytest.mark.asyncio
async def test_captionless_video_uses_local_fallback_and_cleans_workspace(
    tmp_path, monkeypatch
):
    class StubTranscriber:
        workspace = None

        def readiness(self):
            return {
                "enabled": True,
                "available": True,
                "downloader_path": "/tools/yt-dlp",
                "transcriber_path": "/tools/whisper",
                "ffmpeg_path": "/tools/ffmpeg",
                "missing": [],
                "model": "base",
            }

        async def transcribe(self, *, workspace, **_kwargs):
            self.workspace = workspace
            (workspace / "source.webm").write_bytes(b"temporary audio")
            return LocalTranscript(
                text="HBM demand tightened conventional memory supply.",
                segments=[
                    {
                        "start": 2.0,
                        "end": 6.5,
                        "text": "HBM demand tightened conventional memory supply.",
                    }
                ],
                language="en",
                model="base",
                video_metadata={
                    "title": "Memory cycle",
                    "upload_date": "20260820",
                    "duration": 120,
                },
            )

    transcriber = StubTranscriber()
    policy = MediaIngestionPolicy(
        temp_dir=tmp_path,
        temp_retention_hours=24,
        persist_raw_media=False,
        max_download_mb=64,
    )
    service = YouTubeService(
        SimpleNamespace(),
        audio_transcriber=transcriber,
        media_policy=policy,
    )
    service.ingestion = SimpleNamespace(
        ingest_text=AsyncMock(return_value=SimpleNamespace(id=uuid4()))
    )
    service._get_or_create_youtube_source = AsyncMock(
        return_value=SimpleNamespace(id=uuid4())
    )

    def no_captions(_self, _video_id):
        raise TranscriptsDisabled("dQw4w9WgXcQ")

    monkeypatch.setattr(youtube_module.YouTubeTranscriptApi, "fetch", no_captions)

    result = await service.ingest_video("https://www.youtube.com/watch?v=dQw4w9WgXcQ")

    payload = service.ingestion.ingest_text.await_args.args[0]
    assert result["ok"] is True
    assert result["ingest_mode"] == "local_audio_transcription"
    assert payload.title == "Memory cycle"
    assert payload.source_item_type == "video_audio_transcript"
    assert payload.public_time == datetime(2026, 8, 20, tzinfo=UTC)
    assert payload.metadata_json["transcriber"] == "openai_whisper_cli"
    assert payload.metadata_json["raw_media_persisted"] is False
    assert payload.metadata_json["transcript_segments"][0]["start"] == 2.0
    assert transcriber.workspace is not None
    assert not transcriber.workspace.exists()


@pytest.mark.asyncio
async def test_captionless_video_fails_honestly_when_local_fallback_is_disabled(
    monkeypatch,
):
    class DisabledTranscriber:
        def readiness(self):
            return {
                "enabled": False,
                "available": False,
                "downloader_path": None,
                "transcriber_path": None,
                "ffmpeg_path": None,
                "missing": ["yt-dlp", "whisper", "ffmpeg"],
                "model": "base",
            }

    service = YouTubeService(SimpleNamespace(), audio_transcriber=DisabledTranscriber())

    def no_captions(_self, _video_id):
        raise TranscriptsDisabled("dQw4w9WgXcQ")

    monkeypatch.setattr(youtube_module.YouTubeTranscriptApi, "fetch", no_captions)

    result = await service.ingest_video("https://www.youtube.com/watch?v=dQw4w9WgXcQ")

    assert result["ok"] is False
    assert result["ingest_mode"] == "caption_unavailable"
    assert "disabled" in result["error"]
    assert "manual transcript" in result["error"]
