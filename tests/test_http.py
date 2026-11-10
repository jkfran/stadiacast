"""stadiacast.http_server: framing, keep-alive, chunked bodies and the request limits."""

from __future__ import annotations

import asyncio
import socket
import ssl
import sys
import time

import pytest

from stadiacast import http_server
from stadiacast.certs import create_self_signed
from stadiacast.config import Config
from stadiacast.http_server import MAX_BODY_BYTES, MAX_HEAD_BYTES, HttpRequest, HttpResponse, HttpServer
from stadiacast.log import EventLog


async def _serve(tmp_path, handler, **cfg_overrides):
    settings: dict = {"cert": "unused", "key": "unused", "bind": "127.0.0.1", "https_port": None, "http_ports": (0,)}
    settings.update(cfg_overrides)
    cfg = Config(**settings)
    log = EventLog(str(tmp_path / "http.jsonl"), echo=False)
    srv = HttpServer(cfg, log, handler, clock=lambda: 1_700_000_000.0)
    await srv.start()
    return srv, log


async def _talk(port: int, raw: bytes, read_until_close: bool = True) -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(raw)
    await writer.drain()
    out = await asyncio.wait_for(reader.read() if read_until_close else reader.readuntil(b"\r\n\r\n"), 5)
    writer.close()
    return out


def test_keep_alive_host_parsing_and_absolute_form(tmp_path):
    seen: list[HttpRequest] = []

    async def handler(req: HttpRequest) -> HttpResponse:
        seen.append(req)
        return HttpResponse(200, b"ok:" + req.path.encode(), "text/plain")

    async def run():
        srv, _ = await _serve(tmp_path, handler)
        try:
            raw = (
                b"POST /v1:Register?key=K HTTP/1.1\r\nHost: cloudcast-pa.googleapis.com:443\r\n"
                b"Content-Length: 2\r\n\r\nhi"
                b"GET http://update.googleapis.com/service/update2?x=1 HTTP/1.1\r\nHost: ignored:80\r\n"
                b"Connection: close\r\n\r\n"
            )
            out = await _talk(srv.http_ports[0], raw)
        finally:
            await srv.stop()
        assert out.count(b"HTTP/1.1 200 OK") == 2
        assert b"Date: Tue, 14 Nov 2023 22:13:20 GMT" in out  # the injected clock
        assert b"Connection: keep-alive" in out and b"Connection: close" in out
        first, second = seen
        assert (first.method, first.host, first.path, first.query, first.body) == (
            "POST",
            "cloudcast-pa.googleapis.com",
            "/v1:Register",
            "key=K",
            b"hi",
        )
        assert (second.method, second.host, second.path, second.query) == ("GET", "ignored", "/service/update2", "x=1")
        assert second.wants_close() and not first.wants_close()

    asyncio.run(run())


def test_chunked_body_is_reassembled(tmp_path):
    bodies: list[bytes] = []

    async def handler(req: HttpRequest) -> HttpResponse:
        bodies.append(req.body)
        return HttpResponse(204)

    async def run():
        srv, _ = await _serve(tmp_path, handler)
        try:
            raw = (
                b"POST /log/batch HTTP/1.1\r\nHost: play.googleapis.com\r\nTransfer-Encoding: chunked\r\n"
                b"Connection: close\r\n\r\n"
                b"4;ext=1\r\nabcd\r\n2\r\nef\r\n0\r\nTrailer: x\r\n\r\n"
            )
            out = await _talk(srv.http_ports[0], raw)
        finally:
            await srv.stop()
        assert out.startswith(b"HTTP/1.1 204 No Content") and bodies == [b"abcdef"]

    asyncio.run(run())


CHUNKED = b"POST / HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n"


@pytest.mark.parametrize(
    ("head", "status", "event"),
    [
        (
            b"POST / HTTP/1.1\r\nHost: x\r\nContent-Length: %d\r\n\r\n" % (MAX_BODY_BYTES + 1),
            b"413 Content Too Large",
            "http_body_too_large",
        ),
        (b"POST / HTTP/1.1\r\nHost: x\r\nContent-Length: nope\r\n\r\n", b"400", "http_bad_content_length"),
        (b"POST / HTTP/1.1\r\nHost: x\r\nContent-Length: -5\r\n\r\n", b"400", "http_bad_content_length"),
        (b"GARBAGE\r\n\r\n", b"400 Bad Request", "http_bad_request"),
        (CHUNKED + b"%x\r\n" % (MAX_BODY_BYTES + 1), b"413", "http_body_too_large"),
        (CHUNKED + b"zz\r\n", b"400", "http_bad_chunk"),
        (CHUNKED + b"-5\r\nabcde\r\n", b"400", "http_bad_chunk"),
        (CHUNKED + b"1" * (MAX_HEAD_BYTES + 1) + b"\r\n", b"400", "http_bad_chunk"),
        (CHUNKED + b"0\r\n" + b"t" * (MAX_HEAD_BYTES + 1) + b"\r\n\r\n", b"400", "http_bad_chunk"),
    ],
    ids=[
        "a body beyond the limit",
        "a length that is not a number",
        "a negative length",
        "no request line",
        "a chunk beyond the limit",
        "a chunk size that is not a number",
        "a negative chunk size",
        "a chunk size line longer than a head",
        "a trailer line longer than a head",
    ],
)
def test_bad_requests_are_rejected_and_the_connection_closed(tmp_path, head, status, event):
    calls = 0

    async def handler(_req: HttpRequest) -> HttpResponse:
        nonlocal calls
        calls += 1
        return HttpResponse(200)

    async def run():
        srv, log = await _serve(tmp_path, handler)
        try:
            out = await _talk(srv.http_ports[0], head)
        finally:
            await srv.stop()
        assert out.startswith(b"HTTP/1.1 " + status) and b"Connection: close" in out
        assert calls == 0
        assert log.counts.get(event, 0) == 1 and log.counts.get("http_conn_error", 0) == 0

    asyncio.run(run())


@pytest.mark.parametrize(
    "request_bytes",
    [
        pytest.param(b"POST / HTTP/1.1\r\nHost: x\r\nContent-Length: 10\r\n\r\nabc", id="a body that stops short"),
        pytest.param(CHUNKED + b"3\r\nabc\r\n", id="a chunked body that never ends"),
        pytest.param(CHUNKED + b"0\r\nA: 1\r\nB: 2\r\n", id="trailers that never end"),
    ],
)
def test_a_request_whose_body_does_not_arrive_is_dropped_after_the_idle_timeout(tmp_path, request_bytes):
    """The idle timeout covers the body as well as the head: a client cannot park a connection."""

    async def handler(_req: HttpRequest) -> HttpResponse:
        raise AssertionError("the request never completed")

    async def run():
        srv, log = await _serve(tmp_path, handler, idle_timeout=0.2)
        try:
            started = time.monotonic()
            assert await _talk(srv.http_ports[0], request_bytes) == b""  # closed by the server, nothing sent
            assert 0.15 < time.monotonic() - started < 3
        finally:
            await srv.stop()
        (timeout,) = [r for r in log.records if r["event"] == "http_idle_timeout"]
        assert timeout["stalled"] is True and log.counts.get("http_conn_error", 0) == 0

    asyncio.run(run())


def test_a_response_nobody_reads_is_given_up_after_the_idle_timeout(tmp_path):
    async def handler(_req: HttpRequest) -> HttpResponse:
        return HttpResponse(200, b"x" * (32 * 1024 * 1024))  # far more than the socket buffers take

    async def run():
        srv, log = await _serve(tmp_path, handler, idle_timeout=0.2)
        try:
            _reader, writer = await asyncio.open_connection("127.0.0.1", srv.http_ports[0])
            writer.write(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
            await writer.drain()
            for _ in range(100):  # the client never reads; the server gives up by itself
                if log.counts.get("tcp_close", 0):
                    break
                await asyncio.sleep(0.05)
            assert log.counts.get("tcp_close", 0) == 1
            writer.close()
        finally:
            await srv.stop()
        gave_up = [r for r in log.records if r["event"] == "http_idle_timeout"]
        assert len(gave_up) == 1
        # Windows takes one send into its buffers whatever its size, so the response does not stall
        # there: the connection is given up waiting for a next request instead.
        if sys.platform != "win32":
            assert gave_up[0]["stalled"] is True

    asyncio.run(run())


def test_connections_beyond_the_limit_are_closed_on_arrival(tmp_path, monkeypatch):
    monkeypatch.setattr(http_server, "MAX_CONNECTIONS", 2)

    async def handler(_req: HttpRequest) -> HttpResponse:
        return HttpResponse(200, b"ok")

    async def run():
        srv, log = await _serve(tmp_path, handler)
        port = srv.http_ports[0]
        try:
            held = [await asyncio.open_connection("127.0.0.1", port) for _ in range(2)]
            for _ in range(100):
                if log.counts.get("tcp_accept", 0) == 2:
                    break
                await asyncio.sleep(0.01)
            try:
                refused = await _talk(port, b"GET / HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
            except ConnectionError:  # closed with the request unread: a reset instead of an end of stream
                refused = b""
            assert refused == b"" and log.counts.get("http_too_many_connections", 0) == 1
            held[0][1].close()  # a place comes free
            for _ in range(100):
                if log.counts.get("tcp_close", 0) == 1:
                    break
                await asyncio.sleep(0.01)
            out = await _talk(port, b"GET / HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
            assert out.endswith(b"ok")
            held[1][1].close()
        finally:
            await srv.stop()

    asyncio.run(run())


def test_the_status_line_names_the_status(tmp_path):
    async def handler(req: HttpRequest) -> HttpResponse:
        return HttpResponse(int(req.path[1:]))

    async def run():
        srv, _ = await _serve(tmp_path, handler)
        try:
            lines = []
            for status in (200, 403, 503, 599):
                request = f"GET /{status} HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n".encode()
                lines.append((await _talk(srv.http_ports[0], request)).split(b"\r\n", 1)[0])
        finally:
            await srv.stop()
        assert lines == [
            b"HTTP/1.1 200 OK",
            b"HTTP/1.1 403 Forbidden",
            b"HTTP/1.1 503 Service Unavailable",
            b"HTTP/1.1 599 Unknown",
        ]

    asyncio.run(run())


def _reject_the_certificate(port: int) -> str | None:
    """A TLS client that verifies the server's certificate, as the controller does, and so refuses a
    self-signed one.  Blocking: it sends its alert before it closes, which asyncio's client does not."""
    try:
        with socket.create_connection(("127.0.0.1", port)) as sock:
            ssl.create_default_context().wrap_socket(sock, server_hostname="cloudcast-pa.googleapis.com")
    except ssl.SSLError as e:
        return e.reason
    return None


def test_a_client_that_refuses_the_certificate_is_logged_with_its_alert(tmp_path):
    async def run():
        create_self_signed(str(tmp_path / "lab.crt"), str(tmp_path / "lab.key"), days=1)
        srv, log = await _serve(
            tmp_path,
            lambda _req: HttpResponse(200),
            cert=str(tmp_path / "lab.crt"),
            key=str(tmp_path / "lab.key"),
            https_port=0,
        )
        try:
            assert await asyncio.to_thread(_reject_the_certificate, srv.https_port) == "CERTIFICATE_VERIFY_FAILED"
            for _ in range(100):
                if log.counts.get("tls_alert", 0):
                    break
                await asyncio.sleep(0.01)
        finally:
            await srv.stop()
        (alert,) = [r for r in log.records if r["event"] == "tls_alert"]
        assert (alert["alert"], alert["sni"]) == ("UNKNOWN_CA", "cloudcast-pa.googleapis.com")
        assert alert["note"] == "the client does not accept the certificate"
        assert log.counts.get("tcp_accept", 0) == 0  # no connection came of it

    asyncio.run(run())


@pytest.mark.parametrize("what", ["a file that is not a certificate", "a key of another certificate"])
def test_a_certificate_that_cannot_be_used_is_named_in_the_error(tmp_path, what):
    async def run():
        cert, key = str(tmp_path / "lab.crt"), str(tmp_path / "lab.key")
        create_self_signed(cert, key, days=1)
        if what == "a file that is not a certificate":
            (tmp_path / "lab.crt").write_text("not PEM")
        else:
            create_self_signed(str(tmp_path / "other.crt"), key, days=1)
        cfg = Config(cert=cert, key=key, bind="127.0.0.1", https_port=0, http_ports=())
        server = HttpServer(cfg, EventLog(None, echo=False), lambda _req: HttpResponse(200))
        with pytest.raises(OSError, match=r"cannot use the certificate .*lab\.crt with the key .*lab\.key") as error:
            await server.start()
        assert "cannot listen" not in str(error.value) and server.servers == []

    asyncio.run(run())


def test_handler_exceptions_do_not_kill_the_server(tmp_path):
    async def handler(req: HttpRequest) -> HttpResponse:
        if req.path == "/boom":
            raise RuntimeError("boom")
        return HttpResponse(200, b"fine")

    async def run():
        srv, log = await _serve(tmp_path, handler)
        try:
            await _talk(srv.http_ports[0], b"GET /boom HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
            out = await _talk(srv.http_ports[0], b"GET /ok HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
        finally:
            await srv.stop()
        assert out.endswith(b"fine")
        assert any(r["event"] == "http_conn_error" for r in log.records)

    asyncio.run(run())


def test_a_busy_control_port_is_skipped_and_logged(tmp_path):
    async def run():
        blocker = socket.socket()
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        busy = blocker.getsockname()[1]
        try:
            srv, log = await _serve(tmp_path, lambda _req: HttpResponse(200, b"ok"), http_ports=(busy, 0))
            try:
                assert len(srv.http_ports) == 1 and srv.http_ports[0] != busy
                assert log.counts.get("port_busy", 0) == 1
                assert next(r for r in log.records if r["event"] == "port_busy")["port"] == busy
            finally:
                await srv.stop()
            # The HTTPS port is the one the controller needs: busy means the receiver cannot start.
            create_self_signed(str(tmp_path / "lab.crt"), str(tmp_path / "lab.key"), days=1)
            cfg = Config(
                cert=str(tmp_path / "lab.crt"), key=str(tmp_path / "lab.key"), bind="127.0.0.1", https_port=busy
            )
            with pytest.raises(
                OSError, match=rf"cannot listen on 127.0.0.1:{busy} \(https\): TCP port {busy} is taken"
            ):
                await HttpServer(cfg, EventLog(None, echo=False), lambda _req: HttpResponse(200, b"ok")).start()
        finally:
            blocker.close()

    asyncio.run(run())
