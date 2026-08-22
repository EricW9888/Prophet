from pathlib import Path

import pytest

from investos.core.storage import LocalStorage


@pytest.mark.asyncio
async def test_local_storage_reads_objects_inside_root(tmp_path: Path):
    storage = LocalStorage(str(tmp_path))
    target = tmp_path / "2026-08-03" / "evidence.txt"
    target.parent.mkdir()
    target.write_text("source text", encoding="utf-8")

    assert await storage.get_object("2026-08-03/evidence.txt") == b"source text"
    assert await storage.get_object_path("2026-08-03/evidence.txt") == target
    assert await storage.object_exists("2026-08-03/evidence.txt") is True


@pytest.mark.asyncio
async def test_local_storage_rejects_paths_outside_root(tmp_path: Path):
    storage = LocalStorage(str(tmp_path / "storage"))

    with pytest.raises(ValueError, match="escapes the storage root"):
        await storage.get_object("../private.txt")
    with pytest.raises(ValueError, match="escapes the storage root"):
        await storage.get_object_path(str(tmp_path / "private.txt"))
    assert await storage.object_exists("../private.txt") is False
