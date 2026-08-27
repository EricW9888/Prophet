from __future__ import annotations

import asyncio
import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from investos.config import settings
from investos.services.media_workspace import MediaIngestionPolicy


class LocalFrameOCRError(RuntimeError):
    pass


@dataclass(frozen=True)
class LocalFrameOCRConfig:
    enabled: bool
    downloader_bin: str
    ocr_bin: str
    interval_seconds: int
    max_frames: int
    timeout_seconds: int
    max_duration_seconds: int

    @classmethod
    def from_settings(cls) -> "LocalFrameOCRConfig":
        return cls(
            enabled=bool(settings.YOUTUBE_FRAME_OCR_ENABLED),
            downloader_bin=settings.YOUTUBE_DOWNLOADER_BIN.strip() or "yt-dlp",
            ocr_bin=settings.YOUTUBE_OCR_BIN.strip() or "tesseract",
            interval_seconds=max(int(settings.YOUTUBE_FRAME_INTERVAL_SECONDS), 5),
            max_frames=max(1, min(int(settings.YOUTUBE_FRAME_MAX_COUNT), 500)),
            timeout_seconds=max(
                int(settings.YOUTUBE_FRAME_EXTRACTION_TIMEOUT_SECONDS), 30
            ),
            max_duration_seconds=max(int(settings.YOUTUBE_MAX_DURATION_SECONDS), 60),
        )


@dataclass(frozen=True)
class LocalFrameOCR:
    text: str
    frames: list[dict[str, Any]]
    interval_seconds: int
    video_metadata: dict[str, Any]


class LocalYouTubeFrameOCR:
    """Bounded frame sampling and OCR through explicitly installed CLI tools."""

    def __init__(self, config: LocalFrameOCRConfig | None = None):
        self.config = config or LocalFrameOCRConfig.from_settings()

    def readiness(self) -> dict[str, Any]:
        downloader_path = shutil.which(self.config.downloader_bin)
        ffmpeg_path = shutil.which("ffmpeg")
        ocr_path = shutil.which(self.config.ocr_bin)
        missing = [
            label
            for label, path in (
                (self.config.downloader_bin, downloader_path),
                ("ffmpeg", ffmpeg_path),
                (self.config.ocr_bin, ocr_path),
            )
            if path is None
        ]
        return {
            "enabled": self.config.enabled,
            "available": bool(self.config.enabled and not missing),
            "downloader_path": downloader_path,
            "ffmpeg_path": ffmpeg_path,
            "ocr_path": ocr_path,
            "missing": missing,
            "interval_seconds": self.config.interval_seconds,
            "max_frames": self.config.max_frames,
        }

    async def extract(
        self,
        *,
        url: str,
        video_id: str,
        workspace: Path,
        media_policy: MediaIngestionPolicy,
    ) -> LocalFrameOCR:
        readiness = self.readiness()
        if not self.config.enabled:
            raise LocalFrameOCRError(
                "Local YouTube frame OCR is disabled by operator policy."
            )
        if not readiness["available"]:
            raise LocalFrameOCRError(
                "Local YouTube frame OCR is not ready; missing: "
                + ", ".join(readiness["missing"])
                + "."
            )
        try:
            return await asyncio.wait_for(
                self._extract_ready(
                    url=url,
                    video_id=video_id,
                    workspace=workspace,
                    media_policy=media_policy,
                    readiness=readiness,
                ),
                timeout=self.config.timeout_seconds,
            )
        except TimeoutError as exc:
            raise LocalFrameOCRError("Frame extraction timed out.") from exc

    async def _extract_ready(
        self,
        *,
        url: str,
        video_id: str,
        workspace: Path,
        media_policy: MediaIngestionPolicy,
        readiness: dict[str, Any],
    ) -> LocalFrameOCR:
        canonical_url = f"https://www.youtube.com/watch?v={video_id}"
        metadata = await self._probe_video(
            downloader=str(readiness["downloader_path"]),
            url=canonical_url,
        )
        returned_id = str(metadata.get("id") or "")
        if returned_id and returned_id != video_id:
            raise LocalFrameOCRError(
                "The downloader returned metadata for a different YouTube video."
            )
        self._validate_metadata(metadata, media_policy=media_policy)
        media_path = await self._download_video(
            downloader=str(readiness["downloader_path"]),
            url=canonical_url,
            workspace=workspace,
            max_download_mb=media_policy.max_download_mb,
        )
        frames_dir = workspace / "frames"
        frames_dir.mkdir(mode=0o700)
        await self._extract_frames(
            ffmpeg=str(readiness["ffmpeg_path"]),
            media_path=media_path,
            frames_dir=frames_dir,
        )
        frames = await self._read_frames(
            ocr=str(readiness["ocr_path"]),
            frames_dir=frames_dir,
        )
        if not frames:
            raise LocalFrameOCRError(
                "Frame extraction completed without readable on-screen text."
            )
        text = "\n\n".join(
            f"[Frame {self._format_timestamp(item['timestamp_seconds'])}]\n{item['text']}"
            for item in frames
        )
        return LocalFrameOCR(
            text=text,
            frames=frames,
            interval_seconds=self.config.interval_seconds,
            video_metadata=self._safe_video_metadata(metadata, url=url),
        )

    async def _probe_video(self, *, downloader: str, url: str) -> dict[str, Any]:
        stdout = await self._run_process(
            downloader,
            "--ignore-config",
            "--no-remote-components",
            "--no-playlist",
            "--skip-download",
            "--dump-single-json",
            "--no-warnings",
            url,
        )
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise LocalFrameOCRError(
                "The downloader did not return valid video metadata."
            ) from exc
        if not isinstance(payload, dict):
            raise LocalFrameOCRError(
                "The downloader returned unsupported video metadata."
            )
        return payload

    def _validate_metadata(
        self, metadata: dict[str, Any], *, media_policy: MediaIngestionPolicy
    ) -> None:
        duration = metadata.get("duration")
        if duration is not None:
            try:
                if float(duration) > self.config.max_duration_seconds:
                    raise LocalFrameOCRError(
                        "Video duration exceeds the configured media limit."
                    )
            except (TypeError, ValueError) as exc:
                raise LocalFrameOCRError(
                    "The video duration metadata is invalid."
                ) from exc
        size = metadata.get("filesize") or metadata.get("filesize_approx")
        if size is not None:
            try:
                if float(size) > media_policy.max_download_mb * 1024 * 1024:
                    raise LocalFrameOCRError(
                        "Video size exceeds the configured media limit."
                    )
            except (TypeError, ValueError) as exc:
                raise LocalFrameOCRError("The video size metadata is invalid.") from exc

    async def _download_video(
        self,
        *,
        downloader: str,
        url: str,
        workspace: Path,
        max_download_mb: int,
    ) -> Path:
        await self._run_process(
            downloader,
            "--ignore-config",
            "--no-remote-components",
            "--no-playlist",
            "--no-warnings",
            "--max-filesize",
            f"{max_download_mb}M",
            "--format",
            "bestvideo[height<=1080]/best[height<=1080]",
            "--output",
            str(workspace / "source.%(ext)s"),
            url,
        )
        candidates = [
            path
            for path in workspace.glob("source.*")
            if path.is_file() and path.suffix not in {".part", ".ytdl"}
        ]
        if len(candidates) != 1:
            raise LocalFrameOCRError(
                "The downloader did not produce one bounded video file."
            )
        media_path = candidates[0].resolve()
        if media_path.parent != workspace.resolve():
            raise LocalFrameOCRError(
                "The downloader produced media outside the temporary workspace."
            )
        size = media_path.stat().st_size
        if size <= 0 or size > max_download_mb * 1024 * 1024:
            raise LocalFrameOCRError(
                "The downloaded video is empty or exceeds the configured media limit."
            )
        return media_path

    async def _extract_frames(
        self, *, ffmpeg: str, media_path: Path, frames_dir: Path
    ) -> None:
        await self._run_process(
            ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(media_path),
            "-vf",
            f"fps=1/{self.config.interval_seconds},scale=min(1600\\,iw):-2",
            "-frames:v",
            str(self.config.max_frames),
            str(frames_dir / "frame-%05d.png"),
        )

    async def _read_frames(self, *, ocr: str, frames_dir: Path) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        seen_text: set[str] = set()
        for index, frame_path in enumerate(sorted(frames_dir.glob("frame-*.png"))):
            if index >= self.config.max_frames:
                break
            text = " ".join(
                (
                    await self._run_process(
                        ocr,
                        str(frame_path),
                        "stdout",
                        "--psm",
                        "6",
                    )
                ).split()
            ).strip()
            normalized = text.casefold()
            if not normalized or normalized in seen_text:
                continue
            seen_text.add(normalized)
            rows.append(
                {
                    "frame_index": index + 1,
                    "timestamp_seconds": index * self.config.interval_seconds,
                    "text": text,
                }
            )
        return rows

    async def _run_process(self, *args: str) -> str:
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=self.config.timeout_seconds
            )
        except TimeoutError as exc:
            process.kill()
            await process.communicate()
            raise LocalFrameOCRError("Frame extraction timed out.") from exc
        if process.returncode != 0:
            detail = stderr.decode("utf-8", errors="replace").strip()
            raise LocalFrameOCRError(
                "Frame extraction command failed"
                + (f": {detail[:500]}" if detail else ".")
            )
        return stdout.decode("utf-8", errors="replace")

    @staticmethod
    def _format_timestamp(seconds: int) -> str:
        minutes, remaining = divmod(max(0, int(seconds)), 60)
        hours, minutes = divmod(minutes, 60)
        return f"{hours:02d}:{minutes:02d}:{remaining:02d}"

    @staticmethod
    def _safe_video_metadata(metadata: dict[str, Any], *, url: str) -> dict[str, Any]:
        allowed = {
            "id",
            "title",
            "description",
            "duration",
            "upload_date",
            "release_timestamp",
            "timestamp",
            "channel",
            "channel_id",
            "uploader",
            "uploader_id",
            "webpage_url",
        }
        safe = {
            key: metadata.get(key) for key in allowed if metadata.get(key) is not None
        }
        safe["source_url"] = url
        return safe
