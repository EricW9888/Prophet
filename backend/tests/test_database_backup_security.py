from __future__ import annotations

import stat
from types import SimpleNamespace

import pytest

from investos.config import settings
from investos.services.database_backup import DatabaseBackupService


def test_database_backup_keeps_password_out_of_process_arguments(monkeypatch, tmp_path):
    password = "-".join(("process", "list", "credential"))
    captured: dict[str, object] = {}

    monkeypatch.setattr(settings, "BACKUP_ENABLED", True)
    monkeypatch.setattr(settings, "BACKUP_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "POSTGRES_PASSWORD", password)
    monkeypatch.setattr(settings, "POSTGRES_SERVER", "127.0.0.1")
    monkeypatch.setattr(settings, "POSTGRES_PORT", 5432)
    monkeypatch.setattr(settings, "POSTGRES_USER", "investos")
    monkeypatch.setattr(settings, "POSTGRES_DB", "investos")
    monkeypatch.setattr(
        "investos.services.database_backup.shutil.which",
        lambda _name: "/usr/bin/pg_dump",
    )

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["env"] = kwargs["env"]
        output_path = command[command.index("--file") + 1]
        with open(output_path, "wb") as handle:
            handle.write(b"backup")
        return SimpleNamespace(returncode=0, stderr="", stdout="")

    monkeypatch.setattr(
        "investos.services.database_backup.subprocess.run",
        fake_run,
    )

    result = DatabaseBackupService.create_backup()

    command = captured["command"]
    assert isinstance(command, list)
    assert password not in command
    assert captured["env"]["PGPASSWORD"] == password
    assert result.created_bytes == 6
    assert result.created_path is not None
    assert stat.S_IMODE(tmp_path.joinpath(result.created_path).stat().st_mode) == 0o600
    assert not list(tmp_path.glob(".*.tmp"))


def test_database_backup_failure_removes_private_temp_file(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "BACKUP_ENABLED", True)
    monkeypatch.setattr(settings, "BACKUP_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "POSTGRES_PASSWORD", "unit-test-placeholder")
    monkeypatch.setattr(
        "investos.services.database_backup.shutil.which",
        lambda _name: "/usr/bin/pg_dump",
    )
    monkeypatch.setattr(
        "investos.services.database_backup.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=1,
            stderr="synthetic failure",
            stdout="",
        ),
    )

    with pytest.raises(RuntimeError, match="synthetic failure"):
        DatabaseBackupService.create_backup()

    assert not list(tmp_path.iterdir())
