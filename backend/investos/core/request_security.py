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


def api_request_allowed(
    *,
    method: str,
    client_host: str | None,
    origin: str | None,
    allowed_origins: set[str] | frozenset[str],
    allow_non_loopback: bool,
) -> bool:
    if not allow_non_loopback and not is_loopback_host(client_host):
        return False
    if method.upper() in SAFE_HTTP_METHODS or origin is None:
        return True
    return origin.rstrip("/") in allowed_origins
