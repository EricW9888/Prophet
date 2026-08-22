from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

Resolver = Callable[[str, int], Awaitable[Sequence[str]]]


class UrlFetchError(ValueError):
    """Base error for safe external URL retrieval."""


class UnsafeUrlError(UrlFetchError):
    """The requested URL violates the outbound network policy."""


class UrlFetchNetworkError(UrlFetchError):
    """A policy-compliant URL could not be retrieved safely."""


@dataclass(frozen=True)
class ResolvedUrl:
    original_url: str
    request_url: str
    hostname: str
    host_header: str


async def _system_resolver(hostname: str, port: int) -> Sequence[str]:
    loop = asyncio.get_running_loop()
    try:
        records = await loop.getaddrinfo(
            hostname,
            port,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
        )
    except socket.gaierror as exc:
        raise UrlFetchNetworkError("The URL hostname could not be resolved.") from exc
    return sorted({str(record[4][0]) for record in records})


def _public_ip(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    try:
        address = ipaddress.ip_address(value)
    except ValueError as exc:
        raise UnsafeUrlError("The URL resolved to an invalid network address.") from exc
    if not address.is_global:
        raise UnsafeUrlError(
            "URLs resolving to private, loopback, link-local, or reserved networks "
            "are not allowed."
        )
    return address


def _host_header(hostname: str, port: int, scheme: str) -> str:
    host = f"[{hostname}]" if ":" in hostname else hostname
    default_port = 443 if scheme == "https" else 80
    return host if port == default_port else f"{host}:{port}"


async def resolve_public_url(
    url: str,
    *,
    allowed_ports: set[int] | frozenset[int] = frozenset({80, 443}),
    resolver: Resolver | None = None,
) -> ResolvedUrl:
    if not isinstance(url, str) or not url.strip():
        raise UnsafeUrlError("A non-empty URL is required.")
    candidate = url.strip()
    if len(candidate) > 2048:
        raise UnsafeUrlError("The URL is too long.")
    if "\\" in candidate or any(ord(char) < 32 or char.isspace() for char in candidate):
        raise UnsafeUrlError("The URL contains invalid characters.")

    try:
        parsed = urlsplit(candidate)
        port = parsed.port
    except ValueError as exc:
        raise UnsafeUrlError("The URL is malformed.") from exc

    scheme = parsed.scheme.casefold()
    if scheme not in {"http", "https"}:
        raise UnsafeUrlError("Only HTTP and HTTPS URLs are allowed.")
    if parsed.username is not None or parsed.password is not None:
        raise UnsafeUrlError("URLs containing credentials are not allowed.")
    if not parsed.hostname:
        raise UnsafeUrlError("The URL must include a hostname.")

    try:
        hostname = parsed.hostname.rstrip(".").encode("idna").decode("ascii").casefold()
    except UnicodeError as exc:
        raise UnsafeUrlError("The URL hostname is invalid.") from exc
    if hostname == "localhost" or hostname.endswith(".localhost"):
        raise UnsafeUrlError("Localhost URLs are not allowed.")

    port = port or (443 if scheme == "https" else 80)
    if port not in allowed_ports:
        raise UnsafeUrlError("The URL uses a port that is not allowed.")

    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        addresses = await (resolver or _system_resolver)(hostname, port)
        if not addresses:
            raise UrlFetchNetworkError("The URL hostname did not resolve.")
        public_addresses = [_public_ip(value) for value in addresses]
    else:
        public_addresses = [_public_ip(str(literal))]

    # Prefer IPv4 when both families are available because it is more consistently
    # reachable in local development environments. Every answer was validated first.
    selected = sorted(
        public_addresses,
        key=lambda address: (address.version, address.compressed),
    )[0]
    original_url = urlunsplit(
        (scheme, parsed.netloc, parsed.path or "/", parsed.query, "")
    )
    pinned = httpx.URL(original_url).copy_with(host=selected.compressed)
    return ResolvedUrl(
        original_url=original_url,
        request_url=str(pinned),
        hostname=hostname,
        host_header=_host_header(hostname, port, scheme),
    )


async def fetch_public_text(
    url: str,
    *,
    timeout_seconds: float = 20.0,
    max_redirects: int = 5,
    max_bytes: int = 2 * 1024 * 1024,
    allowed_ports: set[int] | frozenset[int] = frozenset({80, 443}),
    resolver: Resolver | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> str:
    if max_redirects < 0:
        raise ValueError("max_redirects must be non-negative")
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")

    current_url = url
    headers = {
        "Accept": "text/html,application/xhtml+xml,text/plain,application/json,application/xml;q=0.9,*/*;q=0.1",
        "User-Agent": "Mozilla/5.0 (compatible; Prophet/0.1)",
    }
    timeout = httpx.Timeout(timeout_seconds)

    try:
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        ) as client:
            for redirect_count in range(max_redirects + 1):
                target = await resolve_public_url(
                    current_url,
                    allowed_ports=allowed_ports,
                    resolver=resolver,
                )
                request_headers = {**headers, "Host": target.host_header}
                extensions = {"sni_hostname": target.hostname}
                async with client.stream(
                    "GET",
                    target.request_url,
                    headers=request_headers,
                    extensions=extensions,
                ) as response:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            raise UrlFetchNetworkError(
                                "The remote server returned a redirect without a destination."
                            )
                        if redirect_count >= max_redirects:
                            raise UrlFetchNetworkError(
                                "The remote server returned too many redirects."
                            )
                        current_url = urljoin(target.original_url, location)
                        continue

                    response.raise_for_status()
                    content_type = response.headers.get("content-type", "").casefold()
                    allowed_content = (
                        not content_type
                        or content_type.startswith("text/")
                        or "json" in content_type
                        or "xml" in content_type
                    )
                    if not allowed_content:
                        raise UrlFetchNetworkError(
                            "The URL did not return a supported text document."
                        )
                    content_length = response.headers.get("content-length")
                    if content_length:
                        try:
                            declared_length = int(content_length)
                        except ValueError:
                            declared_length = 0
                        if declared_length > max_bytes:
                            raise UrlFetchNetworkError(
                                "The remote document exceeds the configured size limit."
                            )

                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > max_bytes:
                            raise UrlFetchNetworkError(
                                "The remote document exceeds the configured size limit."
                            )
                    encoding = response.encoding or "utf-8"
                    try:
                        return bytes(body).decode(encoding, errors="replace")
                    except LookupError:
                        return bytes(body).decode("utf-8", errors="replace")
    except UnsafeUrlError:
        raise
    except UrlFetchNetworkError:
        raise
    except httpx.HTTPStatusError as exc:
        raise UrlFetchNetworkError(
            f"The remote server returned HTTP {exc.response.status_code}."
        ) from exc
    except httpx.RequestError as exc:
        raise UrlFetchNetworkError(
            "The remote document could not be retrieved."
        ) from exc

    raise UrlFetchNetworkError("The remote document could not be retrieved.")
