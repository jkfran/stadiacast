"""stadiacast.netutil: address classification, the upstream fetch guard and the bind hint."""

from __future__ import annotations

import asyncio
import errno
import sys

import pytest

from stadiacast.netutil import bind_hint, fetch_upstream, is_local_address, is_loopback, local_ip_towards


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


@pytest.mark.parametrize(
    ("ip", "loopback"),
    [
        ("127.0.0.1", True),
        ("127.8.9.10", True),
        ("::1", True),
        ("::ffff:127.0.0.1", True),
        ("192.168.2.1", False),
        ("::ffff:192.168.2.1", False),
        ("fe80::1", False),
        ("localhost", False),
    ],
)
def test_is_loopback(ip, loopback):
    assert is_loopback(ip) is loopback


def test_local_ip_towards_loopback():
    assert local_ip_towards("127.0.0.1") == "127.0.0.1"


def test_fetch_upstream_refuses_to_loop_back_to_the_lan():
    pytest.importorskip("aiohttp")
    status, ctype, body, err = asyncio.run(fetch_upstream("https://127.0.0.1/cast/stadiaconfig", "192.168.2.1"))
    assert (status, ctype, body) == (None, None, b"") and "refusing to proxy to myself" in err


def test_bind_hint_names_the_usual_holder_of_a_port(monkeypatch):
    in_use = OSError(errno.EADDRINUSE, "Address already in use")
    denied = PermissionError(13, "Permission denied")
    monkeypatch.setattr(sys, "platform", "linux")
    assert "systemd-resolved" in bind_hint(53, "DNS", in_use)
    assert bind_hint(123, "UDP", in_use).startswith("UDP port 123 is taken by another program")
    assert "root or CAP_NET_BIND_SERVICE" in bind_hint(123, "UDP", denied)
    monkeypatch.setattr(sys, "platform", "win32")
    assert "Stop-Service w32time" in bind_hint(123, "UDP", denied)
    assert "holds it exclusively" in bind_hint(53, "DNS", denied)
    assert bind_hint(53, "DNS", in_use).startswith("DNS port 53 is taken by another program")
    assert bind_hint(8008, "TCP", OSError(errno.EINVAL, "weird")).startswith("cannot bind TCP port 8008")
