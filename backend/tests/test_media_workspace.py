from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest

from investos.services import automation as automation_module
from investos.services.automation import AutomationCoordinator, JobTelemetry
from investos.services.media_workspace import (
    MEDIA_WORKSPACE_PREFIX,
    MediaIngestionPolicy,
    media_temp_workspace,
)


def test_media_temp_workspace_removes_scratch_directory(tmp_path):
    policy = MediaIngestionPolicy(
        temp_dir=tmp_path,
        temp_retention_hours=24,
        persist_raw_media=False,
        max_download_mb=512,
    )

    with media_temp_workspace(policy) as workspace:
        assert workspace.exists()
        assert workspace.name.startswith(MEDIA_WORKSPACE_PREFIX)
        (workspace / "audio.tmp").write_text("temporary audio bytes", encoding="utf-8")

    assert not workspace.exists()


def test_media_policy_prunes_only_stale_managed_workspaces(tmp_path):
    policy = MediaIngestionPolicy(
        temp_dir=tmp_path,
        temp_retention_hours=1,
        persist_raw_media=False,
        max_download_mb=512,
    )
    stale = tmp_path / f"{MEDIA_WORKSPACE_PREFIX}old"
    fresh = tmp_path / f"{MEDIA_WORKSPACE_PREFIX}fresh"
    unmanaged = tmp_path / "manual-notes"
    stale.mkdir()
    fresh.mkdir()
    unmanaged.mkdir()
    old_time = (datetime.now(timezone.utc) - timedelta(hours=3)).timestamp()
    os.utime(stale, (old_time, old_time))

    result = policy.cleanup_stale_workspaces()

    assert result == {"scanned": 2, "deleted": 1}
    assert not stale.exists()
    assert fresh.exists()
    assert unmanaged.exists()


def test_media_policy_capabilities_default_to_no_raw_media(tmp_path):
    policy = MediaIngestionPolicy(
        temp_dir=tmp_path,
        temp_retention_hours=24,
        persist_raw_media=False,
        max_download_mb=256,
    )

    rows = {row["key"]: row for row in policy.capability_rows()}

    assert rows["temporary_workspace_cleanup"]["status"] == "available"
    assert rows["raw_media_persistence"]["status"] == "disabled"
    assert "256 MB" in rows["media_download_guardrail"]["detail"]


@pytest.mark.asyncio
async def test_automation_media_cleanup_prunes_stale_workspaces(tmp_path, monkeypatch):
    policy = MediaIngestionPolicy(
        temp_dir=tmp_path,
        temp_retention_hours=1,
        persist_raw_media=False,
        max_download_mb=512,
    )
    stale = tmp_path / f"{MEDIA_WORKSPACE_PREFIX}old"
    stale.mkdir()
    old_time = (datetime.now(timezone.utc) - timedelta(hours=3)).timestamp()
    os.utime(stale, (old_time, old_time))
    logs = []
    monkeypatch.setattr(
        automation_module.MediaIngestionPolicy,
        "from_settings",
        staticmethod(lambda: policy),
    )
    monkeypatch.setattr(
        automation_module.AgentActionLogService,
        "append",
        lambda **kwargs: logs.append(kwargs),
    )
    coordinator = AutomationCoordinator()
    coordinator.telemetry["media_cleanup"] = JobTelemetry(
        "media_cleanup", interval_seconds=21600
    )

    await coordinator._run_media_cleanup()

    assert not stale.exists()
    assert coordinator.telemetry["media_cleanup"].last_status == "ok"
    assert coordinator.telemetry["media_cleanup"].detail == "scanned=1 deleted=1"
    assert logs[0]["action_type"] == "media_cleanup"
    assert logs[0]["metadata"]["persist_raw_media"] is False
