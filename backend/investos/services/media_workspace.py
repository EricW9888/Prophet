from __future__ import annotations

import shutil
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

from investos.config import settings

MEDIA_WORKSPACE_PREFIX = "prophet-media-"


@dataclass(frozen=True)
class MediaIngestionPolicy:
    """Runtime policy for audio/video connector scratch space and retention."""

    temp_dir: Path
    temp_retention_hours: int
    persist_raw_media: bool
    max_download_mb: int

    @classmethod
    def from_settings(cls) -> "MediaIngestionPolicy":
        default_temp = Path(settings.STORAGE_DIR).parent / "media_tmp"
        return cls(
            temp_dir=Path(settings.MEDIA_TEMP_DIR or default_temp),
            temp_retention_hours=max(int(settings.MEDIA_TEMP_RETENTION_HOURS), 1),
            persist_raw_media=bool(settings.MEDIA_STORE_RAW_MEDIA),
            max_download_mb=max(int(settings.MEDIA_MAX_DOWNLOAD_MB), 1),
        )

    def capability_rows(self) -> list[dict[str, str]]:
        raw_status = "available" if self.persist_raw_media else "disabled"
        raw_detail = (
            "Raw media persistence is enabled; retained media must carry source, capture time, provider, and cleanup metadata."
            if self.persist_raw_media
            else "Raw audio/video persistence is off by default; future media jobs should keep downloads in temporary workspaces and persist transcript/OCR evidence only."
        )
        return [
            {
                "key": "temporary_workspace_cleanup",
                "label": "Temporary media workspace cleanup",
                "status": "available",
                "detail": (
                    "Media jobs should use per-run scratch workspaces that are removed on completion; "
                    f"stale workspaces are eligible for cleanup after {self.temp_retention_hours}h."
                ),
            },
            {
                "key": "raw_media_persistence",
                "label": "Raw audio/video persistence",
                "status": raw_status,
                "detail": raw_detail,
            },
            {
                "key": "media_download_guardrail",
                "label": "Media download guardrail",
                "status": "available",
                "detail": (
                    f"Configured media connectors should cap a single raw media download at {self.max_download_mb} MB unless the user explicitly changes settings."
                ),
            },
        ]

    def cleanup_stale_workspaces(
        self, *, now: datetime | None = None
    ) -> dict[str, int]:
        if not self.temp_dir.exists():
            return {"scanned": 0, "deleted": 0}
        cutoff = (now or datetime.now(timezone.utc)) - timedelta(
            hours=self.temp_retention_hours
        )
        scanned = 0
        deleted = 0
        for child in self.temp_dir.iterdir():
            if not child.name.startswith(MEDIA_WORKSPACE_PREFIX):
                continue
            scanned += 1
            modified = datetime.fromtimestamp(child.stat().st_mtime, timezone.utc)
            if modified >= cutoff:
                continue
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink(missing_ok=True)
            deleted += 1
        return {"scanned": scanned, "deleted": deleted}


@contextmanager
def media_temp_workspace(
    policy: MediaIngestionPolicy | None = None,
) -> Iterator[Path]:
    """Create a per-run media scratch directory and remove it on exit."""

    active_policy = policy or MediaIngestionPolicy.from_settings()
    active_policy.temp_dir.mkdir(parents=True, exist_ok=True)
    workspace = Path(
        tempfile.mkdtemp(prefix=MEDIA_WORKSPACE_PREFIX, dir=active_policy.temp_dir)
    )
    try:
        yield workspace
    finally:
        shutil.rmtree(workspace, ignore_errors=True)
