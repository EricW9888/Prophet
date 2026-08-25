from __future__ import annotations

import asyncio
import json

import pytest

from investos.services.media_workspace import MediaIngestionPolicy
from investos.services.youtube_transcription import (
    LocalTranscriptionConfig,
    LocalTranscriptionError,
    LocalYouTubeTranscriber,
)


def transcription_config(**overrides) -> LocalTranscriptionConfig:
    values = {
        "enabled": True,
        "downloader_bin": "yt-dlp",
        "transcriber_bin": "whisper",
        "model": "base",
        "timeout_seconds": 30,
        "max_duration_seconds": 3600,
    }
    values.update(overrides)
    return LocalTranscriptionConfig(**values)


def media_policy(tmp_path, *, max_download_mb: int = 64) -> MediaIngestionPolicy:
    return MediaIngestionPolicy(
        temp_dir=tmp_path,
        temp_retention_hours=24,
        persist_raw_media=False,
        max_download_mb=max_download_mb,
    )


def test_readiness_requires_explicit_enablement_and_all_local_tools(monkeypatch):
    paths = {
        "yt-dlp": "/tools/yt-dlp",
        "whisper": "/tools/whisper",
        "ffmpeg": "/tools/ffmpeg",
    }
    monkeypatch.setattr(
        "investos.services.youtube_transcription.shutil.which", paths.get
    )

    disabled = LocalYouTubeTranscriber(transcription_config(enabled=False)).readiness()
    enabled = LocalYouTubeTranscriber(transcription_config()).readiness()

    assert disabled["enabled"] is False
    assert disabled["available"] is False
    assert enabled["available"] is True
    assert enabled["missing"] == []


@pytest.mark.asyncio
async def test_transcription_rejects_oversized_duration_before_download(
    tmp_path, monkeypatch
):
    adapter = LocalYouTubeTranscriber(transcription_config(max_duration_seconds=120))
    monkeypatch.setattr(
        adapter,
        "readiness",
        lambda: {
            "enabled": True,
            "available": True,
            "downloader_path": "/tools/yt-dlp",
            "transcriber_path": "/tools/whisper",
            "ffmpeg_path": "/tools/ffmpeg",
            "missing": [],
            "model": "base",
        },
    )

    async def probe(**_kwargs):
        return {"id": "dQw4w9WgXcQ", "duration": 121}

    async def unexpected_download(**_kwargs):
        raise AssertionError("duration validation must run before download")

    monkeypatch.setattr(adapter, "_probe_video", probe)
    monkeypatch.setattr(adapter, "_download_audio", unexpected_download)

    with pytest.raises(LocalTranscriptionError, match="duration exceeds"):
        await adapter.transcribe(
            url="https://youtu.be/dQw4w9WgXcQ",
            video_id="dQw4w9WgXcQ",
            workspace=tmp_path,
            media_policy=media_policy(tmp_path),
        )


@pytest.mark.asyncio
async def test_download_command_is_bounded_and_output_stays_in_workspace(
    tmp_path, monkeypatch
):
    adapter = LocalYouTubeTranscriber(transcription_config())
    captured = []

    async def run_process(*args):
        captured.append(args)
        output_path = tmp_path / "source.webm"
        output_path.write_bytes(b"audio")
        return str(output_path)

    monkeypatch.setattr(adapter, "_run_process", run_process)

    result = await adapter._download_audio(
        downloader="/tools/yt-dlp",
        url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        workspace=tmp_path,
        max_download_mb=64,
    )

    command = captured[0]
    assert result == tmp_path / "source.webm"
    assert "--ignore-config" in command
    assert "--no-playlist" in command
    assert command[command.index("--max-filesize") + 1] == "64M"
    assert command[command.index("--paths") + 1] == str(tmp_path)


@pytest.mark.asyncio
async def test_download_rejects_output_outside_workspace(tmp_path, monkeypatch):
    adapter = LocalYouTubeTranscriber(transcription_config())
    outside = tmp_path.parent / "outside.webm"
    outside.write_bytes(b"audio")

    async def run_process(*_args):
        return str(outside)

    monkeypatch.setattr(adapter, "_run_process", run_process)

    with pytest.raises(LocalTranscriptionError, match="bounded media file"):
        await adapter._download_audio(
            downloader="/tools/yt-dlp",
            url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            workspace=tmp_path,
            max_download_mb=64,
        )


@pytest.mark.asyncio
async def test_whisper_requires_valid_json_output(tmp_path, monkeypatch):
    adapter = LocalYouTubeTranscriber(transcription_config(model="small"))
    media_path = tmp_path / "source.webm"
    media_path.write_bytes(b"audio")

    async def run_process(*args):
        assert args[args.index("--model") + 1] == "small"
        (tmp_path / "source.json").write_text(
            json.dumps(
                {
                    "text": "Demand accelerated.",
                    "language": "en",
                    "segments": [
                        {"start": 1.25, "end": 3.5, "text": "Demand accelerated."}
                    ],
                }
            ),
            encoding="utf-8",
        )
        return ""

    monkeypatch.setattr(adapter, "_run_process", run_process)

    result = await adapter._run_whisper(
        transcriber="/tools/whisper",
        media_path=media_path,
        workspace=tmp_path,
    )

    assert result["text"] == "Demand accelerated."


@pytest.mark.asyncio
async def test_process_timeout_kills_media_tool(monkeypatch):
    adapter = LocalYouTubeTranscriber(transcription_config(timeout_seconds=0.01))

    class SlowProcess:
        def __init__(self):
            self.returncode = None
            self.killed = False
            self.calls = 0

        async def communicate(self):
            self.calls += 1
            if self.calls == 1:
                await asyncio.sleep(1)
            return b"", b""

        def kill(self):
            self.killed = True
            self.returncode = -9

        def terminate(self):
            self.returncode = -15

    process = SlowProcess()

    async def create_process(*_args, **_kwargs):
        return process

    monkeypatch.setattr(
        "investos.services.youtube_transcription.asyncio.create_subprocess_exec",
        create_process,
    )

    with pytest.raises(LocalTranscriptionError, match="timeout"):
        await adapter._run_process("tool", "arg")

    assert process.killed is True


def test_segment_normalization_discards_unusable_items():
    segments = LocalYouTubeTranscriber._normalize_segments(
        [
            {"start": 0, "end": 2.5, "text": " First point "},
            {"start": "bad", "end": 4, "text": "No start"},
            {"start": 4, "end": 5, "text": ""},
            "not a segment",
        ]
    )

    assert segments == [{"start": 0.0, "end": 2.5, "text": "First point"}]
