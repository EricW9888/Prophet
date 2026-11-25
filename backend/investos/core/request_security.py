from __future__ import annotations

from ipaddress import IPv6Address, ip_address

SAFE_HTTP_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def is_loopback_host(host: str | None) -> bool:
    if not host:
        return False
    try:
        address = ip_address(host.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(address, IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return address.is_loopback


def is_loopback_host_header(host_header: str | None) -> bool:
    """True when an HTTP Host header names this machine's loopback interface.

    A page on another site can make the browser send requests to 127.0.0.1
    under its own hostname (DNS rebinding). The peer address is then loopback,
    so only the Host header shows that the request was meant for another site.
    """
    if not host_header:
        return False
    host = host_header.strip().lower()
    if host.startswith("["):  # IPv6 literal, e.g. "[::1]:8000"
        host = host[1 : host.find("]")] if "]" in host else ""
    elif host.count(":") == 1:
        host = host.split(":", 1)[0]
    # RFC 6761 reserves "localhost" and its subdomains for loopback.
    if host == "localhost" or host.endswith(".localhost"):
        return True
    return is_loopback_host(host)


def api_request_allowed(
    *,
    method: str,
    client_host: str | None,
    host_header: str | None,
    origin: str | None,
    allowed_origins: set[str] | frozenset[str],
    allow_non_loopback: bool,
) -> bool:
    if not allow_non_loopback and not (
        is_loopback_host(client_host) and is_loopback_host_header(host_header)
    ):
        return False
    if method.upper() in SAFE_HTTP_METHODS or origin is None:
        return True
    return origin.rstrip("/") in allowed_origins
