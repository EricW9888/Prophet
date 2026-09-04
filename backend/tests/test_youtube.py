from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from youtube_transcript_api import TranscriptsDisabled

from investos.services import youtube as youtube_module
from investos.services.media_workspace import MediaIngestionPolicy
from investos.services.youtube import YouTubeService
from investos.services.youtube_frame_ocr import LocalFrameOCR
from investos.services.youtube_transcription import LocalTranscript


def assessment(*, requested_passes=None, sufficient=True):
    return {
        "status": "complete",
        "materiality": "medium",
        "first_pass_sufficient": sufficient,
        "confidence": 0.8,
        "reason": "Representation assessed.",
        "resolved_points": ["The stated memory-demand claim."],
        "unresolved_gaps": [],
        "requested_passes": requested_passes or [],
        "followup_questions": [],
    }


class StubPlanner:
    def __init__(self, *responses):
        self.responses = list(responses) or [assessment()]
        self.calls = []

    async def assess(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)


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
    monkeypatch.setattr(
        youtube_module.YouTubeChannelEnumerator,
        "readiness",
        lambda _self: {
            "available": False,
            "downloader_path": None,
            "missing": ["yt-dlp"],
        },
    )
    monkeypatch.setattr(
        youtube_module.LocalYouTubeFrameOCR,
        "readiness",
        lambda _self: {
            "enabled": False,
            "available": False,
            "downloader_path": None,
            "ffmpeg_path": None,
            "ocr_path": None,
            "missing": ["yt-dlp", "ffmpeg", "tesseract"],
            "interval_seconds": 30,
            "max_frames": 12,
        },
    )
    result = YouTubeService.media_capabilities()

    assert result["can_extract_without_transcript"] is False
    assert "caption-first analysis" in result["current_best_path"]
    statuses = {item["key"]: item["status"] for item in result["capabilities"]}
    assert statuses["caption_transcript"] == "available"
    assert statuses["adaptive_transcript_assessment"] == "available"
    assert statuses["channel_source_record"] == "available"
    assert statuses["channel_video_enumeration"] == "not_configured"
    assert statuses["audio_transcription"] == "disabled"
    assert statuses["frame_or_slide_ocr"] == "disabled"
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

    planner = StubPlanner(assessment())
    service = YouTubeService(
        SimpleNamespace(),
        audio_transcriber=UnexpectedTranscriber(),
        investigation_planner=planner,
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
    assert payload.metadata_json["representation"] == "caption_transcript"
    assert payload.metadata_json["media_pass"]["role"] == "primary"
    assert payload.metadata_json["investigation_assessment"]["status"] == "complete"
    assert payload.source_item_type == "video_transcript"
    assert len(planner.calls) == 1


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
    planner = StubPlanner(assessment())
    service = YouTubeService(
        SimpleNamespace(),
        audio_transcriber=transcriber,
        investigation_planner=planner,
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
    assert payload.metadata_json["representation"] == "audio_transcript"
    assert payload.metadata_json["investigation_assessment"]["status"] == "complete"
    assert transcriber.workspace is not None
    assert not transcriber.workspace.exists()
    assert planner.calls[0]["representation"] == "audio_transcript"


@pytest.mark.asyncio
async def test_material_caption_gap_runs_linked_audio_pass(tmp_path, monkeypatch):
    class StubTranscriber:
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

        async def transcribe(self, **_kwargs):
            return LocalTranscript(
                text="The chart shows NAND pricing improved twelve percent.",
                segments=[],
                language="en",
                model="base",
                video_metadata={"title": "Memory update", "duration": 90},
            )

    first = assessment(requested_passes=["audio_transcription"], sufficient=False)
    first["unresolved_gaps"] = [
        {
            "description": "Captions omit the quantified pricing statement.",
            "why_material": "The magnitude changes the margin read-through.",
            "recommended_pass": "audio_transcription",
        }
    ]
    planner = StubPlanner(first, assessment())
    primary_id = uuid4()
    supplement_id = uuid4()
    session = SimpleNamespace(commit=AsyncMock())
    service = YouTubeService(
        session,
        audio_transcriber=StubTranscriber(),
        investigation_planner=planner,
        media_policy=MediaIngestionPolicy(
            temp_dir=tmp_path,
            temp_retention_hours=24,
            persist_raw_media=False,
            max_download_mb=64,
        ),
    )
    service.ingestion = SimpleNamespace(
        ingest_text=AsyncMock(
            side_effect=[
                SimpleNamespace(id=primary_id, metadata_json={}),
                SimpleNamespace(id=supplement_id, metadata_json={}),
            ]
        )
    )
    service._get_or_create_youtube_source = AsyncMock(
        return_value=SimpleNamespace(id=uuid4())
    )
    monkeypatch.setattr(
        youtube_module.YouTubeTranscriptApi,
        "fetch",
        lambda _self, _video_id: [SimpleNamespace(text="Pricing improved.")],
    )

    result = await service.ingest_video("https://www.youtube.com/watch?v=dQw4w9WgXcQ")

    assert result["ok"] is True
    assert [item["representation"] for item in result["passes"]] == [
        "caption_transcript",
        "audio_transcript",
    ]
    assert service.ingestion.ingest_text.await_count == 2
    supplement = service.ingestion.ingest_text.await_args_list[1].args[0]
    assert supplement.source_item_type == "video_audio_transcript_supplement"
    assert supplement.metadata_json["media_pass"] == {
        "index": 2,
        "role": "supplemental",
        "parent_evidence_id": str(primary_id),
    }
    assert planner.calls[1]["prior_assessment"] == first
    session.commit.assert_awaited()


@pytest.mark.asyncio
async def test_external_verification_uses_specific_planner_question(monkeypatch):
    first = assessment(requested_passes=["external_verification"], sufficient=False)
    first["followup_questions"] = [
        "Did the issuer report a twelve percent NAND pricing increase?"
    ]
    research = SimpleNamespace(
        run_ad_hoc_request=AsyncMock(
            return_value=SimpleNamespace(
                started=True,
                reason="ok",
                evidence_id=uuid4(),
                query="issuer filing NAND pricing",
            )
        )
    )
    evidence = SimpleNamespace(id=uuid4(), metadata_json={})
    session = SimpleNamespace(commit=AsyncMock())
    service = YouTubeService(
        session,
        audio_transcriber=SimpleNamespace(),
        investigation_planner=StubPlanner(first),
        research_service=research,
    )
    service.ingestion = SimpleNamespace(ingest_text=AsyncMock(return_value=evidence))
    service._get_or_create_youtube_source = AsyncMock(
        return_value=SimpleNamespace(id=uuid4())
    )
    monkeypatch.setattr(
        youtube_module.YouTubeTranscriptApi,
        "fetch",
        lambda _self, _video_id: [SimpleNamespace(text="NAND pricing rose 12%.")],
    )

    result = await service.ingest_video("https://www.youtube.com/watch?v=dQw4w9WgXcQ")

    request = research.run_ad_hoc_request.await_args.kwargs
    assert request["query"] == first["followup_questions"][0]
    assert request["metadata_json"]["parent_evidence_id"] == str(evidence.id)
    assert result["passes"][1]["representation"] == "external_verification"
    assert result["passes"][1]["status"] == "completed"


@pytest.mark.asyncio
async def test_failed_supplemental_pass_preserves_primary_caption_evidence(
    tmp_path, monkeypatch
):
    class FailingTranscriber:
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

        async def transcribe(self, **_kwargs):
            raise RuntimeError("speech adapter failed")

    primary = SimpleNamespace(id=uuid4(), metadata_json={})
    session = SimpleNamespace(commit=AsyncMock())
    service = YouTubeService(
        session,
        audio_transcriber=FailingTranscriber(),
        investigation_planner=StubPlanner(
            assessment(requested_passes=["audio_transcription"], sufficient=False)
        ),
        media_policy=MediaIngestionPolicy(
            temp_dir=tmp_path,
            temp_retention_hours=24,
            persist_raw_media=False,
            max_download_mb=64,
        ),
    )
    service.ingestion = SimpleNamespace(ingest_text=AsyncMock(return_value=primary))
    service._get_or_create_youtube_source = AsyncMock(
        return_value=SimpleNamespace(id=uuid4())
    )
    monkeypatch.setattr(
        youtube_module.YouTubeTranscriptApi,
        "fetch",
        lambda _self, _video_id: [SimpleNamespace(text="Caption evidence.")],
    )

    result = await service.ingest_video("https://www.youtube.com/watch?v=dQw4w9WgXcQ")

    assert result["ok"] is True
    assert result["evidence_id"] == str(primary.id)
    assert result["passes"][1]["status"] == "failed"
    assert result["passes"][1]["reason"] == "speech adapter failed"
    assert primary.metadata_json["followup_outcomes"][0]["status"] == "failed"


@pytest.mark.asyncio
async def test_material_visual_gap_runs_linked_frame_ocr_pass(tmp_path, monkeypatch):
    class StubFrameExtractor:
        def readiness(self):
            return {
                "enabled": True,
                "available": True,
                "downloader_path": "/tools/yt-dlp",
                "ffmpeg_path": "/tools/ffmpeg",
                "ocr_path": "/tools/tesseract",
                "missing": [],
                "interval_seconds": 30,
                "max_frames": 20,
            }

        async def extract(self, **_kwargs):
            return LocalFrameOCR(
                text="[Frame 00:00:30]\nNAND ASP +12% quarter over quarter",
                frames=[
                    {
                        "frame_index": 2,
                        "timestamp_seconds": 30,
                        "text": "NAND ASP +12% quarter over quarter",
                    }
                ],
                interval_seconds=30,
                video_metadata={"title": "Memory chart", "duration": 90},
            )

    first = assessment(requested_passes=["frame_ocr"], sufficient=False)
    second = assessment()
    planner = StubPlanner(first, second)
    primary_id = uuid4()
    frame_id = uuid4()
    session = SimpleNamespace(commit=AsyncMock())
    service = YouTubeService(
        session,
        investigation_planner=planner,
        frame_extractor=StubFrameExtractor(),
        media_policy=MediaIngestionPolicy(
            temp_dir=tmp_path,
            temp_retention_hours=24,
            persist_raw_media=False,
            max_download_mb=64,
        ),
    )
    service.ingestion = SimpleNamespace(
        ingest_text=AsyncMock(
            side_effect=[
                SimpleNamespace(id=primary_id, metadata_json={}),
                SimpleNamespace(id=frame_id, metadata_json={}),
            ]
        )
    )
    service._get_or_create_youtube_source = AsyncMock(
        return_value=SimpleNamespace(id=uuid4())
    )
    monkeypatch.setattr(
        youtube_module.YouTubeTranscriptApi,
        "fetch",
        lambda _self, _video_id: [
            SimpleNamespace(text="The chart shows pricing improved.")
        ],
    )

    result = await service.ingest_video("https://www.youtube.com/watch?v=dQw4w9WgXcQ")

    assert [item["representation"] for item in result["passes"]] == [
        "caption_transcript",
        "frame_ocr",
    ]
    frame_payload = service.ingestion.ingest_text.await_args_list[1].args[0]
    assert frame_payload.source_item_type == "video_frame_ocr"
    assert frame_payload.metadata_json["media_pass"]["parent_evidence_id"] == str(
        primary_id
    )
    assert frame_payload.metadata_json["frames"][0]["timestamp_seconds"] == 30
    assert frame_payload.metadata_json["raw_media_persisted"] is False
    assert planner.calls[1]["representation"] == "frame_ocr"


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
