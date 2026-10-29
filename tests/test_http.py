"""stadiacast.http_server: framing, keep-alive, chunked bodies and the request limits."""

from __future__ import annotations

import asyncio

import pytest

from stadiacast.config import Config
from stadiacast.http_server import MAX_BODY_BYTES, HttpRequest, HttpResponse, HttpServer
from stadiacast.log import EventLog


async def _serve(tmp_path, handler, **cfg_overrides):
    cfg = Config(cert="unused", key="unused", bind="127.0.0.1", https_port=None, http_ports=(0,), **cfg_overrides)
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


@pytest.mark.parametrize(
    ("head", "status"),
    [
        (b"POST / HTTP/1.1\r\nHost: x\r\nContent-Length: %d\r\n\r\n" % (MAX_BODY_BYTES + 1), b"413"),
        (b"POST / HTTP/1.1\r\nHost: x\r\nContent-Length: nope\r\n\r\n", b"400"),
        (b"POST / HTTP/1.1\r\nHost: x\r\nContent-Length: -5\r\n\r\n", b"400"),
        (b"GARBAGE\r\n\r\n", b"400"),
        (b"POST / HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n%x\r\n" % (MAX_BODY_BYTES + 1), b"413"),
    ],
)
def test_bad_requests_are_rejected_and_the_connection_closed(tmp_path, head, status):
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
        assert any(r["event"] in ("http_body_too_large", "http_bad_content_length", "tcp_close") for r in log.records)

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
