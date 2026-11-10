"""The Clearcut analytics upload and the power-off it triggers: --clearcut-block dns / tls and the
connection close after the LogResponse."""

from __future__ import annotations

import asyncio
import socket
import ssl
import struct

import pytest

from stadiacast.config import CLEARCUT_HOSTS, Config
from stadiacast.helpers.dns_sink import Resolver
from stadiacast.http_server import HANDSHAKE_FAILURE_ALERT
from stadiacast.log import EventLog
from tests.helpers import client_hello, fake, http_records, make_receiver, single, wait_for


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


async def _answer_to(port: int, sent: bytes, pieces: int = 1) -> tuple[bytes, str]:
    """What the HTTPS listener sends back to the first bytes of a connection, and how the
    connection stands half a second after its last word: "closed", "reset" or "open"."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    step = max(1, -(-len(sent) // pieces))
    for i in range(0, len(sent), step):
        writer.write(sent[i : i + step])
        await writer.drain()
        if pieces > 1:
            await asyncio.sleep(0.02)
    got, end = bytearray(), "open"
    try:
        while True:
            chunk = await asyncio.wait_for(reader.read(4096), 0.5)
            if not chunk:
                end = "closed"
                break
            got += chunk
    except TimeoutError:
        pass
    except ConnectionResetError:
        end = "reset"
    writer.close()
    return bytes(got), end


def _handshake_error(port: int, name: str) -> str | None:
    """What a blocking TLS client reports when it asks the listener for ``name``."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with socket.create_connection(("127.0.0.1", port)) as sock, ctx.wrap_socket(sock, server_hostname=name):
            return None
    except ssl.SSLError as e:
        return e.reason or str(e)


def test_clearcut_block_tls_answers_the_clearcut_hello_with_a_fatal_alert_and_the_close(tmp_path):
    """--clearcut-block tls on the wire: a ClientHello for play.googleapis.com is answered with one
    TLS record, a fatal handshake_failure alert, and then the close.  The alert is the point: a
    real controller goes on at once after it, and waits about 15 s after a connection that just
    ends.  Every other name is served."""

    async def run():
        rx = make_receiver(tmp_path, clearcut_block="tls")
        await rx.start()
        try:
            port = rx.http.https_port
            refusal = (HANDSHAKE_FAILURE_ALERT, "closed")
            assert await _answer_to(port, client_hello("play.googleapis.com")) == refusal
            assert await _answer_to(port, client_hello("PLAY.GoogleAPIs.com")) == refusal
            assert await _answer_to(port, client_hello("play.googleapis.com", tls13=True)) == refusal
            assert await _answer_to(port, client_hello("play.googleapis.com"), pieces=3) == refusal
            reason = await asyncio.to_thread(_handshake_error, port, "play.googleapis.com")
            assert reason is not None and "ALERT_HANDSHAKE_FAILURE" in reason
            refused = [x for x in rx.log.records if x["event"] == "tls_refused"]
            assert len(refused) == 5
            assert {(x["sni"], x["reason"], x.get("note")) for x in refused} == {
                ("play.googleapis.com", "clearcut-block tls", None)
            }

            flight, end = await _answer_to(port, client_hello("cloudcast-pa.googleapis.com"))
            assert flight[:1] == b"\x16" and end == "open"  # a ServerHello, and the handshake goes on
            assert await asyncio.to_thread(_handshake_error, port, "cloudcast-pa.googleapis.com") is None
        finally:
            await rx.stop()

    asyncio.run(run())


def test_a_clearcut_hello_the_listener_cannot_read_is_refused_by_the_tls_layer(tmp_path):
    """A ClientHello split over two TLS records is one the listener does not put together.  The
    TLS layer does, and its SNI callback refuses the name: the upload fails all the same, though
    asyncio ends such a handshake without sending the alert, and the record says so."""

    async def run():
        rx = make_receiver(tmp_path, clearcut_block="tls")
        await rx.start()
        try:
            hello = client_hello("play.googleapis.com")
            head, body = hello[:3], hello[5:]
            split = b"".join(head + len(part).to_bytes(2, "big") + part for part in (body[:20], body[20:]))
            answer, end = await _answer_to(rx.http.https_port, split)
            assert answer == b"" and end != "open"
            (refused,) = [x for x in rx.log.records if x["event"] == "tls_refused"]
            assert refused["sni"] == "play.googleapis.com" and "without the alert" in refused["note"]
        finally:
            await rx.stop()

    asyncio.run(run())


def test_the_https_listener_gives_up_on_silence_and_leaves_what_is_not_a_hello_to_tls(tmp_path):
    async def run():
        rx = make_receiver(tmp_path, clearcut_block="tls", idle_timeout=0.3)
        await rx.start()
        try:
            port = rx.http.https_port
            reader, writer = await asyncio.open_connection("127.0.0.1", port)  # says nothing
            assert await asyncio.wait_for(reader.read(), 5) == b""
            writer.close()
            answer, end = await _answer_to(port, b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")  # not TLS
            assert b"HTTP/" not in answer and end != "open"
            answer, end = await _answer_to(port, client_hello("play.googleapis.com")[:40])  # half a hello
            assert answer == b"" and end != "open"
            # none of which stops the listener
            assert await asyncio.to_thread(_handshake_error, port, "cloudcast-pa.googleapis.com") is None
            assert await _answer_to(port, client_hello("play.googleapis.com")) == (HANDSHAKE_FAILURE_ALERT, "closed")
        finally:
            await rx.stop()

    asyncio.run(run())


def test_a_wake_with_the_clearcut_block_on_the_refused_upload_then_the_boot(tmp_path):
    """What a wake against the live preset looks like: the upload refused first, then the
    registration with the offer, GMsg and the link, all through the same listener."""

    async def run():
        rx = make_receiver(tmp_path, clearcut_block="tls")
        await rx.start()
        fc = fake(rx, "pad-a")
        try:
            hello = client_hello("play.googleapis.com")
            assert await _answer_to(rx.http.https_port, hello) == (HANDSHAKE_FAILURE_ALERT, "closed")
            await fc.start_udp()
            st, reg = await fc.register(resume=True)
            assert st == 200 and reg["display"]["display_token"] == single(rx).display_token
            env, bundle = await fc.poll_gmsg()
            assert env.message.id == 1 and bundle.sdp.type == rx.protos.SDP_ANSWER
            assert await fc.poll_gmsg() == (None, None)  # the ack
            fc.connect()
            await fc.wait_connected(10)
            await asyncio.wait_for(fc.display_ready.wait(), 5)
            assert fc.resolve_status == 200
            assert (await fc.keepalive())["code"] == 0
            await wait_for(lambda: single(rx).session["display"]["state"] == "ready")
            served = [r for r in rx.log.records if r["event"] == "http_request"]
            assert len(served) >= 3 and all(r["sni"] for r in served)
        finally:
            fc.close()
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
