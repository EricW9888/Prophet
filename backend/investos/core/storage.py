import os
import shutil
from pathlib import Path
from typing import BinaryIO
from uuid import uuid4

from investos.config import settings


class LocalStorage:
    """Simple local filesystem implementation of an object store."""

    def __init__(self, base_dir: str = settings.STORAGE_DIR):
        self.base_dir = Path(base_dir).resolve()
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def _object_path(self, object_path: str) -> Path:
        if not isinstance(object_path, str) or not object_path.strip():
            raise ValueError("Storage object path must be non-empty.")
        candidate = (self.base_dir / object_path).resolve()
        if not candidate.is_relative_to(self.base_dir):
            raise ValueError("Storage object path escapes the storage root.")
        return candidate

    async def put_object(
        self, file_stream: BinaryIO, filename: str, content_type: str
    ) -> str:
        """Saves a file stream to disk and returns the relative storage path."""
        # Use UUID to prevent collisions
        file_ext = Path(filename).suffix
        safe_name = f"{uuid4().hex}{file_ext}"

        # Simple date-based partitioning to avoid massive dirs
        import datetime

        today = datetime.date.today().isoformat()

        target_dir = self.base_dir / today
        target_dir.mkdir(exist_ok=True)

        target_path = target_dir / safe_name

        with target_path.open("wb") as out_file:
            shutil.copyfileobj(file_stream, out_file)

        # Return path relative to base_dir (e.g., "2024-03-25/1234abc.pdf")
        return f"{today}/{safe_name}"

    async def get_object(self, object_path: str) -> bytes:
        """Reads a file from disk into memory."""
        target_path = self._object_path(object_path)
        if not target_path.exists():
            raise FileNotFoundError(f"Storage object not found: {object_path}")

        with target_path.open("rb") as f:
            return f.read()

    async def get_object_path(self, object_path: str) -> Path:
        """Returns the absolute Path to the object for tools that need actual files."""
        target_path = self._object_path(object_path)
        if not target_path.exists():
            raise FileNotFoundError(f"Storage object not found: {object_path}")
        return target_path

    async def object_exists(self, object_path: str | None) -> bool:
        """Checks whether a referenced object currently exists on disk."""
        if not object_path:
            return False
        try:
            return self._object_path(object_path).exists()
        except ValueError:
            return False
