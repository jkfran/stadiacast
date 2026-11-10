"""A small HTTP/1.1 server shaped for the controller's firmware client.

The firmware speaks HTTP/1.1 over TLS with ``Host: name:443``, protobuf bodies, keep-alive
connections and occasionally chunked uploads.  Every request is handed to one async handler
(the receiver's router) and logged by it; this module only does framing.  It also owns the TLS
context, where the SNI callback records the requested name per connection, and the HTTPS
listener, which reads each ClientHello before the TLS layer does: with ``--clearcut-block tls``
the one for the Clearcut host is answered with a TLS alert (see ``HelloServer``).

Every listener is open to the whole network, so nothing a client does may hold the server: a
request has ``idle_timeout`` seconds for its head and as long again for its body, a response as
long to be taken, and the connections open at once are counted.
"""

from __future__ import annotations

import asyncio
import contextlib
import email.utils
import http
import re
import socket
import ssl
import time
import traceback
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from stadiacast import SERVER_NAME, __version__
from stadiacast.config import CLEARCUT_HOSTS, Config
from stadiacast.log import EventLog
from stadiacast.netutil import bind_hint

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
#: Connections served at once; a controller holds one or two.  Further ones are closed on
#: arrival, which keeps a flood of them from using up the process's file descriptors.
MAX_CONNECTIONS = 128
#: The TLS record a refused ClientHello is answered with: a fatal handshake_failure alert.
HANDSHAKE_FAILURE_ALERT = bytes((21, 3, 3, 0, 2, 2, 40))
#: The longest TLS record: 5 bytes of header and 2^14 of content.
MAX_TLS_RECORD_BYTES = 5 + 16384
#: The alerts a TLS client sends when it does not accept the server's certificate.
CERTIFICATE_ALERTS = frozenset(
    {
        "BAD_CERTIFICATE",
        "UNSUPPORTED_CERTIFICATE",
        "CERTIFICATE_REVOKED",
        "CERTIFICATE_EXPIRED",
        "CERTIFICATE_UNKNOWN",
        "UNKNOWN_CA",
    }
)


def client_hello_sni(data: bytes) -> str | None:
    """The server name a TLS ClientHello asks for, lower-cased, read from the first record of a
    connection; None when ``data`` is not a ClientHello or names no server."""
    try:
        if data[0] != 22 or data[5] != 1:  # a handshake record that starts with a ClientHello
            return None
        hello = data[9 : 5 + int.from_bytes(data[3:5], "big")]  # past the record and handshake headers
        i = 2 + 32  # client_version, random
        i += 1 + hello[i]  # session_id
        i += 2 + int.from_bytes(hello[i : i + 2], "big")  # cipher_suites
        i += 1 + hello[i]  # compression_methods
        end = min(len(hello), i + 2 + int.from_bytes(hello[i : i + 2], "big"))
        i += 2
        while i + 4 <= end:
            kind = int.from_bytes(hello[i : i + 2], "big")
            body = hello[i + 4 : i + 4 + int.from_bytes(hello[i + 2 : i + 4], "big")]
            if kind == 0:  # server_name: a list length, then one entry of type 0 (host name) and its length
                name = body[5 : 5 + int.from_bytes(body[3:5], "big")]
                if len(body) < 5 or body[2] != 0 or len(name) != int.from_bytes(body[3:5], "big"):
                    return None
                return name.decode("ascii").lower() or None
            i += 4 + len(body)
    except (IndexError, UnicodeDecodeError):
        pass
    return None


def _hello_arrived(data: bytes) -> bool:
    """Whether ``data``, the start of a connection, holds all there is to look at: a whole first
    TLS record, or bytes that are not one."""
    if not data:
        return False
    if data[0] != 22:
        return True
    if len(data) < 5:
        return False
    whole = 5 + int.from_bytes(data[3:5], "big")
    return whole > MAX_TLS_RECORD_BYTES or len(data) >= whole


async def _peek_hello(conn: socket.socket) -> bytes:
    """What a new connection sends first, left in the socket's buffer for whoever reads it next.

    Polls: no event loop offers a wait for readable data that leaves the data alone on every
    platform, and a ClientHello follows its connection within milliseconds.
    """
    delay = 0.001
    while True:
        try:
            data = conn.recv(MAX_TLS_RECORD_BYTES, socket.MSG_PEEK)
        except (BlockingIOError, InterruptedError):
            data = None
        if data is not None:
            if not data:
                raise ConnectionResetError("closed before a ClientHello")
            if _hello_arrived(data):
                return data
        await asyncio.sleep(delay)
        delay = min(delay * 2, 0.05)


def _listening_sockets(host: str, port: int) -> list[socket.socket]:
    """Listening sockets for ``host``, set up the way ``asyncio.start_server`` sets up its own."""
    socks: list[socket.socket] = []
    try:
        infos = socket.getaddrinfo(host or None, port, type=socket.SOCK_STREAM, flags=socket.AI_PASSIVE)
        for family, kind, proto, _, address in dict.fromkeys(infos):
            sock = socket.socket(family, kind, proto)
            socks.append(sock)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if family == socket.AF_INET6:
                sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            sock.bind(address)
            sock.listen(100)
            sock.setblocking(False)
    except OSError:
        for sock in socks:
            sock.close()
        raise
    return socks


class HelloServer:
    """A listener that hands each connection to ``greet`` as a bare socket, its first bytes unread.

    asyncio's own TLS listener closes a handshake that its SNI callback refuses without sending
    the alert the callback asked for, so the peer sees a connection that just ends, and the
    controller's firmware waits about 15 s after one that ends so.  The HTTPS listener therefore
    accepts its connections itself and decides what a ClientHello gets before the TLS layer has
    read anything.  It stands in for an ``asyncio.Server``.
    """

    def __init__(
        self,
        sockets: list[socket.socket],
        greet: Callable[[socket.socket], Coroutine[Any, Any, None]],
        log: EventLog,
    ) -> None:
        self.sockets = sockets
        self._greet = greet
        self._log = log
        self._greeting: set[asyncio.Task[None]] = set()
        self._accepting = [asyncio.create_task(self._accept(sock)) for sock in sockets]

    async def _accept(self, listener: socket.socket) -> None:
        loop = asyncio.get_running_loop()
        try:
            while True:
                try:
                    conn, _ = await loop.sock_accept(listener)
                except OSError as e:  # out of file descriptors, say: wait as asyncio's servers do
                    self._log("tcp_accept_failed", error=str(e))
                    await asyncio.sleep(1)
                    continue
                if len(self._greeting) >= MAX_CONNECTIONS:
                    conn.close()
                    continue
                conn.setblocking(False)
                task = loop.create_task(self._greet(conn))
                self._greeting.add(task)
                task.add_done_callback(self._greeting.discard)
        finally:
            listener.close()  # once the loop no longer waits on it

    def close(self) -> None:
        for task in (*self._accepting, *self._greeting):
            task.cancel()

    async def wait_closed(self) -> None:
        await asyncio.gather(*self._accepting, *self._greeting, return_exceptions=True)


def reason_phrase(status: int) -> str:
    if status in HTTP_REASONS:
        return HTTP_REASONS[status]
    with contextlib.suppress(ValueError):
        return http.HTTPStatus(status).phrase
    return "Unknown"


class BadChunkError(ValueError):
    """A chunked body whose framing cannot be read."""


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
        self.servers: list[asyncio.Server | HelloServer] = []
        self.https_port: int | None = None
        self.http_ports: list[int] = []
        self._conn_seq = 0
        self._open_conns = 0

    # ---- TLS -----------------------------------------------------------------------------
    def make_ssl_context(self) -> ssl.SSLContext:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(self.cfg.cert, self.cfg.key)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.options |= ssl.OP_NO_COMPRESSION
        ctx.sni_callback = self._sni_cb
        # asyncio reports a failed handshake nowhere, so the alert the client sends is read off the
        # ssl module's message callback, a private hook: without it only this record is lost.
        with contextlib.suppress(AttributeError):
            ctx._msg_callback = self._tls_message_cb  # type: ignore[attr-defined]
        return ctx

    def _tls_message_cb(
        self, sslobj: Any, direction: str, _version: Any, content_type: Any, msg_type: Any, _data: bytes
    ) -> None:
        """Log an alert the client sent, other than the one that ends every connection."""
        if direction != "read" or getattr(content_type, "name", None) != "ALERT":
            return
        alert = getattr(msg_type, "name", str(msg_type))
        if alert == "CLOSE_NOTIFY":
            return
        self.log(
            "tls_alert",
            sni=getattr(sslobj, "_stadia_sni", None),
            alert=alert,
            note="the client does not accept the certificate" if alert in CERTIFICATE_ALERTS else None,
        )

    def _sni_cb(self, sslobj: ssl.SSLObject, name: str | None, _ctx: ssl.SSLContext) -> int | None:
        # The stdlib offers no other way to read the SNI from the connection later: stash it on the object.
        try:
            sslobj._stadia_sni = name  # type: ignore[attr-defined]
        except Exception:
            pass
        self.log("tls_client_hello", sni=name)
        if self.cfg.clearcut_block == "tls" and (name or "").lower() in CLEARCUT_HOSTS:
            # The listener refuses this name before a handshake starts (_greet), so this is a
            # ClientHello it could not read the way the TLS layer does.  The upload must fail all
            # the same, but asyncio closes the connection without sending the alert returned here.
            self.log(
                "tls_refused",
                sni=name,
                reason="clearcut-block tls",
                note="refused by the TLS layer: the connection ends without the alert",
            )
            return ssl.ALERT_DESCRIPTION_HANDSHAKE_FAILURE
        return None

    async def _greet(self, conn: socket.socket, ctx: ssl.SSLContext) -> None:
        """One new HTTPS connection.  With ``--clearcut-block tls`` a ClientHello for the Clearcut
        host is answered with a fatal alert and the close, which fails the controller's upload
        within milliseconds.  (The dns mode is not enough: on NXDOMAIN the firmware falls back to
        8.8.8.8 and reaches the real host.)  Every other connection goes to asyncio's TLS with
        its ClientHello still unread."""
        try:
            try:
                hello = await asyncio.wait_for(_peek_hello(conn), timeout=self.cfg.idle_timeout)
            except (TimeoutError, OSError):  # nothing sent in time, or gone already
                conn.close()
                return
            name = client_hello_sni(hello)
            if self.cfg.clearcut_block == "tls" and name in CLEARCUT_HOSTS:
                self.log("tls_client_hello", sni=name)
                self.log("tls_refused", sni=name, reason="clearcut-block tls")
                with contextlib.suppress(OSError):
                    # Read what was looked at and whatever came with it: closing a socket with
                    # unread data resets the connection, and a reset can cost the peer the alert
                    # sent just before it.
                    conn.recv(MAX_TLS_RECORD_BYTES)
                with contextlib.suppress(OSError):
                    conn.send(HANDSHAKE_FAILURE_ALERT)
                conn.close()
                return
            reader = asyncio.StreamReader(limit=MAX_HEAD_BYTES)
            protocol = asyncio.StreamReaderProtocol(reader, lambda r, w: self._on_conn(r, w, True))
        except BaseException:
            conn.close()
            raise
        try:
            # From here the socket is asyncio's, which closes it when the handshake fails.
            await asyncio.get_running_loop().connect_accepted_socket(lambda: protocol, sock=conn, ssl=ctx)
        except Exception:  # a failed handshake: asyncio.start_server swallows those too
            pass

    # ---- lifecycle -----------------------------------------------------------------------
    async def start(self) -> None:
        if self.cfg.https_port is not None:
            try:
                ctx = self.make_ssl_context()
            except OSError as e:  # ssl.SSLError is one: a file that is not PEM, a key that is not the certificate's
                raise OSError(f"cannot use the certificate {self.cfg.cert} with the key {self.cfg.key}: {e}") from e
            # the controller connects to 443 only: without this listener there is no receiver
            srv = await self._listen(self.cfg.https_port, ctx)
            self.https_port = srv.sockets[0].getsockname()[1]
            self.log("listening", proto="https", bind=self.cfg.bind, port=self.https_port)
        for p in self.cfg.http_ports:
            try:
                srv = await self._listen(p, None)
            except OSError as e:
                # 80 and 8008 only carry the local control endpoints: one that cannot be bound must
                # not stop the receiver, since 443 is the port the controller needs.
                self.log("port_busy", proto="http", port=p, error=str(e))
                continue
            port = srv.sockets[0].getsockname()[1]
            self.http_ports.append(port)
            self.log("listening", proto="http", bind=self.cfg.bind, port=port)

    async def _listen(self, port: int, tls: ssl.SSLContext | None) -> asyncio.Server | HelloServer:
        srv: asyncio.Server | HelloServer
        try:
            if tls is not None:
                ctx = tls
                sockets = _listening_sockets(self.cfg.bind, port)
                srv = HelloServer(sockets, lambda conn: self._greet(conn, ctx), self.log)
            else:
                srv = await asyncio.start_server(
                    lambda r, w: self._on_conn(r, w, False),
                    self.cfg.bind,
                    port,
                    limit=MAX_HEAD_BYTES,
                    reuse_address=True,
                )
        except OSError as e:
            proto = "https" if tls else "http"
            raise OSError(f"cannot listen on {self.cfg.bind}:{port} ({proto}): {bind_hint(port, 'TCP', e)}") from e
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

    # ---- connections ---------------------------------------------------------------------
    async def _on_conn(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, tls: bool) -> None:
        self._conn_seq += 1
        conn_id = f"c{self._conn_seq}"
        peer = writer.get_extra_info("peername")
        if self._open_conns >= MAX_CONNECTIONS:
            self.log("http_too_many_connections", conn=conn_id, peer=peer, open=self._open_conns)
            writer.close()
            return
        self._open_conns += 1
        try:
            await self._serve_conn(reader, writer, tls, conn_id, peer)
        finally:
            self._open_conns -= 1

    async def _serve_conn(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        tls: bool,
        conn_id: str,
        peer: tuple[str, int] | None,
    ) -> None:
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
                    self.log("http_bad_request", conn=conn_id, head=head[:200].decode("latin-1"))
                    await self._reject(writer, 400)
                    break
                try:
                    req.body = await asyncio.wait_for(self._read_body(reader, req), timeout=self.cfg.idle_timeout)
                except BadChunkError as e:
                    self.log("http_bad_chunk", conn=conn_id, error=str(e))
                    await self._reject(writer, 400)
                    break
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
                await asyncio.wait_for(self._write(writer, req, resp), timeout=self.cfg.idle_timeout)
                if resp.close or req.wants_close():
                    break
        except (ConnectionResetError, BrokenPipeError, asyncio.IncompleteReadError):
            pass
        except TimeoutError:  # a body that does not arrive, a response that is not taken
            self.log("http_idle_timeout", conn=conn_id, requests=requests, stalled=True)
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
        try:
            while True:
                line = await reader.readuntil(b"\r\n")
                size = int(line.split(b";")[0].strip() or b"0", 16)
                if size == 0:
                    while await reader.readuntil(b"\r\n") != b"\r\n":  # trailers up to the empty line
                        pass
                    return bytes(body)
                if len(body) + size > MAX_BODY_BYTES:
                    raise BodyTooLargeError(len(body) + size)
                body += await reader.readexactly(size)  # ValueError for a negative size
                await reader.readexactly(2)
        except (ValueError, asyncio.LimitOverrunError) as e:  # not a number, or a line longer than a head
            raise BadChunkError(f"chunked framing that cannot be read ({e})") from e

    async def _reject(self, writer: asyncio.StreamWriter, status: int) -> None:
        reason = reason_phrase(status)
        writer.write(f"HTTP/1.1 {status} {reason}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".encode("latin-1"))
        await asyncio.wait_for(writer.drain(), timeout=self.cfg.idle_timeout)

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
        head = (
            f"HTTP/1.1 {resp.status} {reason_phrase(resp.status)}\r\n"
            + "".join(f"{k}: {v}\r\n" for k, v in headers)
            + "\r\n"
        )
        writer.write(head.encode("latin-1") + resp.body)
        await writer.drain()
