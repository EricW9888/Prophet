from __future__ import annotations

import asyncio
import json
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote, unquote, urlparse

from investos.config import settings


class YouTubeChannelError(RuntimeError):
    def __init__(self, message: str, *, unavailable: bool = False):
        super().__init__(message)
        self.unavailable = unavailable


@dataclass(frozen=True)
class YouTubeChannelConfig:
    downloader_bin: str
    timeout_seconds: int

    @classmethod
    def from_settings(cls) -> "YouTubeChannelConfig":
        return cls(
            downloader_bin=settings.YOUTUBE_DOWNLOADER_BIN.strip() or "yt-dlp",
            timeout_seconds=max(
                int(settings.YOUTUBE_CHANNEL_ENUMERATION_TIMEOUT_SECONDS), 10
            ),
        )


class YouTubeChannelEnumerator:
    """Bounded, metadata-only discovery for a stored YouTube channel."""

    def __init__(self, config: YouTubeChannelConfig | None = None):
        self.config = config or YouTubeChannelConfig.from_settings()

    def readiness(self) -> dict[str, Any]:
        downloader_path = shutil.which(self.config.downloader_bin)
        return {
            "available": downloader_path is not None,
            "downloader_path": downloader_path,
            "missing": [] if downloader_path else [self.config.downloader_bin],
        }

    async def list_recent(self, *, channel_url: str, limit: int) -> dict[str, Any]:
        canonical_url = self.canonical_channel_videos_url(channel_url)
        readiness = self.readiness()
        if not readiness["available"]:
            raise YouTubeChannelError(
                f"YouTube channel review is not ready; missing: {self.config.downloader_bin}.",
                unavailable=True,
            )

        stdout = await self._run_process(
            str(readiness["downloader_path"]),
            "--ignore-config",
            "--no-remote-components",
            "--flat-playlist",
            "--playlist-end",
            str(limit),
            "--dump-single-json",
            "--no-warnings",
            canonical_url,
        )
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise YouTubeChannelError(
                "The downloader did not return valid channel metadata."
            ) from exc
        if not isinstance(payload, dict):
            raise YouTubeChannelError(
                "The downloader returned an unsupported channel payload."
            )

        videos: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        entries = payload.get("entries")
        if isinstance(entries, list):
            for entry in entries:
                normalized = self._normalize_video(entry)
                if normalized is None or normalized["video_id"] in seen_ids:
                    continue
                seen_ids.add(normalized["video_id"])
                videos.append(normalized)
                if len(videos) >= limit:
                    break

        return {
            "channel_url": canonical_url,
            "channel_id": self._clean_text(
                payload.get("channel_id") or payload.get("uploader_id")
            ),
            "channel_name": self._clean_text(
                payload.get("channel")
                or payload.get("uploader")
                or payload.get("title")
            ),
            "videos": videos,
        }

    @staticmethod
    def canonical_channel_videos_url(value: str) -> str:
        parsed = urlparse((value or "").strip())
        host = (parsed.hostname or "").lower()
        if host == "youtube.com" or host.endswith(".youtube.com"):
            parts = [part for part in parsed.path.split("/") if part]
            if not parts:
                raise YouTubeChannelError(
                    "The source URL is not a YouTube channel URL."
                )
            first = unquote(parts[0])
            if first.startswith("@"):
                identifier_parts = [first]
            elif first.lower() in {"channel", "c", "user"} and len(parts) >= 2:
                identifier_parts = [first.lower(), unquote(parts[1])]
            else:
                raise YouTubeChannelError(
                    "The source URL is not a YouTube channel URL."
                )
            if any(
                not part or "/" in part or part in {".", ".."}
                for part in identifier_parts
            ):
                raise YouTubeChannelError(
                    "The source URL has an invalid channel identifier."
                )
            encoded_path = "/".join(
                quote(part, safe="@._-") for part in identifier_parts
            )
            return f"https://www.youtube.com/{encoded_path}/videos"
        raise YouTubeChannelError("The source URL is not hosted by YouTube.")

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
            raise YouTubeChannelError(
                f"Channel review exceeded the {self.config.timeout_seconds}-second timeout."
            ) from exc
        except asyncio.CancelledError:
            process.kill()
            await process.communicate()
            raise
        if process.returncode != 0:
            detail = stderr.decode("utf-8", errors="replace").strip()
            if len(detail) > 1000:
                detail = detail[-1000:]
            raise YouTubeChannelError(
                f"Channel metadata tool failed with exit code {process.returncode}: "
                f"{detail or 'no diagnostic output'}"
            )
        return stdout.decode("utf-8", errors="replace").strip()

    @classmethod
    def _normalize_video(cls, value: Any) -> dict[str, Any] | None:
        if not isinstance(value, dict):
            return None
        video_id = cls._clean_text(value.get("id"))
        if not video_id or len(video_id) != 11:
            return None
        return {
            "video_id": video_id,
            "title": cls._clean_text(value.get("title")) or f"YouTube video {video_id}",
            "url": f"https://www.youtube.com/watch?v={video_id}",
            "published_at": cls._published_at(value),
            "duration_seconds": cls._as_float(value.get("duration")),
            "view_count": cls._as_int(value.get("view_count")),
            "live_status": cls._clean_text(value.get("live_status")),
            "availability": cls._clean_text(value.get("availability")),
        }

    @staticmethod
    def _published_at(value: dict[str, Any]) -> datetime | None:
        raw_timestamp = value.get("release_timestamp") or value.get("timestamp")
        try:
            if raw_timestamp is not None:
                return datetime.fromtimestamp(float(raw_timestamp), tz=UTC)
        except (OSError, TypeError, ValueError):
            pass
        upload_date = str(value.get("upload_date") or "").strip()
        try:
            return datetime.strptime(upload_date, "%Y%m%d").replace(tzinfo=UTC)
        except ValueError:
            return None

    @staticmethod
    def _clean_text(value: Any) -> str | None:
        text = str(value or "").strip()
        return text or None

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
