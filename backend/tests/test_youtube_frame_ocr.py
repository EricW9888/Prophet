import json
from pathlib import Path

import pytest

from investos.services.media_workspace import MediaIngestionPolicy
from investos.services.youtube_frame_ocr import (
    LocalFrameOCRConfig,
    LocalYouTubeFrameOCR,
)


def frame_config(**overrides) -> LocalFrameOCRConfig:
    values = {
        "enabled": True,
        "downloader_bin": "yt-dlp",
        "ocr_bin": "tesseract",
        "interval_seconds": 30,
        "max_frames": 10,
        "timeout_seconds": 60,
        "max_duration_seconds": 3600,
    }
    values.update(overrides)
    return LocalFrameOCRConfig(**values)


def test_frame_ocr_readiness_respects_operator_policy(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: f"/tools/{name}")

    readiness = LocalYouTubeFrameOCR(frame_config(enabled=False)).readiness()

    assert readiness["enabled"] is False
    assert readiness["available"] is False
    assert readiness["missing"] == []


@pytest.mark.asyncio
async def test_frame_ocr_is_bounded_attributed_and_deduplicated(tmp_path, monkeypatch):
    extractor = LocalYouTubeFrameOCR(frame_config())
    monkeypatch.setattr(
        extractor,
        "readiness",
        lambda: {
            "enabled": True,
            "available": True,
            "downloader_path": "/tools/yt-dlp",
            "ffmpeg_path": "/tools/ffmpeg",
            "ocr_path": "/tools/tesseract",
            "missing": [],
            "interval_seconds": 30,
            "max_frames": 10,
        },
    )
    commands = []

    async def run_process(*args):
        commands.append(args)
        if "--dump-single-json" in args:
            return json.dumps(
                {
                    "id": "dQw4w9WgXcQ",
                    "title": "Memory charts",
                    "duration": 90,
                    "upload_date": "20260820",
                }
            )
        if args[0] == "/tools/yt-dlp":
            output = Path(args[args.index("--output") + 1].replace("%(ext)s", "mp4"))
            output.write_bytes(b"bounded test video")
            return ""
        if args[0] == "/tools/ffmpeg":
            pattern = Path(args[-1])
            pattern.parent.mkdir(exist_ok=True)
            (pattern.parent / "frame-00001.png").write_bytes(b"frame one")
            (pattern.parent / "frame-00002.png").write_bytes(b"frame two")
            return ""
        if args[0] == "/tools/tesseract":
            return "NAND ASP +12%\n"
        raise AssertionError(f"unexpected command: {args}")

    monkeypatch.setattr(extractor, "_run_process", run_process)
    result = await extractor.extract(
        url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        video_id="dQw4w9WgXcQ",
        workspace=tmp_path,
        media_policy=MediaIngestionPolicy(
            temp_dir=tmp_path,
            temp_retention_hours=24,
            persist_raw_media=False,
            max_download_mb=64,
        ),
    )

    assert result.text == "[Frame 00:00:00]\nNAND ASP +12%"
    assert len(result.frames) == 1
    assert result.video_metadata["source_url"].endswith("dQw4w9WgXcQ")
    ffmpeg_command = next(
        command for command in commands if command[0] == "/tools/ffmpeg"
    )
    assert ffmpeg_command[ffmpeg_command.index("-frames:v") + 1] == "10"
    assert any(command[0] == "/tools/tesseract" for command in commands)
