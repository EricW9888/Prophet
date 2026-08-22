import io

import pytest
from fastapi import UploadFile

from investos.core.uploads import UploadTooLargeError, read_upload_limited


async def test_read_upload_limited_accepts_content_at_limit():
    upload = UploadFile(file=io.BytesIO(b"12345"), filename="sample.txt")

    assert await read_upload_limited(upload, max_bytes=5) == b"12345"


async def test_read_upload_limited_rejects_content_over_limit():
    upload = UploadFile(file=io.BytesIO(b"123456"), filename="sample.txt")

    with pytest.raises(UploadTooLargeError, match="size limit"):
        await read_upload_limited(upload, max_bytes=5)
