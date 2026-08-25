from __future__ import annotations

import asyncio
import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from investos.config import settings
from investos.services.media_workspace import MediaIngestionPolicy


class LocalTranscriptionError(RuntimeError):
    pass


@dataclass(frozen=True)
class LocalTranscriptionConfig:
    enabled: bool
    downloader_bin: str
    transcriber_bin: str
    model: str
    timeout_seconds: int
    max_duration_seconds: int

    @classmethod
    def from_settings(cls) -> "LocalTranscriptionConfig":
        return cls(
            enabled=bool(settings.YOUTUBE_LOCAL_TRANSCRIPTION_ENABLED),
            downloader_bin=settings.YOUTUBE_DOWNLOADER_BIN.strip() or "yt-dlp",
            transcriber_bin=settings.YOUTUBE_TRANSCRIBER_BIN.strip() or "whisper",
            model=settings.YOUTUBE_TRANSCRIPTION_MODEL.strip() or "base",
            timeout_seconds=max(
                int(settings.YOUTUBE_TRANSCRIPTION_TIMEOUT_SECONDS), 30
            ),
            max_duration_seconds=max(int(settings.YOUTUBE_MAX_DURATION_SECONDS), 60),
        )


@dataclass(frozen=True)
class LocalTranscript:
    text: str
    segments: list[dict[str, Any]]
    language: str | None
    model: str
    video_metadata: dict[str, Any]


class LocalYouTubeTranscriber:
    """Adapter for explicitly installed yt-dlp and OpenAI Whisper CLIs."""

    def __init__(self, config: LocalTranscriptionConfig | None = None):
        self.config = config or LocalTranscriptionConfig.from_settings()

    def readiness(self) -> dict[str, Any]:
        downloader_path = shutil.which(self.config.downloader_bin)
        transcriber_path = shutil.which(self.config.transcriber_bin)
        ffmpeg_path = shutil.which("ffmpeg")
        missing = [
            label
            for label, path in (
                (self.config.downloader_bin, downloader_path),
                (self.config.transcriber_bin, transcriber_path),
                ("ffmpeg", ffmpeg_path),
            )
            if path is None
        ]
        return {
            "enabled": self.config.enabled,
            "available": bool(self.config.enabled and not missing),
            "downloader_path": downloader_path,
            "transcriber_path": transcriber_path,
            "ffmpeg_path": ffmpeg_path,
            "missing": missing,
            "model": self.config.model,
        }

    async def transcribe(
        self,
        *,
        url: str,
        video_id: str,
        workspace: Path,
        media_policy: MediaIngestionPolicy,
    ) -> LocalTranscript:
        readiness = self.readiness()
        if not self.config.enabled:
            raise LocalTranscriptionError(
                "Local YouTube transcription is disabled by operator policy."
            )
        if not readiness["available"]:
            missing = ", ".join(readiness["missing"])
            raise LocalTranscriptionError(
                f"Local YouTube transcription is not ready; missing: {missing}."
            )

        canonical_url = f"https://www.youtube.com/watch?v={video_id}"
        metadata = await self._probe_video(
            downloader=str(readiness["downloader_path"]),
            url=canonical_url,
        )
        returned_id = str(metadata.get("id") or "")
        if returned_id and returned_id != video_id:
            raise LocalTranscriptionError(
                "The downloader returned metadata for a different YouTube video."
            )
        self._validate_metadata(metadata, media_policy=media_policy)

        media_path = await self._download_audio(
            downloader=str(readiness["downloader_path"]),
            url=canonical_url,
            workspace=workspace,
            max_download_mb=media_policy.max_download_mb,
        )
        downloaded_size = media_path.stat().st_size
        if downloaded_size <= 0:
            raise LocalTranscriptionError(
                "The downloader produced an empty media file."
            )
        if downloaded_size > media_policy.max_download_mb * 1024 * 1024:
            raise LocalTranscriptionError(
                f"Downloaded media exceeded the {media_policy.max_download_mb} MB limit."
            )

        transcript = await self._run_whisper(
            transcriber=str(readiness["transcriber_path"]),
            media_path=media_path,
            workspace=workspace,
        )
        text = str(transcript.get("text") or "").strip()
        if not text:
            raise LocalTranscriptionError(
                "Local transcription completed without producing transcript text."
            )
        segments = self._normalize_segments(transcript.get("segments"))
        language = str(transcript.get("language") or "").strip() or None
        return LocalTranscript(
            text=text,
            segments=segments,
            language=language,
            model=self.config.model,
            video_metadata=self._safe_video_metadata(metadata, url=url),
        )

    async def _probe_video(self, *, downloader: str, url: str) -> dict[str, Any]:
        stdout = await self._run_process(
            downloader,
            "--ignore-config",
            "--no-playlist",
            "--skip-download",
            "--dump-single-json",
            "--no-warnings",
            url,
        )
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise LocalTranscriptionError(
                "The downloader did not return valid video metadata."
            ) from exc
        if not isinstance(payload, dict):
            raise LocalTranscriptionError(
                "The downloader returned an unsupported metadata payload."
            )
        return payload

    def _validate_metadata(
        self,
        metadata: dict[str, Any],
        *,
        media_policy: MediaIngestionPolicy,
    ) -> None:
        duration = self._as_float(metadata.get("duration"))
        if duration is None or duration <= 0 or metadata.get("is_live"):
            raise LocalTranscriptionError(
                "Video duration could not be verified before download."
            )
        if duration > self.config.max_duration_seconds:
            raise LocalTranscriptionError(
                f"Video duration exceeds the {self.config.max_duration_seconds}-second limit."
            )
        estimated_size = self._as_int(
            metadata.get("filesize") or metadata.get("filesize_approx")
        )
        max_bytes = media_policy.max_download_mb * 1024 * 1024
        if estimated_size is not None and estimated_size > max_bytes:
            raise LocalTranscriptionError(
                f"Estimated media size exceeds the {media_policy.max_download_mb} MB limit."
            )

    async def _download_audio(
        self,
        *,
        downloader: str,
        url: str,
        workspace: Path,
        max_download_mb: int,
    ) -> Path:
        stdout = await self._run_process(
            downloader,
            "--ignore-config",
            "--no-playlist",
            "--no-progress",
            "--no-warnings",
            "--format",
            "bestaudio/best",
            "--max-filesize",
            f"{max_download_mb}M",
            "--paths",
            str(workspace),
            "--output",
            "source.%(ext)s",
            "--print",
            "after_move:filepath",
            url,
        )
        output_lines = [line.strip() for line in stdout.splitlines() if line.strip()]
        candidates = [Path(output_lines[-1])] if output_lines else []
        candidates.extend(path for path in workspace.iterdir() if path.is_file())
        workspace_root = workspace.resolve()
        for candidate in candidates:
            resolved = candidate.resolve()
            if resolved.is_relative_to(workspace_root) and resolved.is_file():
                return resolved
        raise LocalTranscriptionError(
            "The downloader completed without producing a bounded media file."
        )

    async def _run_whisper(
        self,
        *,
        transcriber: str,
        media_path: Path,
        workspace: Path,
    ) -> dict[str, Any]:
        await self._run_process(
            transcriber,
            str(media_path),
            "--model",
            self.config.model,
            "--output_format",
            "json",
            "--output_dir",
            str(workspace),
            "--fp16",
            "False",
            "--verbose",
            "False",
        )
        output_path = workspace / f"{media_path.stem}.json"
        if not output_path.is_file():
            raise LocalTranscriptionError(
                "The transcriber completed without producing its JSON transcript."
            )
        try:
            payload = json.loads(output_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise LocalTranscriptionError(
                "The transcriber produced an unreadable JSON transcript."
            ) from exc
        if not isinstance(payload, dict):
            raise LocalTranscriptionError(
                "The transcriber produced an unsupported transcript payload."
            )
        return payload

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
            raise LocalTranscriptionError(
                f"Media processing exceeded the {self.config.timeout_seconds}-second timeout."
            ) from exc
        except asyncio.CancelledError:
            process.kill()
            await process.communicate()
            raise
        if process.returncode != 0:
            detail = stderr.decode("utf-8", errors="replace").strip()
            if len(detail) > 1000:
                detail = detail[-1000:]
            raise LocalTranscriptionError(
                f"Media tool failed with exit code {process.returncode}: {detail or 'no diagnostic output'}"
            )
        return stdout.decode("utf-8", errors="replace").strip()

    @staticmethod
    def _normalize_segments(value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        segments: list[dict[str, Any]] = []
        for item in value:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text") or "").strip()
            start = LocalYouTubeTranscriber._as_float(item.get("start"))
            end = LocalYouTubeTranscriber._as_float(item.get("end"))
            if not text or start is None or end is None:
                continue
            segments.append({"start": start, "end": end, "text": text})
        return segments

    @staticmethod
    def _safe_video_metadata(metadata: dict[str, Any], *, url: str) -> dict[str, Any]:
        safe_keys = (
            "id",
            "title",
            "channel",
            "channel_id",
            "uploader",
            "uploader_id",
            "duration",
            "timestamp",
            "release_timestamp",
            "upload_date",
            "webpage_url",
        )
        result = {
            key: metadata[key] for key in safe_keys if metadata.get(key) is not None
        }
        result["requested_url"] = url
        return result

    @staticmethod
    def _as_float(value: Any) -> float | None:
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _as_int(value: Any) -> int | None:
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None
