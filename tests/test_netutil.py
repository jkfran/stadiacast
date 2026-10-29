"""stadiacast.netutil: address classification and the upstream fetch guard."""

from __future__ import annotations

import asyncio

import pytest

from stadiacast.netutil import fetch_upstream, is_local_address, local_ip_towards


@pytest.mark.parametrize(
    ("ip", "local"),
    [
        ("127.0.0.1", True),
        ("192.168.2.1", True),
        ("10.1.2.3", True),
        ("172.16.0.9", True),
        ("169.254.1.1", True),
        ("fd00::1", True),
        ("fe80::1", True),
        ("8.8.8.8", False),
        ("142.250.74.100", False),
        ("2607:f8b0::1", False),
        ("not-an-ip", False),
    ],
)
def test_is_local_address(ip, local):
    assert is_local_address(ip) is local


def test_local_ip_towards_loopback():
    assert local_ip_towards("127.0.0.1") == "127.0.0.1"


def test_fetch_upstream_refuses_to_loop_back_to_the_lan():
    pytest.importorskip("aiohttp")
    status, ctype, body, err = asyncio.run(fetch_upstream("https://127.0.0.1/cast/stadiaconfig", "192.168.2.1"))
    assert (status, ctype, body) == (None, None, b"") and "refusing to proxy to myself" in err
