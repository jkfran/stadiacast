"""The Clearcut analytics upload and the power-off it triggers: --clearcut-block dns / tls and the
connection close after the LogResponse."""

from __future__ import annotations

import asyncio
import ssl
import struct

import pytest

from stadiacast.config import CLEARCUT_HOSTS, Config
from stadiacast.helpers.dns_sink import Resolver
from stadiacast.log import EventLog
from tests.helpers import http_records, make_receiver


def test_clearcut_block_dns_answers_nxdomain_for_the_clearcut_host(tmp_path):
    """--clearcut-block dns: dns_sink answers NXDOMAIN for play.googleapis.com at once
    (the controller's first Clearcut upload then fails in ms) and still redirects the
    other googleapis names to us."""

    def query(name: str, qtype: int = 1) -> bytes:
        q = b"".join(bytes([len(p)]) + p.encode() for p in name.split(".")) + b"\x00"
        return struct.pack("!HHHHHH", 0x1234, 0x0100, 1, 0, 0, 0) + q + struct.pack("!HH", qtype, 1)

    r = Resolver(
        EventLog(str(tmp_path / "dns.jsonl"), echo=False),
        "127.0.0.1",
        "192.168.2.1",
        ["*.googleapis.com", "googleapis.com"],
        controller_ip="192.168.2.2",
        nxdomain=list(CLEARCUT_HOSTS),
    )
    resp = r.handle(query("play.googleapis.com"), ("192.168.2.2", 5353), "udp")
    assert resp[:2] == b"\x12\x34"
    assert resp[3] & 0x0F == 3
    assert struct.unpack("!H", resp[6:8])[0] == 0  # no answers
    resp = r.handle(query("play.googleapis.com", 28), ("192.168.2.2", 5353), "udp")
    assert resp[3] & 0x0F == 3  # AAAA too
    resp = r.handle(query("cloudcast-pa.googleapis.com"), ("192.168.2.2", 5353), "udp")
    assert resp[3] & 0x0F == 0
    assert struct.unpack("!H", resp[6:8])[0] == 1
    assert resp[-4:] == bytes([192, 168, 2, 1])
    assert Config(cert="c", key="k").clearcut_block == "off"


def test_clearcut_block_tls_aborts_the_clearcut_handshake_only(tmp_path):
    """--clearcut-block tls: a ClientHello with SNI play.googleapis.com gets a fatal
    alert at once; every other host still completes TLS."""

    async def run():
        rx = make_receiver(tmp_path, clearcut_block="tls")
        await rx.start()
        try:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE

            async def post_to_clearcut_host() -> None:
                r, w = await asyncio.open_connection(
                    "127.0.0.1", rx.http.https_port, ssl=ctx, server_hostname="play.googleapis.com"
                )
                w.write(b"POST /log/batch HTTP/1.1\r\nHost: play.googleapis.com\r\nContent-Length: 0\r\n\r\n")
                await w.drain()
                await r.read(1)

            with pytest.raises((ssl.SSLError, ConnectionResetError)):
                await post_to_clearcut_host()
            _, w = await asyncio.open_connection(
                "127.0.0.1", rx.http.https_port, ssl=ctx, server_hostname="cloudcast-pa.googleapis.com"
            )
            w.close()
            refused = [x for x in rx.log.records if x["event"] == "tls_refused"]
            assert [x["sni"] for x in refused] == ["play.googleapis.com"]
        finally:
            await rx.stop()

    asyncio.run(run())


def test_clearcut_connection_closed_after_log_response(tmp_path):
    """The firmware posts Clearcut batches without a Connection header and never
    closes that connection; the receiver closes it after the LogResponse (default)
    or keeps it alive with --clearcut-keepalive."""

    async def post_clearcut(rx) -> tuple:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        reader, writer = await asyncio.open_connection(
            "127.0.0.1", rx.http.https_port, ssl=ctx, server_hostname="play.googleapis.com"
        )
        batch = rx.protos.BatchedLogRequest()
        batch.log_request.add().log_source = 618
        body = batch.SerializeToString()
        writer.write(
            (
                f"POST /log/batch HTTP/1.1\r\nHost: play.googleapis.com\r\n"
                f"Content-Type: application/x-protobuf\r\nContent-Length: {len(body)}\r\n\r\n"
            ).encode()
            + body
        )
        await writer.drain()
        head = (await reader.readuntil(b"\r\n\r\n")).decode("latin-1").lower()
        clen = int(head.split("content-length:")[1].split("\r\n")[0])
        await reader.readexactly(clen)
        try:
            closed = await asyncio.wait_for(reader.read(1), 0.5) == b""
        except TimeoutError:
            closed = False
        writer.close()
        return "connection: close" in head, closed

    async def run():
        for keepalive in (False, True):
            rx = make_receiver(tmp_path, clearcut_close=not keepalive)
            await rx.start()
            try:
                says_close, closed = await post_clearcut(rx)
                assert (says_close, closed) == (not keepalive, not keepalive)
                rec = http_records(rx, "clearcut")[-1]
                assert rec.get("connection_close", False) is (not keepalive)
            finally:
                await rx.stop()

    asyncio.run(run())
