from __future__ import annotations

from fastapi import UploadFile


class UploadTooLargeError(ValueError):
    """An uploaded document exceeded its configured byte limit."""


async def read_upload_limited(file: UploadFile, *, max_bytes: int) -> bytes:
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    content = await file.read(max_bytes + 1)
    if len(content) > max_bytes:
        raise UploadTooLargeError(
            "The uploaded document exceeds the configured size limit."
        )
    return content
