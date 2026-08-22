from __future__ import annotations

import pytest

from investos.core.request_security import api_request_allowed, is_loopback_host


@pytest.mark.parametrize(
    "host",
    ["127.0.0.1", "127.12.34.56", "::1", "::ffff:127.0.0.1"],
)
def test_loopback_host_detection_accepts_local_addresses(host):
    assert is_loopback_host(host) is True


@pytest.mark.parametrize("host", [None, "192.0.2.10", "example.com", "0.0.0.0"])
def test_loopback_host_detection_rejects_other_peers(host):
    assert is_loopback_host(host) is False


def test_private_api_rejects_non_loopback_peers_by_default():
    assert (
        api_request_allowed(
            method="GET",
            client_host="192.0.2.10",
            origin=None,
            allowed_origins=frozenset({"http://127.0.0.1:3000"}),
            allow_non_loopback=False,
        )
        is False
    )


def test_private_api_accepts_local_cli_and_allowed_frontend_requests():
    allowed = frozenset({"http://127.0.0.1:3000"})
    assert api_request_allowed(
        method="POST",
        client_host="127.0.0.1",
        origin=None,
        allowed_origins=allowed,
        allow_non_loopback=False,
    )
    assert api_request_allowed(
        method="POST",
        client_host="127.0.0.1",
        origin="http://127.0.0.1:3000",
        allowed_origins=allowed,
        allow_non_loopback=False,
    )


def test_private_api_rejects_cross_origin_local_mutations():
    assert (
        api_request_allowed(
            method="POST",
            client_host="127.0.0.1",
            origin="https://untrusted.example",
            allowed_origins=frozenset({"http://127.0.0.1:3000"}),
            allow_non_loopback=False,
        )
        is False
    )


def test_non_loopback_override_does_not_bypass_origin_check_for_mutations():
    assert (
        api_request_allowed(
            method="POST",
            client_host="192.0.2.10",
            origin="https://untrusted.example",
            allowed_origins=frozenset({"https://prophet.example"}),
            allow_non_loopback=True,
        )
        is False
    )
