from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import delete

from investos.api.routes import source as source_routes
from investos.db import async_session_maker
from investos.models.evidence import RawEvidence
from investos.models.source import Source
from investos.services.youtube import YouTubeService
from investos.services.youtube_channel import (
    YouTubeChannelConfig,
    YouTubeChannelEnumerator,
    YouTubeChannelError,
)


def channel_config(**overrides) -> YouTubeChannelConfig:
    values = {"downloader_bin": "yt-dlp", "timeout_seconds": 30}
    values.update(overrides)
    return YouTubeChannelConfig(**values)


def test_channel_url_normalization_accepts_supported_youtube_shapes():
    normalize = YouTubeChannelEnumerator.canonical_channel_videos_url

    assert (
        normalize("https://youtube.com/@RhinoFinance")
        == "https://www.youtube.com/@RhinoFinance/videos"
    )
    assert (
        normalize("https://www.youtube.com/channel/UC123/videos?view=0")
        == "https://www.youtube.com/channel/UC123/videos"
    )
    assert (
        normalize("https://m.youtube.com/user/Example")
        == "https://www.youtube.com/user/Example/videos"
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://evilyoutube.com/@RhinoFinance",
        "https://youtube.example/@RhinoFinance",
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://www.youtube.com/playlist?list=PL123",
    ],
)
def test_channel_url_normalization_rejects_non_channel_urls(url):
    with pytest.raises(YouTubeChannelError):
        YouTubeChannelEnumerator.canonical_channel_videos_url(url)


@pytest.mark.asyncio
async def test_channel_listing_is_metadata_only_bounded_and_normalized(monkeypatch):
    enumerator = YouTubeChannelEnumerator(channel_config())
    monkeypatch.setattr(
        enumerator,
        "readiness",
        lambda: {
            "available": True,
            "downloader_path": "/tools/yt-dlp",
            "missing": [],
        },
    )
    captured = []

    async def run_process(*args):
        captured.append(args)
        return json.dumps(
            {
                "channel_id": "UC-rhino",
                "channel": "Rhino Finance",
                "entries": [
                    {
                        "id": "dQw4w9WgXcQ",
                        "title": "Memory cycle update",
                        "upload_date": "20260823",
                        "duration": 754,
                        "view_count": 12000,
                        "availability": "public",
                    },
                    {"id": "bad", "title": "Invalid id"},
                    {"id": "dQw4w9WgXcQ", "title": "Duplicate"},
                ],
            }
        )

    monkeypatch.setattr(enumerator, "_run_process", run_process)

    result = await enumerator.list_recent(
        channel_url="https://www.youtube.com/@RhinoFinance", limit=12
    )

    command = captured[0]
    assert command[0] == "/tools/yt-dlp"
    assert "--ignore-config" in command
    assert "--no-remote-components" in command
    assert "--flat-playlist" in command
    assert command[command.index("--playlist-end") + 1] == "12"
    assert "--dump-single-json" in command
    assert "--no-simulate" not in command
    assert result["channel_name"] == "Rhino Finance"
    assert result["videos"] == [
        {
            "video_id": "dQw4w9WgXcQ",
            "title": "Memory cycle update",
            "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "published_at": datetime(2026, 8, 23, tzinfo=UTC),
            "duration_seconds": 754.0,
            "view_count": 12000,
            "live_status": None,
            "availability": "public",
        }
    ]


@pytest.mark.asyncio
async def test_channel_listing_reports_missing_downloader_without_running(monkeypatch):
    enumerator = YouTubeChannelEnumerator(channel_config())
    monkeypatch.setattr(
        enumerator,
        "readiness",
        lambda: {
            "available": False,
            "downloader_path": None,
            "missing": ["yt-dlp"],
        },
    )

    with pytest.raises(YouTubeChannelError, match="missing: yt-dlp") as exc_info:
        await enumerator.list_recent(
            channel_url="https://www.youtube.com/@RhinoFinance", limit=12
        )

    assert exc_info.value.unavailable is True


class ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class ScalarListResult:
    def __init__(self, values):
        self.values = values

    def scalars(self):
        return self

    def all(self):
        return self.values


@pytest.mark.asyncio
async def test_channel_preview_binds_ingestion_state_to_tracked_source():
    source_id = uuid4()
    evidence_id = uuid4()
    source = SimpleNamespace(
        id=source_id,
        name="Rhino Finance",
        source_type="youtube",
        url="https://www.youtube.com/@RhinoFinance",
    )
    evidence = SimpleNamespace(
        id=evidence_id,
        metadata_json={"video_id": "dQw4w9WgXcQ"},
    )
    session = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[ScalarResult(source), ScalarListResult([evidence])]
        )
    )
    enumerator = SimpleNamespace(
        list_recent=AsyncMock(
            return_value={
                "channel_url": "https://www.youtube.com/@RhinoFinance/videos",
                "channel_id": "UC-rhino",
                "channel_name": "Rhino Finance",
                "videos": [
                    {
                        "video_id": "dQw4w9WgXcQ",
                        "title": "Memory cycle update",
                        "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                        "published_at": None,
                        "duration_seconds": None,
                        "view_count": None,
                        "live_status": None,
                        "availability": "public",
                    }
                ],
            }
        )
    )

    result = await YouTubeService(
        session, channel_enumerator=enumerator
    ).list_channel_videos(source_id, limit=12)

    assert result["source_id"] == source_id
    assert result["videos"][0]["already_ingested"] is True
    assert result["videos"][0]["evidence_id"] == evidence_id


@pytest.mark.asyncio
async def test_tracked_video_ingestion_is_idempotent_before_transcript_fetch(
    monkeypatch,
):
    source_id = uuid4()
    evidence_id = uuid4()
    source = SimpleNamespace(
        id=source_id,
        name="Rhino Finance",
        source_type="youtube",
        url="https://www.youtube.com/@RhinoFinance",
    )
    existing = SimpleNamespace(
        id=evidence_id,
        metadata_json={"video_id": "dQw4w9WgXcQ", "ingest_mode": "caption_transcript"},
    )
    session = SimpleNamespace(
        execute=AsyncMock(side_effect=[ScalarResult(source), ScalarResult(existing)])
    )

    def unexpected_fetch(*_args):
        raise AssertionError("an already-ingested video must not be fetched again")

    monkeypatch.setattr(
        "investos.services.youtube.YouTubeTranscriptApi.fetch", unexpected_fetch
    )

    result = await YouTubeService(session).ingest_video(
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        source_id=source_id,
    )

    assert result == {
        "ok": True,
        "already_ingested": True,
        "evidence_id": str(evidence_id),
        "video_id": "dQw4w9WgXcQ",
        "source_id": str(source_id),
        "ingest_mode": "caption_transcript",
    }


@pytest.mark.asyncio(loop_scope="session")
async def test_existing_video_lookup_uses_persisted_json_provenance():
    source_id = uuid4()
    evidence_id = uuid4()
    async with async_session_maker() as session:
        session.add(
            Source(
                id=source_id,
                name=f"YouTube lookup test {source_id}",
                source_type="youtube",
                url="https://www.youtube.com/@ExampleFinance",
                is_trusted=False,
            )
        )
        await session.flush()
        session.add(
            RawEvidence(
                id=evidence_id,
                source_id=source_id,
                source_item_type="video_transcript",
                metadata_json={"video_id": "dQw4w9WgXcQ"},
            )
        )
        await session.commit()
        try:
            found = await YouTubeService(session)._find_existing_video(
                source_id, "dQw4w9WgXcQ"
            )
            assert found is not None
            assert found.id == evidence_id
        finally:
            await session.execute(
                delete(RawEvidence).where(RawEvidence.id == evidence_id)
            )
            await session.execute(delete(Source).where(Source.id == source_id))
            await session.commit()


@pytest.mark.asyncio
async def test_channel_preview_route_maps_missing_local_tool_to_service_unavailable(
    monkeypatch,
):
    class UnavailableService:
        def __init__(self, _session):
            pass

        async def list_channel_videos(self, _source_id, *, limit):
            assert limit == 12
            raise YouTubeChannelError("yt-dlp is missing", unavailable=True)

    monkeypatch.setattr(source_routes, "YouTubeService", UnavailableService)

    with pytest.raises(HTTPException) as exc_info:
        await source_routes.list_youtube_channel_videos(
            uuid4(), limit=12, session=SimpleNamespace()
        )

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == "yt-dlp is missing"


@pytest.mark.asyncio
async def test_channel_preview_route_rejects_invalid_source(monkeypatch):
    class InvalidSourceService:
        def __init__(self, _session):
            pass

        async def list_channel_videos(self, _source_id, *, limit):
            assert limit == 12
            raise ValueError("The selected source is not a YouTube source.")

    monkeypatch.setattr(source_routes, "YouTubeService", InvalidSourceService)

    with pytest.raises(HTTPException) as exc_info:
        await source_routes.list_youtube_channel_videos(
            uuid4(), limit=12, session=SimpleNamespace()
        )

    assert exc_info.value.status_code == 400
