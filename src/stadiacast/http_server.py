"""A small HTTP/1.1 server shaped for the controller's firmware client.

The firmware speaks HTTP/1.1 over TLS with ``Host: name:443``, protobuf bodies, keep-alive
connections and occasionally chunked uploads.  Every request is handed to one async handler
(the receiver's router) and logged by it; this module only does framing.  It also owns the TLS
context: the SNI callback records the requested name per connection and, with
``--clearcut-block tls``, fails the handshake for the Clearcut host.
"""

from __future__ import annotations

import asyncio
import email.utils
import re
import ssl
import time
import traceback
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from stadiacast import SERVER_NAME, __version__
from stadiacast.config import CLEARCUT_HOSTS, Config
from stadiacast.log import EventLog

HTTP_REASONS = {
    200: "OK",
    204: "No Content",
    400: "Bad Request",
    404: "Not Found",
    405: "Method Not Allowed",
    409: "Conflict",
    413: "Content Too Large",
    500: "Internal Server Error",
    502: "Bad Gateway",
}

#: asyncio StreamReader limit: the longest request head we accept.
MAX_HEAD_BYTES = 64 * 1024
#: Longest request body we accept (the controller's bodies are a few KiB at most).
MAX_BODY_BYTES = 4 * 1024 * 1024


class BodyTooLargeError(Exception):
    def __init__(self, size: int) -> None:
        super().__init__(f"request body of {size} bytes exceeds {MAX_BODY_BYTES}")
        self.size = size


@dataclass
class HttpRequest:
    method: str
    target: str
    version: str
    headers: list[tuple[str, str]]  # in wire order
    body: bytes
    peer: tuple[str, int] | None
    conn_id: str
    tls: dict[str, Any] | None
    sni: str | None
    host: str = ""
    path: str = ""
    query: str = ""

    def header(self, name: str, default: str | None = None) -> str | None:
        name = name.lower()
        for k, v in self.headers:
            if k.lower() == name:
                return v
        return default

    def wants_close(self) -> bool:
        conn = (self.header("connection") or "").lower()
        return conn == "close" or (self.version == "HTTP/1.0" and conn != "keep-alive")


@dataclass
class HttpResponse:
    status: int = 200
    body: bytes = b""
    content_type: str | None = None
    headers: list[tuple[str, str]] = field(default_factory=list)
    handler: str = "?"  # name of the handler that produced it (logged)
    note: str | None = None  # free-text remark for the log (assumptions, fallbacks)
    extra_log: dict[str, Any] = field(default_factory=dict)  # merged into the http_request record
    close: bool = False  # close the connection after this response


Handler = Callable[[HttpRequest], Awaitable[HttpResponse]]


class HttpServer:
    """HTTPS + plaintext listeners feeding one request handler."""

    def __init__(self, cfg: Config, log: EventLog, handler: Handler, clock: Callable[[], float] = time.time) -> None:
        self.cfg = cfg
        self.log = log
        self.handler = handler
        self.clock = clock  # wall time for the Date header (the receiver may pin it)
        self.servers: list[asyncio.Server] = []
        self.https_port: int | None = None
        self.http_ports: list[int] = []
        self._conn_seq = 0

    # ---- TLS -----------------------------------------------------------------------------
    def make_ssl_context(self) -> ssl.SSLContext:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(self.cfg.cert, self.cfg.key)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.options |= ssl.OP_NO_COMPRESSION
        ctx.sni_callback = self._sni_cb
        return ctx

    def _sni_cb(self, sslobj: ssl.SSLObject, name: str | None, _ctx: ssl.SSLContext) -> int | None:
        # The stdlib offers no other way to read the SNI from the connection later: stash it on the object.
        try:
            sslobj._stadia_sni = name  # type: ignore[attr-defined]
        except Exception:
            pass
        self.log("tls_client_hello", sni=name)
        if self.cfg.clearcut_block == "tls" and (name or "").lower() in CLEARCUT_HOSTS:
            # Fail the controller's Clearcut upload within milliseconds of its ClientHello.  (The dns
            # mode is not enough: on NXDOMAIN the firmware falls back to 8.8.8.8 and reaches the real host.)
            self.log("tls_refused", sni=name, reason="clearcut-block tls")
            return ssl.ALERT_DESCRIPTION_HANDSHAKE_FAILURE
        return None

    # ---- lifecycle -----------------------------------------------------------------------
    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(self._loop_exception_handler)
        if self.cfg.https_port is not None:
            srv = await self._listen(self.cfg.https_port, tls=True)
            self.https_port = srv.sockets[0].getsockname()[1]
            self.log("listening", proto="https", bind=self.cfg.bind, port=self.https_port)
        for p in self.cfg.http_ports:
            srv = await self._listen(p, tls=False)
            port = srv.sockets[0].getsockname()[1]
            self.http_ports.append(port)
            self.log("listening", proto="http", bind=self.cfg.bind, port=port)

    async def _listen(self, port: int, *, tls: bool) -> asyncio.Server:
        try:
            srv = await asyncio.start_server(
                lambda r, w: self._on_conn(r, w, tls),
                self.cfg.bind,
                port,
                ssl=self.make_ssl_context() if tls else None,
                limit=MAX_HEAD_BYTES,
                reuse_address=True,
            )
        except OSError as e:
            raise OSError(f"cannot listen on {self.cfg.bind}:{port} ({'https' if tls else 'http'}): {e}") from e
        self.servers.append(srv)
        return srv

    async def stop(self) -> None:
        for s in self.servers:
            s.close()
        for s in self.servers:
            try:
                await asyncio.wait_for(s.wait_closed(), 2)
            except Exception:  # best effort on shutdown
                pass
        self.servers.clear()

    def _loop_exception_handler(self, loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        exc = context.get("exception")
        msg = context.get("message", "")
        if isinstance(exc, ssl.SSLError) or "handshake" in msg.lower():
            transport = context.get("transport")
            peer = transport.get_extra_info("peername") if transport else None
            self.log(
                "tls_handshake_failed",
                peer=peer,
                reason=getattr(exc, "reason", None),
                detail=str(exc)[:200],
                message=msg,
                note="controller rejects our certificate" if "UNKNOWN_CA" in str(exc).upper() else None,
            )
            return
        loop.default_exception_handler(context)

    # ---- connections ---------------------------------------------------------------------
    async def _on_conn(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, tls: bool) -> None:
        self._conn_seq += 1
        conn_id = f"c{self._conn_seq}"
        peer = writer.get_extra_info("peername")
        sslobj = writer.get_extra_info("ssl_object") if tls else None
        sni = getattr(sslobj, "_stadia_sni", None) if sslobj else None
        tls_info = {"version": sslobj.version(), "cipher": sslobj.cipher()[0]} if sslobj else None
        self.log("tcp_accept", conn=conn_id, peer=peer, tls=tls_info, sni=sni)
        t0 = time.monotonic()
        requests = 0
        try:
            while True:
                try:
                    head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=self.cfg.idle_timeout)
                except asyncio.IncompleteReadError as e:
                    if e.partial:
                        self.log("http_partial", conn=conn_id, data=e.partial[:200].decode("latin-1"))
                    break
                except TimeoutError:
                    self.log("http_idle_timeout", conn=conn_id, requests=requests)
                    break
                except asyncio.LimitOverrunError:
                    self.log("http_header_too_large", conn=conn_id)
                    break
                req = self._parse_head(head, peer, conn_id, tls_info, sni)
                if req is None:
                    await self._reject(writer, 400)
                    break
                try:
                    req.body = await self._read_body(reader, req)
                except ValueError:
                    self.log("http_bad_content_length", conn=conn_id, value=req.header("content-length"))
                    await self._reject(writer, 400)
                    break
                except BodyTooLargeError as e:
                    self.log("http_body_too_large", conn=conn_id, size=e.size)
                    await self._reject(writer, 413)
                    break
                requests += 1
                resp = await self.handler(req)
                await self._write(writer, req, resp)
                if resp.close or req.wants_close():
                    break
        except (ConnectionResetError, BrokenPipeError, asyncio.IncompleteReadError):
            pass
        except Exception as e:  # keep serving other connections
            self.log("http_conn_error", conn=conn_id, error=repr(e), tb=traceback.format_exc()[-800:])
        finally:
            try:
                writer.close()
            except Exception:
                pass
            self.log("tcp_close", conn=conn_id, requests=requests, ms=round((time.monotonic() - t0) * 1000))

    @staticmethod
    def _parse_head(
        head: bytes, peer: tuple[str, int] | None, conn_id: str, tls_info: dict[str, Any] | None, sni: str | None
    ) -> HttpRequest | None:
        try:
            text = head.decode("latin-1")
            lines = text.split("\r\n")
            method, target, version = lines[0].split(" ", 2)
        except ValueError:
            return None
        headers = []
        for ln in lines[1:]:
            if not ln:
                continue
            k, _, v = ln.partition(":")
            headers.append((k.strip(), v.strip()))
        req = HttpRequest(method.upper(), target, version.strip(), headers, b"", peer, conn_id, tls_info, sni)
        host = req.header("host") or sni or ""
        req.host = host.rsplit(":", 1)[0] if re.search(r":\d+$", host) else host
        if target.startswith(("http://", "https://")):
            parts = urlsplit(target)
            req.host = req.host or parts.hostname or ""
            req.path, req.query = parts.path or "/", parts.query
        else:
            req.path, _, req.query = target.partition("?")
        return req

    @staticmethod
    async def _read_body(reader: asyncio.StreamReader, req: HttpRequest) -> bytes:
        if (req.header("transfer-encoding") or "").lower() == "chunked":
            return await HttpServer._read_chunked(reader)
        clen = req.header("content-length")
        if not clen:
            return b""
        n = int(clen)  # ValueError on garbage
        if n < 0:
            raise ValueError(clen)
        if n > MAX_BODY_BYTES:
            raise BodyTooLargeError(n)
        return await reader.readexactly(n)

    @staticmethod
    async def _read_chunked(reader: asyncio.StreamReader) -> bytes:
        body = bytearray()
        while True:
            line = await reader.readuntil(b"\r\n")
            size = int(line.split(b";")[0].strip() or b"0", 16)
            if size == 0:
                while True:  # trailers up to the empty line
                    trailer = await reader.readuntil(b"\r\n")
                    if trailer == b"\r\n":
                        return bytes(body)
            if len(body) + size > MAX_BODY_BYTES:
                raise BodyTooLargeError(len(body) + size)
            body += await reader.readexactly(size)
            await reader.readexactly(2)

    async def _reject(self, writer: asyncio.StreamWriter, status: int) -> None:
        reason = HTTP_REASONS.get(status, "Error")
        writer.write(f"HTTP/1.1 {status} {reason}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".encode("latin-1"))
        await writer.drain()

    async def _write(self, writer: asyncio.StreamWriter, req: HttpRequest, resp: HttpResponse) -> None:
        close = resp.close or req.wants_close()
        headers = [
            ("Date", email.utils.formatdate(self.clock(), usegmt=True)),
            ("Server", f"{SERVER_NAME}/{__version__}"),
        ]
        if resp.content_type:
            headers.append(("Content-Type", resp.content_type))
        headers += list(resp.headers)
        headers.append(("Content-Length", str(len(resp.body))))
        headers.append(("Connection", "close" if close else "keep-alive"))
        reason = HTTP_REASONS.get(resp.status, "OK")
        head = f"HTTP/1.1 {resp.status} {reason}\r\n" + "".join(f"{k}: {v}\r\n" for k, v in headers) + "\r\n"
        writer.write(head.encode("latin-1") + resp.body)
        await writer.drain()
