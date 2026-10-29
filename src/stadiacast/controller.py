"""One controller as the receiver sees it: discovery, the GMsg queue, the display link and its game session.

The receiver creates a :class:`ControllerSession` the first time a controller identifies itself
and routes everything that controller does to it.  Each session owns its own UDP sockets (the
display link and the game session), ICE credentials, DTLS certificates, GMsg queue and display
RPC state machine, so controllers never share wire state; the receiver only shares the HTTP
listeners, the helper processes and the log.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

from google.protobuf.message import DecodeError

from stadiacast import SERVER_NAME, signaling
from stadiacast.game_session import GameSession
from stadiacast.gamepad import GamepadState
from stadiacast.http_server import HttpResponse
from stadiacast.input_protocol import InputDecoder
from stadiacast.protos import PKG_RPC, decode_unknown
from stadiacast.rtc import (
    WIRE_ID_RTP,
    WIRE_IDS_DC_RPC,
    DtlsCertificate,
    RtcEndpoint,
    RtpPacket,
    pack_wire_message,
    parse_rtp,
    parse_wire_message,
)

if TYPE_CHECKING:
    from stadiacast.receiver import Receiver

#: Milestones of the post-DTLS phase, in order.  The display state never moves backwards.
DISPLAY_STATES = ("idle", "dtls-connected", "get-controller-state-sent", "controller-state-received", "ready", "input")

#: Placeholder identity of this display in the discovery responses (the firmware only checks presence).
DISPLAY_ID = SERVER_NAME
#: The controller echoes the display token as ``GmsgPollRequest.subscription`` (confirmed live), so a
#: per-controller token attributes GMsg polls.  Protocol maximum: 63 characters.
DISPLAY_TOKEN_PREFIX = f"{SERVER_NAME}-display"
#: Placeholder ``registration_token`` of RegisterControllerResponse (presence is all the firmware checks).
REGISTRATION_TOKEN = SERVER_NAME


def _fresh_display_session() -> dict[str, Any]:
    return {
        "state": "idle",
        "rpc_in": 0,
        "rpc_out": 0,
        "requests": {},
        "notifications": {},
        "get_controller_state_sent": 0,
        "controller_state": None,
        "controller_state_errors": 0,
        "trigger": None,
        "time_replies": 0,
        "keepalive_replies": 0,
        "wire_in": {},
        "input_rpc": 0,
    }


class ControllerSession:
    """Everything the receiver keeps for one controller."""

    def __init__(self, receiver: Receiver, slot: int, key: str, ip: str | None) -> None:
        self.rx = receiver
        self.cfg = receiver.cfg
        self.protos = receiver.protos
        self.slot = slot
        self.label = f"c{slot}"  # short name in logs, /_status and ?controller=
        self.key = key  # the identifier the controller was first seen with (device id, or ip:<addr>)
        self.ip = ip
        self.device_id: str | None = None  # RegisterController / DiscoverDisplay body `device_id` (a UUID)
        self.serial: str | None = None  # the `controller-device-id` header (serial-like)
        self.display_token = f"{DISPLAY_TOKEN_PREFIX}-{slot}"
        self.last_seen = time.monotonic()
        self.log = receiver.log.bind(controller=self.label)
        self.cert = DtlsCertificate()
        self.endpoint = RtcEndpoint(
            cert=self.cert,
            controlling=True,
            dtls_role=self.cfg.dtls_role,
            log=self.log,
            on_rtp=self._on_rtp,
            on_rtcp=self._on_rtcp,
            on_app_data=self._on_app_data,
            on_state=self._on_rtc_state,
            name="input",
            fast_check_interval=self.cfg.ice_fast_checks,
        )
        self.input = InputDecoder(self.protos, on_event=self.emit_input_event)
        self.game = GameSession(self)
        self.session: dict[str, Any] = {
            "state": "idle",
            "role": None,
            "remote": None,
            "offer_served": 0,
            "answers_received": 0,
            "offers_received": 0,
            "input_series": 0,
            "last_ack": None,
            "input_payload_type": None,
            "registered": None,
            "discovery": None,
            "gmsg": {"pending_id": None, "sent": 0, "acked": 0, "polls": 0, "last_ack": None},
            "display": _fresh_display_session(),
            "game": self.game.state,
        }
        self._offer_msg: Any = None
        self._last_answer: Any = None
        self._gmsg_seq = 0
        self._gmsg_pending: dict[str, Any] | None = None
        self._gmsg_event = asyncio.Event()
        self._rtcp_task: asyncio.Task[None] | None = None
        self._display_task: asyncio.Task[None] | None = None
        self._display_fallback_task: asyncio.Task[None] | None = None
        self._dtls_at: float | None = None  # monotonic time of dtls-connected
        self._resolved_at: float | None = None  # monotonic time v1:ResolveDiscovery was answered
        self._rpc_ticket = 0
        self._rpc_pending: dict[int, str] = {}  # tickets of our requests -> purpose
        self._last_input: GamepadState | None = None  # previous ControllerToDisplay #3 report

    def __repr__(self) -> str:
        return f"ControllerSession({self.label}, key={self.key!r}, ip={self.ip!r})"

    @property
    def display_state(self) -> str:
        """The post-DTLS milestone reached (one of :data:`DISPLAY_STATES`)."""
        return str(self.session["display"]["state"])

    def now(self) -> float:
        return self.rx.now()

    def touch(self, ip: str | None, device_id: str | None = None, serial: str | None = None) -> None:
        """Note a sighting: the address it came from and any identifier it carried."""
        self.last_seen = time.monotonic()
        if ip:
            self.ip = ip
        if device_id:
            self.device_id = device_id
        if serial:
            self.serial = serial

    # ---- lifecycle -----------------------------------------------------------------------
    async def start(self, bind: str, udp_port: int, game_udp_port: int) -> None:
        await self.endpoint.start(bind, udp_port)
        await self.game.start(bind, game_udp_port)

    def stop(self) -> None:
        for task in (self._rtcp_task, self._display_task, self._display_fallback_task):
            if task:
                task.cancel()
        self._rtcp_task = self._display_task = self._display_fallback_task = None
        self.game.close()
        self.endpoint.close()

    def reset_boot_state(self) -> None:
        """A new offer from this controller = a new boot: per-boot timestamps and the post-DTLS tasks start over.

        Otherwise a second full boot inherits the first boot's dtls-connected time:
        ``--gmsg-ack-release`` then answers the new ACK poll at once (the controller powers off
        immediately) and the display RPC trigger never fires.
        """
        if self._dtls_at is None and self._resolved_at is None and self._display_task is None:
            return
        for name in ("_rtcp_task", "_display_task", "_display_fallback_task"):
            task = getattr(self, name)
            if task is not None and task is not asyncio.current_task():
                task.cancel()
            setattr(self, name, None)
        self.log(
            "boot_state_reset", previous_dtls=self._dtls_at is not None, previous_resolve=self._resolved_at is not None
        )
        self._dtls_at = None
        self._resolved_at = None
        self._rpc_pending.clear()
        self._rpc_ticket = 0
        self._last_input = None
        self.session["display"] = _fresh_display_session()
        self.game.reset()

    def status(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "key": self.key,
            "device_id": self.device_id,
            "serial": self.serial,
            "ip": self.ip,
            "last_seen_s": round(time.monotonic() - self.last_seen, 1),
            "session": self.session,
            "rtc_stats": self.endpoint.stats,
            "input_stats": dict(self.input.stats),
            "local_ice": self.endpoint.local_params(),
            "udp": [self.cfg.advertise_ip, self.endpoint.local_port],
            **self.game.status(),
        }

    # ---- discovery RPCs ------------------------------------------------------------------
    def register(self, request: Any, extra: dict[str, Any]) -> tuple[Any, str]:
        """``v1:RegisterController``: the response message and the assumption note to log."""
        p = self.protos
        dev = request.device_id if request else ""
        self.session["registered"] = {
            "device_id": dev,
            "attempt_id": request.attempt_id if request else None,
            "cloud_device_id": request.cloud_device_id if request else None,
        }
        resp = p.RegisterControllerResponse()
        resp.registration_id = f"local-{(dev or 'controller')[:40]}"
        resp.poll_interval_ms = 1000
        resp.registration_token = REGISTRATION_TOKEN
        note = "RegisterControllerResponse: placeholder id/token, poll_interval_ms=1000 (firmware requires > 0)"
        if request and request.HasField("discovery"):
            # Autolinking boot (discovery.resume = true + SDP offer): the controller only continues
            # (GMsg poll, ICE) when the response carries the display oneof; without field 5 it logs
            # to Clearcut and powers down.
            resp.display.display_id = DISPLAY_ID
            resp.display.display_token = self.display_token
            note += "; display{id,token} included because the request carries a (resumed) discovery"
            if request.discovery.sdp_offer_bundle:
                extra["discovery_offer"] = self._offer_from_request(request.discovery, "rpc:RegisterController")
        return resp, note

    def discover(self, request: Any, extra: dict[str, Any]) -> tuple[Any, str]:
        """``v1:DiscoverDisplay``: accept the offer in field 4, answer with placeholder display ids."""
        p = self.protos
        resp = p.DiscoverDisplayResponse()
        resp.display_id = DISPLAY_ID
        resp.display_token = self.display_token
        note = "DiscoverDisplayResponse: placeholder display id/token (display_token must be non-empty)"
        if request:
            self.session["discovery"] = {
                "device_id": request.device_id,
                "attempt_id": request.attempt_id,
                "typed_keys": list(request.typed_keys.key),
                "app_id": request.app_id,
                "has_offer": bool(request.sdp_offer_bundle),
            }
            if request.sdp_offer_bundle:
                extra["discovery_offer"] = self._offer_from_request(request, "rpc:DiscoverDisplay")
        return resp, note

    def _offer_from_request(self, disc: Any, via: str) -> dict[str, Any]:
        """``sdp_offer_bundle``: accept the controller's offer and queue our answer bundle for its next GMsg poll."""
        p = self.protos
        try:
            bundle = p.parse_sdp_bundle(disc.sdp_offer_bundle)
        except DecodeError as e:
            return {"error": f"sdp_offer_bundle undecodable: {e}", "hex": disc.sdp_offer_bundle[:64].hex()}
        info: dict[str, Any] = {"bundle": p.to_dict(bundle)}
        if bundle.sdp.type != p.SDP_OFFER:
            info["error"] = f"bundle sdp.type={bundle.sdp.type}, expected SDP_OFFER"
            return info
        if self.cfg.signaling_mode == "offer":
            info["note"] = "signaling-mode=offer: controller offer recorded but not answered"
            return info
        answer_bundle = self._accept_offer(bundle.sdp.sdp_offer, list(bundle.ice_candidate), via)
        info["answer"] = p.to_dict(self._last_answer)
        info["gmsg_id"] = self.queue_gmsg(answer_bundle, "sdp_answer")
        return info

    def _accept_offer(self, offer: Any, candidates: list[Any], via: str) -> bytes:
        """Shared by the RPC path and the bare-signaling path: become the answerer."""
        self.reset_boot_state()
        self.session.update(role="answerer", state="offer-received", remote=self.protos.to_dict(offer))
        self.session["offers_received"] += 1
        self.endpoint.controlling = False
        self.endpoint.set_remote(offer.ice_ufrag, offer.ice_password, offer.fingerprint or None)
        added = [c for c in (self._add_candidate(s) for s in candidates) if c]
        answer = self.build_answer(offer)
        self._last_answer = answer
        self.log("offer_accepted", via=via, rtc_session_id=offer.rtc_session_id, candidates=added)
        return self.bundle(answer)

    def _add_candidate(self, s: Any) -> tuple[str, int] | None:
        if s.type != self.protos.ICE_CANDIDATE:
            return None
        addr = signaling.candidate_address(s)
        if addr is None:
            return None
        ip, port = addr
        if ":" in ip:  # the input socket is IPv4; the firmware also offers link-local + ULA v6 candidates
            self.log("rtc_remote_candidate_skipped", ip=ip, port=port, reason="IPv6 candidate, IPv4 socket")
            return None
        self.endpoint.add_remote_candidate(ip, port)
        return ip, port

    def on_discovery_resolved(self) -> None:
        """``v1:ResolveDiscovery`` arrived.

        The controller sends it on entering S/UNREGISTERING, i.e. after the connection registered
        its RPC read callback; our 200 then leads to ACCEPT_COMMANDS.  A display RPC sent before
        that point never got an answer live, so this is the default trigger.
        """
        first = self._resolved_at is None
        self._resolved_at = time.monotonic()
        if not first:
            return
        since = round(self._resolved_at - self._dtls_at, 3) if self._dtls_at is not None else None
        self.log("discovery_resolved", since_dtls_s=since)
        if self.cfg.display_rpc == "on" and self.cfg.display_rpc_trigger == "resolve" and self._dtls_at is not None:
            self._start_display_rpc("resolve-discovery")

    # ---- GMsg push channel ---------------------------------------------------------------
    def queue_gmsg(self, payload: bytes, kind: str) -> int:
        self._gmsg_seq += 1
        self._gmsg_pending = {"id": self._gmsg_seq, "payload": payload, "kind": kind, "sent": 0}
        self.session["gmsg"]["pending_id"] = self._gmsg_seq
        self._gmsg_event.set()
        return self._gmsg_seq

    async def gmsg_poll(self, poll: Any, extra: dict[str, Any]) -> HttpResponse:
        """One ``/gmsg`` poll from this controller: deliver the pending envelope (re-sent until acked),
        hold the poll while nothing is pending, or answer empty."""
        p = self.protos
        g = self.session["gmsg"]
        g["polls"] += 1
        is_ack = False
        if poll is not None and poll.HasField("last_message") and poll.last_message.HasField("id"):
            is_ack = True
            acked = poll.last_message.id
            g["last_ack"] = acked
            if self._gmsg_pending and self._gmsg_pending["id"] == acked:
                g["acked"] += 1
                g["pending_id"] = None
                self.log("gmsg_acked", id=acked, kind=self._gmsg_pending["kind"])
                self._gmsg_pending = None
        if self._gmsg_pending is None and self.cfg.signaling_mode == "offer" and self.session["offer_served"] == 0:
            # legacy experiment: we offer, inside the GMsg envelope
            self.endpoint.controlling = True
            self.session.update(role="offerer", state="offer-served")
            self.session["offer_served"] += 1
            self.queue_gmsg(self.bundle(self.build_offer()), "sdp_offer")
        if self._gmsg_pending is None and self.cfg.gmsg_hold > 0:
            self._gmsg_event.clear()
            # An ACK poll may be released early: the firmware discards its result, and its serial
            # HTTP client then serves v1:ResolveDiscovery immediately instead of racing the power-off.
            while self._gmsg_pending is None:
                now = time.monotonic()
                end = now + self.cfg.gmsg_hold
                if is_ack and self.cfg.gmsg_ack_release > 0 and self._dtls_at is not None:
                    end = min(end, self._dtls_at + self.cfg.gmsg_ack_release)
                if now >= end:
                    break
                try:
                    await asyncio.wait_for(self._gmsg_event.wait(), end - now)
                except TimeoutError:
                    break
                self._gmsg_event.clear()
        if self._gmsg_pending is None:
            return HttpResponse(
                200,
                b"",
                "application/x-protobuf",
                handler="gmsg:idle",
                extra_log=extra,
                note="nothing pending: empty body = no message (firmware event 10, polls again)",
            )
        pend = self._gmsg_pending
        pend["sent"] += 1
        g["sent"] += 1
        body = p.gmsg_envelope(pend["payload"], pend["id"])
        extra.update(gmsg_id=pend["id"], kind=pend["kind"], resend=pend["sent"] - 1)
        return HttpResponse(
            200,
            body,
            "application/x-protobuf",
            handler=f"gmsg:{pend['kind']}",
            extra_log=extra,
            note="GmsgEnvelope{message{id, type=42, controller_command{payload}}}; re-sent until acked",
        )

    # ---- signaling -----------------------------------------------------------------------
    def build_offer(self) -> Any:
        """Our RtcSdpOffer (legacy offerer mode), built once per controller."""
        if self._offer_msg is None:
            self._offer_msg = signaling.build_offer(self.protos, self.endpoint, self.cfg.offer_overrides)
        return self._offer_msg

    def candidate_message(self, endpoint: RtcEndpoint | None = None) -> Any:
        ep = endpoint or self.endpoint
        return signaling.candidate_message(self.protos, self.cfg.advertise_ip or "", ep.local_port or 0)

    def bundle(self, sdp_msg: Any) -> bytes:
        return signaling.bundle(self.protos, sdp_msg, self.candidate_message())

    def build_answer(self, offer: Any, endpoint: RtcEndpoint | None = None) -> Any:
        return signaling.build_answer(self.protos, offer, endpoint or self.endpoint, boreal=self.cfg.boreal)

    def serve_offer(self, via: str) -> HttpResponse:
        """Legacy: answer a request with our offer bundle (``--offer-on`` paths)."""
        if self.cfg.signaling_mode == "answer":
            return HttpResponse(
                200,
                b"",
                "application/x-protobuf",
                handler=f"{via}:no-offer",
                note="signaling-mode=answer: not offering",
            )
        offer = self.build_offer()
        self.endpoint.controlling = True
        self.session.update(role="offerer", state="offer-served")
        self.session["offer_served"] += 1
        return HttpResponse(
            200,
            self.bundle(offer),
            "application/x-protobuf",
            handler=f"{via}:offer",
            note="served RtcSdpOffer bundle (assumption: envelope + delivery path)",
            extra_log={"offer": self.protos.to_dict(offer)},
        )

    def handle_signaling(self, kind: str, msg: Any) -> HttpResponse:
        """A bare WebRtcSdp or bundle found in a request body (any path)."""
        p = self.protos
        sdps = [msg.sdp, *msg.ice_candidate] if kind == "bundle" else [msg]
        reply: bytes | None = None
        handler = "signaling"
        extra: dict[str, Any] = {"signaling_kind": kind}
        for s in sdps:
            if s.type == p.SDP_OFFER:
                o = s.sdp_offer
                reply = self._accept_offer(o, [], "signaling")
                handler = "signaling:answer"
                extra["offer"] = p.to_dict(o)
                extra["answer"] = p.to_dict(self._last_answer)
            elif s.type == p.SDP_ANSWER:
                a = s.sdp_answer
                self.session.update(role="offerer", state="answer-received", remote=p.to_dict(a))
                self.session["answers_received"] += 1
                self.endpoint.controlling = True
                self.endpoint.set_remote(a.ice_ufrag, a.ice_password, a.fingerprint or None)
                reply = self.candidate_message().SerializeToString()
                handler = "signaling:answer-accepted"
                extra["answer"] = p.to_dict(a)
            elif s.type == p.ICE_CANDIDATE:
                addr = signaling.candidate_address(s)
                if addr:
                    self.endpoint.add_remote_candidate(*addr)
                    extra.setdefault("candidates", []).append(list(addr))
                if reply is None:
                    reply = b""
                    handler = "signaling:candidate"
        return HttpResponse(200, reply or b"", "application/x-protobuf", handler=handler, extra_log=extra)

    # ---- RTC callbacks (display link) ----------------------------------------------------
    def _on_rtc_state(self, state: str, info: dict[str, Any]) -> None:
        self.session["state"] = state
        self.session["rtc"] = info
        if state == "dtls-connected" and self.cfg.rtcp_interval > 0 and self._rtcp_task is None:
            self._rtcp_task = asyncio.get_running_loop().create_task(self._rtcp_loop())
        if state == "dtls-connected" and self._dtls_at is None:
            self._dtls_at = time.monotonic()
            self._gmsg_event.set()  # wake an ACK-poll hold so it can recompute its deadline
            if self.cfg.display_rpc == "on":
                self._set_display_state("dtls-connected")
                if self.cfg.display_rpc_trigger == "dtls" or self._resolved_at is not None:
                    self._start_display_rpc(
                        "dtls-connected" if self.cfg.display_rpc_trigger == "dtls" else "resolve-discovery"
                    )
                elif self.cfg.display_rpc_fallback > 0:
                    self._display_fallback_task = asyncio.get_running_loop().create_task(self._display_rpc_fallback())

    async def _rtcp_loop(self) -> None:
        """Display-side keepalive: an RTCP RR every ``cfg.rtcp_interval`` seconds.

        The real display, as RTP receiver of the controller's mic/input streams, sends RTCP;
        some controller state machines only count the link as live once display traffic arrives.
        """
        interval = self.cfg.rtcp_interval
        try:
            while True:
                await asyncio.sleep(interval)
                if self.endpoint.srtp_out is None:
                    continue
                if self.endpoint.send_rtcp_rr():
                    self.session["rtcp_out"] = self.session.get("rtcp_out", 0) + 1
                    if self.session["rtcp_out"] <= 3:
                        self.log("rtcp_rr_sent", name="input", count=self.session["rtcp_out"])
        except asyncio.CancelledError:
            pass

    def _on_rtp(self, pkt: RtpPacket, via: str = "srtp") -> None:
        """Input RTP, either SRTP on the UDP wire or (raw_dtls) a plain RTP packet on WireHeader2
        sub-channel 0x66; the ack goes back the same way."""
        self.session["input_payload_type"] = pkt.payload_type
        try:
            decoded = self.input.decode(pkt.payload)
        except (DecodeError, ValueError) as e:
            self.log(
                "rtp_payload_undecodable",
                pt=pkt.payload_type,
                seq=pkt.seq,
                len=len(pkt.payload),
                ext=[(i, v.hex()) for i, v in pkt.extensions],
                error=str(e),
                head=pkt.payload[:48].hex(),
            )
            return
        self.session["input_series"] += 1
        if self.session["input_series"] == 1:
            self._set_display_state("input", via=via)
        self.log(
            "rtp_input",
            via=via,
            **pkt.summary(),
            n_events=len(decoded.get("events", [])),
            sequence_number=decoded.get("sequence_number"),
            session_id=decoded.get("session_id"),
            compressed=decoded.get("compressed", False),
            ack=decoded.get("ack_sequence_number"),
        )
        if self.cfg.ack_mode == "rtp" and decoded.get("sequence_number") is not None:
            ack = self.protos.InputProtocolMessage()
            ack.ack_sequence_number = decoded["sequence_number"]
            send = self.endpoint.send_rtp_wire if via == "dtls" else self.endpoint.send_rtp
            if send(ack.SerializeToString(), payload_type=pkt.payload_type):
                self.session["last_ack"] = decoded["sequence_number"]

    def _on_rtcp(self, data: bytes) -> None:
        self.log("rtcp", pt=data[1] if len(data) > 1 else None, len=len(data), head=data[:16].hex())

    def _on_app_data(self, data: bytes) -> None:
        """raw_dtls: every DTLS application-data record is one WireHeader2 message.

        On the display link the controller frames its RpcMessages with id 0x74 (0x75 from the
        other side); 0x66 RTP is kept for the legacy fake-controller path.
        """
        try:
            msg_id, payload = parse_wire_message(data)
        except ValueError as e:
            try:  # not framed: the older guess (a bare InputProtocolMessage), else a dump
                decoded = self.input.decode(data)
                self.log(
                    "dtls_app_input",
                    len=len(data),
                    n_events=len(decoded.get("events", [])),
                    sequence_number=decoded.get("sequence_number"),
                )
            except (DecodeError, ValueError):
                self.log(
                    "dtls_app_data",
                    len=len(data),
                    head=data[:48].hex(),
                    wire_error=str(e),
                    decoded=decode_unknown(data),
                )
            return
        wire_in = self.session["display"]["wire_in"]
        key = f"{msg_id:#06x}"
        wire_in[key] = wire_in.get(key, 0) + 1
        if msg_id == WIRE_ID_RTP:
            if len(payload) > 1 and 192 <= payload[1] <= 223:  # RFC 5761 RTCP range
                self._on_rtcp(payload)
                return
            try:
                pkt = parse_rtp(payload)
            except ValueError as e:
                self.log("wire_rtp_unparsable", len=len(payload), error=str(e), head=payload[:48].hex())
                return
            self.endpoint.stats["rtp_in"] += 1
            self._on_rtp(pkt, via="dtls")
        elif msg_id in WIRE_IDS_DC_RPC or msg_id == self.cfg.rpc_wire_id:
            self._on_rpc(payload)
        else:
            self.log(
                "wire_message",
                message_id=key,
                len=len(payload),
                head=payload[:48].hex(),
                decoded=decode_unknown(payload),
            )

    # ---- display side of the connection --------------------------------------------------
    def _set_display_state(self, state: str, **info: Any) -> None:
        """Advance the post-DTLS milestone (never backwards); READY arms StartGame."""
        d = self.session["display"]
        if DISPLAY_STATES.index(state) > DISPLAY_STATES.index(d["state"]):
            d["state"] = state
            self.log("display_state", display_state=state, **info)
            if DISPLAY_STATES.index(state) >= DISPLAY_STATES.index("ready"):
                self.game.maybe_start()

    def mark_input(self, via: str) -> None:
        """Input has arrived over ``via`` (the game session reports through here)."""
        self._set_display_state("input", via=via)

    def _start_display_rpc(self, reason: str) -> None:
        if self._display_task is not None:
            return
        if self._display_fallback_task is not None and self._display_fallback_task is not asyncio.current_task():
            self._display_fallback_task.cancel()
        self.session["display"]["trigger"] = reason
        since = round(time.monotonic() - self._dtls_at, 3) if self._dtls_at is not None else None
        self.log("display_rpc_trigger", reason=reason, since_dtls_s=since, delay_s=self.cfg.display_rpc_delay)
        self._display_task = asyncio.get_running_loop().create_task(self._display_rpc_loop())

    async def _display_rpc_fallback(self) -> None:
        try:
            await asyncio.sleep(self.cfg.display_rpc_fallback)
            if self._display_task is None:
                self.log(
                    "display_rpc_fallback",
                    after_s=self.cfg.display_rpc_fallback,
                    note="no v1:ResolveDiscovery since DTLS; starting the display RPCs anyway",
                )
                self._start_display_rpc("fallback")
        except asyncio.CancelledError:
            pass

    async def _display_rpc_loop(self) -> None:
        """Ask for the controller state until it answers OK.

        The controller sits in R/C/NOT_READY or R/C/WAITING_GET_CONTROLLER_STATE until the display
        asks for its state; READY needs that request plus the controller's own ACCEPT_COMMANDS
        (posted when discovery ends, after our ResolveDiscovery 200).  The request is re-sent
        until an OK answer with the controller state (an INTERNAL reply, e.g. in
        R/C/WAITING_ACCEPT_COMMAND, does not count) or until the controller's READY-only requests
        show up.
        """
        d = self.session["display"]
        cfg = self.cfg
        try:
            await asyncio.sleep(cfg.display_rpc_delay)
            deadline = time.monotonic() + cfg.display_rpc_window
            while time.monotonic() < deadline:
                if d["controller_state"] is not None or DISPLAY_STATES.index(d["state"]) >= DISPLAY_STATES.index(
                    "ready"
                ):
                    return
                msg = self.protos.DisplayToController()
                msg.get_controller_state.display_supports_send_log_report = cfg.send_log_report
                n = d["get_controller_state_sent"]
                if self.send_rpc("request", msg, purpose="get_controller_state", quiet=n >= 3) is not None:
                    d["get_controller_state_sent"] += 1
                    self._set_display_state("get-controller-state-sent")
                await asyncio.sleep(cfg.display_rpc_interval)
            self.log(
                "display_rpc_unanswered",
                sent=d["get_controller_state_sent"],
                rpc_in=d["rpc_in"],
                controller_state_errors=d["controller_state_errors"],
                window_s=cfg.display_rpc_window,
            )
        except asyncio.CancelledError:
            pass

    def send_rpc(
        self,
        kind: str,
        payload: Any = None,
        *,
        ticket: int | None = None,
        code: int = 0,
        message: str = "",
        purpose: str | None = None,
        quiet: bool = False,
    ) -> int | None:
        """One RpcMessage to the controller in a WireHeader2 record with id ``cfg.rpc_wire_id``.

        ``kind`` is request / notification / response; ``payload`` is a DisplayToController (or
        None).  Returns the ticket, or None when there is no DTLS session to send on.
        """
        p = self.protos
        if ticket is None:
            self._rpc_ticket += 1
            ticket = self._rpc_ticket
        if kind == "request":
            data = p.rpc_request(ticket, payload)
        elif kind == "notification":
            data = p.rpc_notification(ticket, payload)
        else:
            data = p.rpc_response(ticket, payload, code=code, message=message)
        record = pack_wire_message(self.cfg.rpc_wire_id, data)
        if not self.endpoint.send_app_data(record):
            self.log("rpc_out_failed", kind=kind, ticket=ticket, purpose=purpose, reason="no DTLS session")
            return None
        if kind == "request" and purpose:
            self._rpc_pending[ticket] = purpose
        self.session["display"]["rpc_out"] += 1
        self.log(
            "rpc_out",
            _quiet=quiet,
            kind=kind,
            ticket=ticket,
            purpose=purpose,
            code=code if kind == "response" else None,
            len=len(data),
            record=record.hex(),
            msg=p.to_dict(payload) if payload is not None else None,
        )
        return ticket

    def _on_rpc(self, data: bytes) -> None:
        """One RpcMessage from the controller; its payloads are ControllerToDisplay."""
        p = self.protos
        d = self.session["display"]
        d["rpc_in"] += 1
        try:
            rpc = p.parse_rpc(data, p.ControllerToDisplay)
        except DecodeError as e:
            self.log(
                "rpc_in_undecodable", len=len(data), error=str(e), head=data[:48].hex(), decoded=decode_unknown(data)
            )
            return
        kind, ticket, msg = rpc["kind"], rpc["ticket"], rpc["payload"]
        variant = msg.WhichOneof("variant") if msg is not None else None
        rec: dict[str, Any] = {"kind": kind, "ticket": ticket, "variant": variant}
        if kind == "response":
            rec.update(
                purpose=self._rpc_pending.pop(ticket, None),
                code=rpc.get("code"),
                error_message=rpc.get("error_message") or None,
            )
        if rpc["payload_error"]:
            rec.update(payload_error=rpc["payload_error"], payload_head=rpc["payload_raw"][:64].hex())
        counter = {"request": "requests", "notification": "notifications"}.get(kind)
        if counter:
            d[counter][variant or "?"] = d[counter].get(variant or "?", 0) + 1
        # READY-only traffic repeats every 1.5-3 s: the console shows the first few of each
        repeats = d[counter].get(variant or "?", 0) if counter else 0
        self.log("rpc_in", _quiet=repeats > 3, **rec, msg=p.to_dict(msg) if msg is not None else None)
        if kind == "request":
            self._answer_request(ticket, variant, msg, quiet=repeats > 3)
        elif kind == "notification" and variant == "message3":
            self._on_input_rpc(msg.message3)
        elif kind == "response" and rec.get("purpose") == "start_game":
            self.game.on_start_game_response(ticket, rpc)
        elif kind == "response" and rec.get("purpose") == "get_controller_state":
            if rpc.get("code") or variant != "controller_state":
                # e.g. 13 INTERNAL from the responder destructor (request in R/C/WAITING_ACCEPT_COMMAND)
                d["controller_state_errors"] += 1
                self.log(
                    "display_controller_state_error",
                    _quiet=d["controller_state_errors"] > 3,
                    code=rpc.get("code"),
                    error_message=rpc.get("error_message") or None,
                    variant=variant,
                )
                return
            state = p.to_dict(msg.controller_state)
            d["controller_state"] = state
            self.log(
                "display_controller_state",
                code=rpc.get("code"),
                device_id=state.get("device_id"),
                n_fields=len(state),
                controller_state=state,
            )
            self._set_display_state("controller-state-received")

    def _on_input_rpc(self, m: Any) -> None:
        """ControllerToDisplay #3 as an RpcNotification: the controller's input on the display link
        (READY only, sent on change).  Layout and button names: :mod:`stadiacast.gamepad`."""
        self.session["display"]["input_rpc"] += 1
        state = GamepadState.from_message3(m)
        ev = state.as_event(self._last_input)
        self._last_input = state
        self._set_display_state("input", via="rpc")
        self.emit_input_event(ev)

    def _answer_request(self, ticket: int | None, variant: str | None, msg: Any, quiet: bool = False) -> None:
        """Answers for the controller's requests.  The periodic ones only start on entry to
        R/C/READY, so the first of them marks READY."""
        if ticket is None:
            return  # the controller's own incoming_rpc_manager drops these too
        p = self.protos
        d = self.session["display"]
        if variant in ("get_current_time", "keepalive"):
            self._set_display_state("ready")
        if variant == "get_current_time":
            if self.cfg.time_sync == "unsupported":
                self.send_rpc(
                    "response",
                    None,
                    ticket=ticket,
                    code=p.enum(f"{PKG_RPC}.StatusCode", "NOT_FOUND"),
                    message="GetCurrentTimeRequest not supported",
                    quiet=quiet,
                )
                return
            reply = p.DisplayToController()
            # controller_time_us echoes the request: remote_clock.cc computes rtt = now - f1 and CHECK-fails on rtt < 0
            reply.current_time.controller_time_us = msg.get_current_time.controller_time_us
            reply.current_time.display_time_us = int(self.now() * 1_000_000)
            d["time_replies"] += 1
            self.send_rpc("response", reply, ticket=ticket, quiet=quiet)
        elif variant in ("keepalive", "disconnect"):
            if variant == "keepalive":
                d["keepalive_replies"] += 1
            self.send_rpc("response", None, ticket=ticket, quiet=quiet)
        elif variant == "proxy_request":
            self.game.on_proxy_request(ticket, msg.proxy_request)
        else:  # what the controller itself answers to requests it does not implement
            self.send_rpc(
                "response",
                None,
                ticket=ticket,
                code=p.enum(f"{PKG_RPC}.StatusCode", "UNIMPLEMENTED"),
                message="Unimplemented RPC in local receiver",
                quiet=quiet,
            )

    # ---- input events --------------------------------------------------------------------
    def emit_input_event(self, ev: dict[str, Any]) -> None:
        """Log one decoded input event, stamped with this controller, and hand it to the receiver's queue."""
        ev["controller"] = self.label
        self.log("input", **ev)
        self.rx.publish_event(ev)
