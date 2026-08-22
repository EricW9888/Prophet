import httpx
import pytest

from investos.core.url_security import (
    UnsafeUrlError,
    UrlFetchNetworkError,
    fetch_public_text,
    resolve_public_url,
)


async def _public_resolver(_hostname: str, _port: int) -> list[str]:
    return ["93.184.216.34"]


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "http://user:password@example.com/",
        "http://localhost/",
        "http://127.0.0.1/",
        "http://[::1]/",
        "http://169.254.169.254/latest/meta-data/",
        "https://example.com:8443/",
        "https://example.com/path with spaces",
    ],
)
async def test_resolve_public_url_rejects_unsafe_targets(url: str):
    with pytest.raises(UnsafeUrlError):
        await resolve_public_url(url, resolver=_public_resolver)


async def test_resolve_public_url_rejects_mixed_public_and_private_dns():
    async def mixed_resolver(_hostname: str, _port: int) -> list[str]:
        return ["93.184.216.34", "10.0.0.5"]

    with pytest.raises(UnsafeUrlError):
        await resolve_public_url("https://example.com", resolver=mixed_resolver)


async def test_fetch_public_text_pins_dns_and_preserves_host_and_sni():
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "93.184.216.34"
        assert request.headers["host"] == "example.com"
        assert request.extensions["sni_hostname"] == "example.com"
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            text="<html><title>Safe page</title></html>",
        )

    result = await fetch_public_text(
        "https://example.com/article",
        resolver=_public_resolver,
        transport=httpx.MockTransport(handler),
    )

    assert "Safe page" in result


async def test_fetch_public_text_revalidates_redirect_destinations():
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://127.0.0.1/admin"})

    with pytest.raises(UnsafeUrlError):
        await fetch_public_text(
            "https://example.com/start",
            resolver=_public_resolver,
            transport=httpx.MockTransport(handler),
        )


async def test_fetch_public_text_rejects_oversized_streamed_body():
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/plain"},
            content=b"123456",
        )

    with pytest.raises(UrlFetchNetworkError, match="size limit"):
        await fetch_public_text(
            "https://example.com/large",
            max_bytes=5,
            resolver=_public_resolver,
            transport=httpx.MockTransport(handler),
        )


async def test_fetch_public_text_rejects_non_text_documents():
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/octet-stream"},
            content=b"binary",
        )

    with pytest.raises(UrlFetchNetworkError, match="text document"):
        await fetch_public_text(
            "https://example.com/archive",
            resolver=_public_resolver,
            transport=httpx.MockTransport(handler),
        )
