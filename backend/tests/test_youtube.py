from types import SimpleNamespace

import pytest

from investos.services.youtube import YouTubeService


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


def test_youtube_media_capabilities_do_not_claim_no_transcript_extraction():
    result = YouTubeService.media_capabilities()

    assert result["can_extract_without_transcript"] is False
    assert "manual transcript/notes" in result["current_best_path"]
    statuses = {item["key"]: item["status"] for item in result["capabilities"]}
    assert statuses["caption_transcript"] == "available"
    assert statuses["channel_source_record"] == "available"
    assert statuses["audio_transcription"] == "not_configured"
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
