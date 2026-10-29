"""The receiver: the HTTP front door for every controller, and the registry of their sessions.

The :class:`Receiver` owns what is shared: the HTTPS/HTTP listeners, the helper processes (DNS
sink, SNTP), the clock and the log.  Everything a controller does is routed to its own
:class:`~stadiacast.controller.ControllerSession`, created the first time the controller
identifies itself.  Requests are attributed to a controller by, in order: the GMsg
``subscription`` token (our per-controller display token, echoed by the firmware), the
``controller-cloud-device-id`` / ``controller-device-id`` headers or the ``device_id`` in the
request body, and finally the peer address.

``docs/protocol.md`` describes the protocol itself and what is confirmed versus assumed.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import os
import re
import sys
import time
import traceback
from typing import Any
from urllib.parse import parse_qs

from google.protobuf import json_format
from google.protobuf.message import DecodeError

from stadiacast import SERVER_NAME, __version__
from stadiacast.certs import ensure_certificate, resolve_clock_pin
from stadiacast.config import CLEARCUT_HOSTS, Config
from stadiacast.controller import ControllerSession
from stadiacast.http_server import HttpRequest, HttpResponse, HttpServer
from stadiacast.log import EventLog
from stadiacast.netutil import fetch_upstream, local_ip_towards
from stadiacast.protos import Protos, decode_unknown, printable

#: Sessions kept at once; beyond this the least recently seen controller is forgotten.
MAX_CONTROLLERS = 8

RPC_PATH = re.compile(r"^/v1:([A-Za-z0-9_]+)$")


class Receiver:
    """The LAN-side stand-in for the Stadia cloud."""

    def __init__(self, cfg: Config, log: EventLog | None = None) -> None:
        self.cfg = cfg
        if cfg.log_dir is None:
            cfg.log_dir = os.path.join("captures", time.strftime("%Y%m%d-%H%M%S"))
        os.makedirs(os.path.join(cfg.log_dir, "bodies"), exist_ok=True)
        self.log = log or EventLog(os.path.join(cfg.log_dir, "server.jsonl"), verbose=cfg.verbose, echo=cfg.echo)
        self.protos = Protos(cfg.proto_dir)
        self.http = HttpServer(cfg, self.log, self.handle, self.now)
        self.controllers: dict[str, ControllerSession] = {}  # label -> session, in creation order
        self._by_id: dict[str, ControllerSession] = {}  # device ids and serials
        self._by_ip: dict[str, ControllerSession] = {}  # latest controller seen from an address
        self._by_token: dict[str, ControllerSession] = {}  # display token -> session (GMsg attribution)
        self._free_slots: list[int] = []
        self._next_slot = 0
        self.events: asyncio.Queue[dict[str, Any]] | None = None  # decoded input events of every controller
        self._body_seq = 0
        self._clock_base: float | None = None  # pinned epoch (cfg.clock_pin); None = real time
        self._clock_t0 = time.monotonic()
        self._stats_task: asyncio.Task[None] | None = None
        self._dns_proc: asyncio.subprocess.Process | None = None
        self._sntp_proc: asyncio.subprocess.Process | None = None
        self.started = False

    def now(self) -> float:
        """Wall time as the controllers should see it: real time, or (with ``--clock-pin``) the
        pinned epoch advanced by uptime.  Controllers take their clock from our Date header and
        GetCurrentTime replies."""
        if self._clock_base is None:
            return time.time()
        return self._clock_base + (time.monotonic() - self._clock_t0)

    # ---- lifecycle -----------------------------------------------------------------------
    async def start(self) -> None:
        self.events = asyncio.Queue()
        ensure_certificate(self.cfg, self.log)
        if self.cfg.clock_pin:
            self._clock_base = resolve_clock_pin(self.cfg)
            self.log(
                "clock_pinned",
                source=self.cfg.clock_pin,
                at=datetime.datetime.fromtimestamp(self._clock_base, datetime.UTC).isoformat(),
                note="the controller judges the cert's validity against this clock",
            )
        if not self.cfg.advertise_ip:
            self.cfg.advertise_ip = local_ip_towards(self.cfg.controller_ip) or (
                self.cfg.bind if self.cfg.bind not in ("0.0.0.0", "") else "127.0.0.1"
            )
        await self.http.start()
        if self.cfg.dns_upstream:
            await self._start_dns()
        if self.cfg.sntp:
            await self._start_sntp()
        self.started = True
        self._stats_task = asyncio.get_running_loop().create_task(self._stats_loop())
        self.log(
            "ready",
            version=__version__,
            https_port=self.http.https_port,
            http_ports=self.http.http_ports,
            udp_port=self.cfg.udp_port,
            game_udp_port=self.cfg.game_udp_port,
            udp_ports_per_controller=2,
            advertise_ip=self.cfg.advertise_ip,
            signaling_mode=self.cfg.signaling_mode,
            dtls_role=self.cfg.dtls_role,
            cert=self.cfg.cert,
            preset=self.cfg.preset,
            clearcut_block=self.cfg.clearcut_block,
            rpc_wire_id=f"{self.cfg.rpc_wire_id:#04x}",
            start_game=self.cfg.start_game,
            log_dir=self.cfg.log_dir,
        )

    async def stop(self) -> None:
        if self._stats_task:
            self._stats_task.cancel()
            self._stats_task = None
        for ctl in list(self.controllers.values()):
            ctl.stop()
        await self.http.stop()
        for proc in (self._dns_proc, self._sntp_proc):
            if proc and proc.returncode is None:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), 3)
                except TimeoutError:
                    proc.kill()
        self.log(
            "stopped",
            controllers={
                ctl.label: {
                    "rtc_stats": ctl.endpoint.stats,
                    "game_rtc_stats": ctl.game.endpoint.stats,
                    "input_stats": dict(ctl.input.stats),
                }
                for ctl in self.controllers.values()
            },
        )
        self.log.close()

    async def _spawn_helper(self, module: str, args: list[str], out_name: str) -> asyncio.subprocess.Process:
        """Run a helper (``python -m stadiacast.helpers.<module>``) with its output in the run directory."""
        assert self.cfg.log_dir is not None
        with open(os.path.join(self.cfg.log_dir, out_name), "ab") as out:
            return await asyncio.create_subprocess_exec(
                sys.executable, "-m", f"stadiacast.helpers.{module}", *args, stdout=out, stderr=out
            )

    async def _start_dns(self) -> None:
        assert self.cfg.log_dir is not None and self.cfg.dns_upstream is not None and self.cfg.advertise_ip is not None
        patterns = ["*.googleapis.com", "googleapis.com", *self.cfg.dns_redirect]
        args = [
            "--upstream", self.cfg.dns_upstream, "--redirect-ip", self.cfg.advertise_ip,
            "--controller-ip", self.cfg.controller_ip, "--log", os.path.join(self.cfg.log_dir, "dns.jsonl"),
        ]  # fmt: skip
        args.extend(arg for pat in patterns for arg in ("--redirect", pat))
        nxdomain: list[str] = []
        if self.cfg.clearcut_block == "dns":
            # The first Clearcut upload (batcher tick at ~6 s uptime) is followed by radio silence
            # within ~0.3 s in every wake; a failed upload is requeued with the next tick 60 s out,
            # letting a queued v1:ResolveDiscovery reach the wire.
            nxdomain = list(CLEARCUT_HOSTS)
            args.extend(arg for host in nxdomain for arg in ("--nxdomain", host))
        self._dns_proc = await self._spawn_helper("dns_sink", args, "dns.out")
        self.log(
            "dns_sink_started",
            pid=self._dns_proc.pid,
            upstream=self.cfg.dns_upstream,
            redirect_ip=self.cfg.advertise_ip,
            redirect=patterns,
            nxdomain=nxdomain,
        )

    async def _start_sntp(self) -> None:
        """The firmware's lwIP SNTP client asks time.google.com; the signed RPC headers need a wall
        clock, so answer SNTP ourselves (the DNS sink points the name at us)."""
        assert self.cfg.log_dir is not None
        args = ["--controller-ip", self.cfg.controller_ip, "--log", os.path.join(self.cfg.log_dir, "sntp.jsonl")]
        self._sntp_proc = await self._spawn_helper("sntp_server", args, "sntp.out")
        self.log("sntp_server_started", pid=self._sntp_proc.pid, port=123)

    # ---- the controller registry ---------------------------------------------------------
    def find(self, name: str) -> ControllerSession | None:
        """A controller by label (``c0``), device id, serial, display token or address."""
        return self.controllers.get(name) or self._by_id.get(name) or self._by_token.get(name) or self._by_ip.get(name)

    def single(self) -> ControllerSession:
        """The one known controller (tests and single-controller tooling)."""
        if len(self.controllers) != 1:
            raise LookupError(f"{len(self.controllers)} controllers known: {sorted(self.controllers)}")
        return next(iter(self.controllers.values()))

    async def controller_for(
        self, req: HttpRequest, *, device_id: str | None = None, token: str | None = None, create: bool = True
    ) -> ControllerSession | None:
        """The session a request belongs to, created on first sight when ``create`` is set.

        Identification order: the GMsg token we issued, the ids the firmware sends (the body
        ``device_id`` and the ``controller-cloud-device-id`` / ``controller-device-id`` headers),
        then the peer address.  A request without any id from a new address creates a session
        keyed by that address, which later ids adopt.
        """
        ip = req.peer[0] if req.peer else None
        cloud = req.header("controller-cloud-device-id")
        serial = req.header("controller-device-id")
        ids = [x for x in (device_id, cloud, serial) if x]
        ctl: ControllerSession | None = None
        if token:
            ctl = self._by_token.get(token)
        for ident in ids:
            if ctl is not None:
                break
            ctl = self._by_id.get(ident)
        if ctl is None and ip:
            known = self._by_ip.get(ip)
            if known is not None and (not ids or known.device_id is None):
                ctl = known  # the same address, not identified (yet): the controller we know there
        if ctl is None and create:
            key = ids[0] if ids else (f"ip:{ip}" if ip else None)
            if key is None:
                return None
            ctl = await self._create(key, ip)
        if ctl is not None:
            ctl.touch(ip, device_id=device_id or cloud, serial=serial)
            for ident in ids:
                self._by_id[ident] = ctl
            if ip:
                self._by_ip[ip] = ctl
        return ctl

    async def _create(self, key: str, ip: str | None) -> ControllerSession:
        if len(self.controllers) >= MAX_CONTROLLERS:
            self._evict(min(self.controllers.values(), key=lambda c: c.last_seen))
        slot = self._free_slots.pop(0) if self._free_slots else self._next_slot
        if slot == self._next_slot:
            self._next_slot += 1
        ctl = ControllerSession(self, slot, key, ip)
        # every controller gets its own pair of UDP sockets: base port + 2 * slot (0 = ephemeral)
        udp = self.cfg.udp_port + 2 * slot if self.cfg.udp_port else 0
        game_udp = self.cfg.game_udp_port + 2 * slot if self.cfg.game_udp_port else 0
        await ctl.start(self.cfg.bind, udp, game_udp)
        self.controllers[ctl.label] = ctl
        self._by_token[ctl.display_token] = ctl
        self.log(
            "controller_new",
            controller=ctl.label,
            key=key,
            ip=ip,
            udp_port=ctl.endpoint.local_port,
            game_udp_port=ctl.game.endpoint.local_port,
            display_token=ctl.display_token,
            controllers=len(self.controllers),
        )
        return ctl

    def _evict(self, ctl: ControllerSession) -> None:
        self.log(
            "controller_evicted",
            controller=ctl.label,
            key=ctl.key,
            ip=ctl.ip,
            idle_s=round(time.monotonic() - ctl.last_seen),
        )
        ctl.stop()
        self.controllers.pop(ctl.label, None)
        self._by_token.pop(ctl.display_token, None)
        for table in (self._by_id, self._by_ip):
            for k in [k for k, v in table.items() if v is ctl]:
                del table[k]
        self._free_slots.append(ctl.slot)
        self._free_slots.sort()

    def _pick(self, req: HttpRequest, body: dict[str, Any] | None = None) -> ControllerSession | HttpResponse:
        """The controller a local control request (``/_rumble``, ``/_start_game``) is for.

        ``?controller=<label|device id|ip>`` (or the same key in a JSON body) selects one; with a
        single known controller it may be omitted.
        """
        name = parse_qs(req.query).get("controller", [None])[0] or (body or {}).get("controller")
        if name:
            ctl = self.find(str(name))
            if ctl is None:
                return self._json(404, {"error": f"no controller {name!r}", "controllers": self._controller_index()})
            return ctl
        if len(self.controllers) == 1:
            return next(iter(self.controllers.values()))
        if not self.controllers:
            return self._json(409, {"error": "no controller has connected yet"})
        return self._json(
            400, {"error": "several controllers: pass ?controller=<label>", "controllers": self._controller_index()}
        )

    def _controller_index(self) -> dict[str, dict[str, Any]]:
        return {
            c.label: {"device_id": c.device_id, "ip": c.ip, "display": c.display_state, "game": c.game.state["state"]}
            for c in self.controllers.values()
        }

    @staticmethod
    def _json(status: int, body: dict[str, Any], handler: str = "control") -> HttpResponse:
        return HttpResponse(status, json.dumps(body, default=str).encode(), "application/json", handler=handler)

    # ---- HTTP routing --------------------------------------------------------------------
    async def handle(self, req: HttpRequest) -> HttpResponse:
        """Route one request and write the ``http_request`` record (bodies are dumped to disk)."""
        t0 = time.monotonic()
        body_file = self._dump_body(req)
        try:
            resp = await self._route(req)
        except Exception as e:  # keep serving: the record below says what failed
            self.log("handler_error", conn=req.conn_id, path=req.path, error=repr(e), tb=traceback.format_exc()[-1200:])
            resp = HttpResponse(500, b"", handler="error")
        rec: dict[str, Any] = {
            "conn": req.conn_id,
            "peer": req.peer,
            "sni": req.sni,
            "method": req.method,
            "host": req.host,
            "path": req.path,
            "query": req.query,
            "version": req.version,
            "request_headers": req.headers,
            "body_len": len(req.body),
            "status": resp.status,
            "response_len": len(resp.body),
            "handler": resp.handler,
            "ms": round((time.monotonic() - t0) * 1000, 1),
            "body_file": body_file,
        }
        known = self._by_ip.get(req.peer[0]) if req.peer else None
        if known is not None:
            rec["controller"] = known.label
        if req.body:
            rec["body_hex"] = req.body[:2048].hex()
            text = printable(req.body)
            if text is not None:
                rec["body_text"] = text[:4096]
            else:
                rec["decoded"] = decode_unknown(req.body)
        if resp.body:
            rec["response_hex"] = resp.body[:1024].hex()
        if resp.note:
            rec["note"] = resp.note
        if resp.close:
            rec["connection_close"] = True
        rec.update(resp.extra_log)
        self.log("http_request", **rec)
        return resp

    def _dump_body(self, req: HttpRequest) -> str | None:
        if not req.body:
            return None
        assert self.cfg.log_dir is not None
        self._body_seq += 1
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", f"{req.host}{req.path}")[:80]
        path = os.path.join(self.cfg.log_dir, "bodies", f"{self._body_seq:04d}-{safe}.bin")
        with open(path, "wb") as fh:
            fh.write(req.body)
        return path

    async def _route(self, req: HttpRequest) -> HttpResponse:
        host, path = req.host.lower(), req.path
        if path == "/generate_204" or host.startswith("connectivitycheck."):
            return HttpResponse(204, handler="netcheck")
        if path.startswith("/cast/chromecast/device/stadiaconfig"):
            return await self._stadiaconfig(req)
        if path == "/service/update2":
            return self._omaha(req)
        if path == "/log/batch":
            return self._clearcut(req)
        if path == "/gmsg" or host.startswith("cloudcast-gmsg"):
            return await self._gmsg(req)
        m = RPC_PATH.match(path)
        if m:
            return await self._cloudcast_rpc(req, m.group(1))
        # Anything else: a bare WebRtcSdp / bundle in the body is still answered
        # (legacy fake-controller direction, unexpected envelopes).
        if req.body:
            found = self.protos.sniff_signaling(req.body)
            if found:
                ctl = await self.controller_for(req)
                if ctl is not None:
                    return ctl.handle_signaling(*found)
        if path.startswith("/cr/report"):
            return HttpResponse(200, b"", "text/plain", handler="crash_report")
        if path == "/setup/send_log_report":
            return HttpResponse(200, b'{"status":"ok"}', "application/json", handler="send_log_report")
        if path == "/_status":
            return HttpResponse(
                200, json.dumps(self.status(), indent=1, default=str).encode(), "application/json", handler="status"
            )
        if path in ("/_start_game", "/_rumble"):
            return self._control(req)
        if path in self.cfg.offer_on:
            ctl = await self.controller_for(req)
            if ctl is not None:
                return ctl.serve_offer("offer_on")
        return HttpResponse(404, b"", handler="unhandled", note="no handler for this path; body dumped")

    def _control(self, req: HttpRequest) -> HttpResponse:
        """The local control endpoints, dispatched to the selected controller."""
        body: dict[str, Any] | None = None
        if req.body.strip():
            try:
                parsed = json.loads(req.body)
                body = parsed if isinstance(parsed, dict) else None
            except ValueError:
                body = None  # the game session reports the malformed body itself
        picked = self._pick(req, body)
        if isinstance(picked, HttpResponse):
            return picked
        if req.path == "/_start_game":
            return picked.game.handle_start_game_request(req)
        return picked.game.handle_rumble_request(req)

    # ---- boot-time endpoints -------------------------------------------------------------
    async def _stadiaconfig(self, req: HttpRequest) -> HttpResponse:
        mode = self.cfg.stadiaconfig
        if mode.startswith("file:"):
            with open(mode[5:], "rb") as fh:
                return HttpResponse(200, fh.read(), "application/json", handler="stadiaconfig(file)")
        if mode.startswith("json:"):
            return HttpResponse(200, mode[5:].encode(), "application/json", handler="stadiaconfig(json)")
        upstream = f"https://clients3.google.com{req.path}" + (f"?{req.query}" if req.query else "")
        status, ctype, body, err = await fetch_upstream(upstream, self.cfg.advertise_ip)
        if status:
            return HttpResponse(
                status,
                body,
                ctype or "application/json",
                handler="stadiaconfig(passthrough)",
                extra_log={"upstream": upstream},
            )
        return HttpResponse(
            200,
            b"{}",
            "application/json",
            handler="stadiaconfig(fallback)",
            note=f"upstream fetch failed: {err}",
            extra_log={"upstream": upstream},
        )

    def _omaha(self, req: HttpRequest) -> HttpResponse:
        """Omaha update check: always "no update"."""
        text = req.body.decode("utf-8", "replace")
        apps = re.findall(r'<app\s[^>]*appid="([^"]+)"', text)
        now = datetime.datetime.now(datetime.UTC)
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        elapsed_days = (now.date() - datetime.date(2007, 1, 1)).days
        body = [
            '<?xml version="1.0" encoding="UTF-8"?>',
            f'<response protocol="3.0" server="{SERVER_NAME}">',
            f'<daystart elapsed_seconds="{int((now - midnight).total_seconds())}" elapsed_days="{elapsed_days}"/>',
        ]
        body.extend(
            f'<app appid="{appid}" status="ok"><updatecheck status="noupdate"/><ping status="ok"/></app>'
            for appid in apps or ["{00000000-0000-0000-0000-000000000000}"]
        )
        body.append("</response>")
        return HttpResponse(
            200,
            "".join(body).encode(),
            "text/xml; charset=utf-8",
            handler="omaha",
            extra_log={"omaha_request": text[:2000], "appids": apps},
        )

    def _clearcut(self, req: HttpRequest) -> HttpResponse:
        """Clearcut analytics upload: decode for the log, answer a LogResponse."""
        info: dict[str, Any] = {}
        try:
            batch = self.protos.BatchedLogRequest()
            batch.ParseFromString(req.body)
            info["log_requests"] = len(batch.log_request)
            info["events"] = sum(len(r.log_event) for r in batch.log_request)
            srcs = {
                self.protos.enum_name("chrome.cloudcast.proto.common.LogRequest.LogSource", r.log_source)
                for r in batch.log_request
                if r.HasField("log_source")
            }
            info["log_sources"] = sorted(srcs)
            for r in batch.log_request:
                if r.HasField("client_info"):
                    info["client_info"] = self.protos.to_dict(r.client_info)
                    break
        except DecodeError as e:
            info["decode_error"] = str(e)
        resp = self.protos.LogResponse()
        resp.next_request_wait_millis = 30 * 60 * 1000
        # The firmware's HTTPS requests are serialized and its Clearcut uploader never closes its
        # connection.  When the boot upload went out before v1:ResolveDiscovery, a kept-alive
        # connection here was followed by no ResolveDiscovery at all.
        return HttpResponse(
            200,
            resp.SerializeToString(),
            "application/octet-stream",
            handler="clearcut",
            extra_log={"clearcut": info},
            close=self.cfg.clearcut_close,
        )

    # ---- cloudcast discovery RPCs (schemas/discovery_api.proto) --------------------------
    async def _cloudcast_rpc(self, req: HttpRequest, method: str) -> HttpResponse:
        extra: dict[str, Any] = {
            "rpc": method,
            "api_key": parse_qs(req.query).get("key", [None])[0],
            "controller_device_id": req.header("controller-device-id"),
            "controller_cloud_device_id": req.header("controller-cloud-device-id"),
        }
        if req.method != "POST":
            return HttpResponse(405, handler=f"rpc:{method}", extra_log=extra)
        p = self.protos
        request = None
        if p.rpc_request_class(method) is not None:
            try:
                request = p.parse_rpc_request(method, req.body)
                extra["request"] = p.to_dict(request)
            except DecodeError as e:
                extra["request_decode_error"] = str(e)
                found = p.sniff_signaling(req.body) if req.body else None
                if found:  # not a firmware request body: a bare SDP (legacy fake controller)
                    ctl = await self.controller_for(req)
                    if ctl is not None:
                        return ctl.handle_signaling(*found)
        device_id = getattr(request, "device_id", "") or None
        ctl = await self.controller_for(req, device_id=device_id)
        if ctl is None:
            return HttpResponse(
                400, b"", handler=f"rpc:{method}", note="request without any controller identity", extra_log=extra
            )
        extra["controller"] = ctl.label
        canned = self.cfg.rpc_responses.get(method)
        if canned is not None:
            return HttpResponse(200, canned, "application/x-protobuf", handler=f"rpc:{method}(canned)", extra_log=extra)
        if f"/v1:{method}" in self.cfg.offer_on:
            return ctl.serve_offer(f"rpc:{method}")
        note = None
        resp = None
        if method == "FirmwareVersionCheck":
            resp = p.FirmwareVersionCheckResponse()
            resp.requires_update = False
        elif method == "RegisterController":
            resp, note = ctl.register(request, extra)
        elif method == "DiscoverDisplay":
            resp, note = ctl.discover(request, extra)
        elif method == "ResolveDiscovery":
            note = (
                "ResolveDiscovery: sent on entering S/UNREGISTERING, right after OnConnected registered the "
                "RPC reader; the firmware ignores the body (event 13 UNREGISTERED); empty 200"
            )
            ctl.on_discovery_resolved()
        else:
            note = "unknown RPC; empty message"
        body = b""
        if resp is not None:
            overrides = self.cfg.rpc_fields.get(method)
            if overrides:
                json_format.ParseDict(overrides, resp)
                extra["response_overrides"] = overrides
            body = resp.SerializeToString()
            extra["response"] = p.to_dict(resp)
        if note:
            extra["assumption"] = note
        return HttpResponse(200, body, "application/x-protobuf", handler=f"rpc:{method}", note=note, extra_log=extra)

    # ---- GMsg push channel ---------------------------------------------------------------
    async def _gmsg(self, req: HttpRequest) -> HttpResponse:
        """A ``/gmsg`` poll: find the controller behind it and let its session answer."""
        p = self.protos
        extra: dict[str, Any] = {}
        poll = None
        if req.body:
            try:
                poll = p.GmsgPollRequest()
                poll.ParseFromString(req.body)
                extra["poll"] = p.to_dict(poll)
            except DecodeError as e:
                poll = None
                extra["poll_decode_error"] = str(e)
        canned = self.cfg.rpc_responses.get("gmsg")
        if canned is not None:
            return HttpResponse(200, canned, "application/x-protobuf", handler="gmsg(canned)", extra_log=extra)
        token = poll.subscription if poll is not None and poll.subscription else None
        ctl = await self.controller_for(req, token=token, create=False)
        if ctl is None:
            self.log("gmsg_unattributed", peer=req.peer, token=token)
            return HttpResponse(
                200,
                b"",
                "application/x-protobuf",
                handler="gmsg:unattributed",
                extra_log=extra,
                note="poll from a controller that never registered: empty body (no message)",
            )
        extra["controller"] = ctl.label
        return await ctl.gmsg_poll(poll, extra)

    # ---- events and status ---------------------------------------------------------------
    def publish_event(self, ev: dict[str, Any]) -> None:
        """A decoded input event from one of the controllers (``ev["controller"]`` says which)."""
        if self.events is not None:
            self.events.put_nowait(ev)

    async def wait_event(self, timeout: float = 5.0) -> dict[str, Any]:
        assert self.events is not None
        return await asyncio.wait_for(self.events.get(), timeout)

    async def _stats_loop(self, interval: float = 10.0) -> None:
        """Write each controller's inbound counters to the log whenever they change, so they survive an unclean stop."""
        last: dict[str, Any] = {}
        try:
            while True:
                await asyncio.sleep(interval)
                for ctl in list(self.controllers.values()):
                    snap = (
                        dict(ctl.endpoint.stats),
                        ctl.session["display"]["rpc_in"],
                        dict(ctl.session["display"]["wire_in"]),
                        dict(ctl.game.endpoint.stats),
                        dict(ctl.game.state["wire_in"]),
                    )
                    if snap != last.get(ctl.label):
                        last[ctl.label] = snap
                        ctl.log(
                            "rtc_stats",
                            _quiet=True,
                            rtc_stats=snap[0],
                            rpc_in=snap[1],
                            wire_in=snap[2],
                            display_state=ctl.display_state,
                            game_rtc_stats=snap[3],
                            game_wire_in=snap[4],
                            game_state=ctl.game.state["state"],
                        )
        except asyncio.CancelledError:
            pass

    def status(self) -> dict[str, Any]:
        """The ``/_status`` document."""
        return {
            "version": __version__,
            "controllers": {ctl.label: ctl.status() for ctl in self.controllers.values()},
            "advertise_ip": self.cfg.advertise_ip,
            "event_counts": self.log.counts,
            "log_dir": self.cfg.log_dir,
        }
