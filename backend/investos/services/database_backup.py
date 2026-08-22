from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from investos.config import settings


@dataclass
class DatabaseBackupResult:
    created_path: str | None
    created_bytes: int
    removed_files: list[str]
    remaining_files: list[str]
    total_bytes: int


class DatabaseBackupService:
    FILE_PREFIX = "investos_local_"
    FILE_SUFFIX = ".dump"

    @classmethod
    def backups_dir(cls) -> Path:
        path = Path(settings.BACKUP_DIR)
        path.mkdir(parents=True, exist_ok=True)
        return path

    @classmethod
    def backup_files(cls) -> list[Path]:
        return sorted(
            cls.backups_dir().glob(f"{cls.FILE_PREFIX}*{cls.FILE_SUFFIX}"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )

    @classmethod
    def _timestamped_path(cls) -> Path:
        stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S_%f")
        return cls.backups_dir() / f"{cls.FILE_PREFIX}{stamp}{cls.FILE_SUFFIX}"

    @classmethod
    def _total_bytes(cls, files: list[Path]) -> int:
        return sum(path.stat().st_size for path in files if path.exists())

    @classmethod
    def prune_backups(cls) -> DatabaseBackupResult:
        files = cls.backup_files()
        removed: list[str] = []

        # First keep only the most recent N backups.
        keep_count = max(settings.BACKUP_KEEP_COUNT, 1)
        for path in files[keep_count:]:
            try:
                path.unlink()
                removed.append(path.name)
            except FileNotFoundError:
                continue

        files = cls.backup_files()

        # Then enforce a total-size cap, deleting oldest remaining files first.
        max_total_bytes = max(settings.BACKUP_MAX_TOTAL_MB, 1) * 1024 * 1024
        total_bytes = cls._total_bytes(files)
        while total_bytes > max_total_bytes and len(files) > 1:
            oldest = files[-1]
            try:
                oldest.unlink()
                removed.append(oldest.name)
            except FileNotFoundError:
                pass
            files = cls.backup_files()
            total_bytes = cls._total_bytes(files)

        return DatabaseBackupResult(
            created_path=None,
            created_bytes=0,
            removed_files=removed,
            remaining_files=[path.name for path in files],
            total_bytes=cls._total_bytes(files),
        )

    @classmethod
    def create_backup(cls) -> DatabaseBackupResult:
        if not settings.BACKUP_ENABLED:
            raise RuntimeError("database_backup_disabled")

        pg_dump_bin = shutil.which("pg_dump")
        if not pg_dump_bin:
            raise RuntimeError("pg_dump_not_found")

        output_path = cls._timestamped_path()
        descriptor, temp_name = tempfile.mkstemp(
            dir=output_path.parent,
            prefix=f".{output_path.name}.",
            suffix=".tmp",
        )
        os.close(descriptor)
        temp_path = Path(temp_name)
        env = os.environ.copy()
        env["PGPASSWORD"] = settings.POSTGRES_PASSWORD

        command = [
            pg_dump_bin,
            "--format=custom",
            "--file",
            str(temp_path),
            "--no-owner",
            "--no-privileges",
            "--host",
            settings.POSTGRES_SERVER,
            "--port",
            str(settings.POSTGRES_PORT),
            "--username",
            settings.POSTGRES_USER,
            "--dbname",
            settings.POSTGRES_DB,
        ]
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                env=env,
            )
            if completed.returncode != 0:
                stderr = (
                    completed.stderr.strip()
                    or completed.stdout.strip()
                    or "pg_dump_failed"
                )
                raise RuntimeError(stderr)

            if not temp_path.exists():
                raise RuntimeError("backup_file_missing")

            created_bytes = temp_path.stat().st_size
            if created_bytes <= 0:
                raise RuntimeError("backup_file_empty")

            os.replace(temp_path, output_path)
            output_path.chmod(0o600)
        finally:
            temp_path.unlink(missing_ok=True)

        pruned = cls.prune_backups()
        remaining_files = cls.backup_files()
        return DatabaseBackupResult(
            created_path=str(output_path),
            created_bytes=created_bytes,
            removed_files=pruned.removed_files,
            remaining_files=[path.name for path in remaining_files],
            total_bytes=cls._total_bytes(remaining_files),
        )

    @classmethod
    async def create_backup_async(cls) -> DatabaseBackupResult:
        return await asyncio.to_thread(cls.create_backup)
