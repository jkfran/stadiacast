"""Small network helpers shared by the receiver and its helper processes."""

from __future__ import annotations

import asyncio
import errno
import ipaddress
import socket
import sys
from urllib.parse import urlsplit

from stadiacast import SERVER_NAME


def bind_hint(port: int, protocol: str, error: OSError) -> str:
    """One sentence for the user on a port that could not be bound: who holds it, and what to do.

    Windows refuses a port another program holds exclusively with a permission error, and the
    Windows Time service holds UDP 123 that way while it runs; Linux refuses the ports below 1024
    to anyone but root.
    """
    taken = f"{protocol} port {port} is taken"
    if sys.platform == "win32" and isinstance(error, PermissionError):
        if port == 123:
            return (
                f"{taken}, on Windows normally by the Windows Time service: run Stop-Service w32time in an "
                f"administrator PowerShell (it starts again at the next boot) and start the receiver again ({error})"
            )
        return f"{taken} by another program, which holds it exclusively ({error})"
    if isinstance(error, PermissionError):
        return f"binding {protocol} port {port} needs root or CAP_NET_BIND_SERVICE ({error})"
    if error.errno == errno.EADDRINUSE:
        if port == 53 and sys.platform.startswith("linux"):
            return f"{taken} by another program, often systemd-resolved or dnsmasq ({error})"
        return f"{taken} by another program ({error})"
    return f"cannot bind {protocol} port {port}: {error}"


def local_ip_towards(target: str) -> str | None:
    """This host's IPv4 address on the interface that routes to ``target`` (no packet is sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((target, 9))
        return str(s.getsockname()[0])
    except OSError:
        return None
    finally:
        s.close()


def is_local_address(ip: str) -> bool:
    """True for loopback, link-local and RFC 1918 / ULA addresses (anything that is not the public Internet)."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return addr.is_private or addr.is_loopback or addr.is_link_local


def is_loopback(ip: str) -> bool:
    """True for an address of this computer's loopback interface, also as IPv6 writes an IPv4 one."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    mapped = getattr(addr, "ipv4_mapped", None)
    return bool((mapped or addr).is_loopback)


async def fetch_upstream(
    url: str, own_ip: str | None, timeout: float = 6.0
) -> tuple[int | None, str | None, bytes, str | None]:
    """GET ``url`` from the real host behind a redirected name.

    Used for the ``stadiaconfig`` passthrough.  Returns ``(status, content_type, body, error)``;
    ``status`` is None when nothing was fetched and ``error`` says why.  Refuses to fetch from
    any address that is this host or on the LAN, which would loop the request back to the
    receiver through the DNS redirect.
    """
    try:
        import aiohttp  # optional dependency: stadiacast[passthrough]
    except ImportError:
        return None, None, b"", "aiohttp not installed"
    host = urlsplit(url).hostname or ""
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        addrs = {str(info[4][0]) for info in infos}
    except socket.gaierror as e:
        return None, None, b"", f"dns: {e}"
    if (own_ip and own_ip in addrs) or any(is_local_address(a) for a in addrs):
        return None, None, b"", f"{host} resolves to a local address ({sorted(addrs)}); refusing to proxy to myself"
    try:
        async with (
            aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session,
            session.get(url, headers={"User-Agent": SERVER_NAME}) as r,
        ):
            return r.status, r.headers.get("Content-Type"), await r.read(), None
    except Exception as e:  # network errors of all kinds are reported, not raised
        return None, None, b"", repr(e)
