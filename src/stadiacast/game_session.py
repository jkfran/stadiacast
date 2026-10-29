"""The game-streaming session: StartGame, the StreamingClient's proxied RPCs, a second RTC endpoint and rumble.

Once the display link reaches READY the display may send ``StartGame`` (DisplayToController #8).
The controller's app then starts its StreamingClient, which signals a *second* WebRTC session
through the display link: an ``InitializationRequest`` (an RtcSdpOffer with a fresh certificate)
wrapped in ControllerToDisplay #1 ``ProxyRequest``.  We answer with the game endpoint's ICE/DTLS
parameters and host candidate; the controller then connects to it as the DTLS client.  Input
arrives on that session's sub-channel 0x66, and the same channel carries the input feedback
relay, which is the only path to the controller's rumble motors
(``InputFeedbackMessage_V2.gamepad_vibration``).
"""

from __future__ import annotations

import asyncio
import datetime
import json
import math
import secrets
import time
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs

from google.protobuf.message import DecodeError

from stadiacast import signaling
from stadiacast.http_server import HttpRequest, HttpResponse
from stadiacast.input_protocol import InputDecoder
from stadiacast.log import now_iso
from stadiacast.protos import PKG_RPC, decode_unknown
from stadiacast.rtc import (
    WIRE_ID_DC_RPC_CONTROLLER,
    WIRE_ID_DC_RPC_DISPLAY,
    WIRE_ID_RPC,
    WIRE_ID_RTP,
    WIRE_IDS_DC_RPC,
    DtlsCertificate,
    RtcEndpoint,
    RtpPacket,
    parse_rtp,
    parse_wire_message,
    wire_id_valid,
)

if TYPE_CHECKING:
    from stadiacast.controller import ControllerSession

#: StartGame key (DisplayToController #8 f1) and ProxyRequest f2: the protocol's maximum length.
GAME_KEY_MAX = 23
#: String limits of the game session's InitializationResponse; a longer value fails the controller's decode.
GAME_ANSWER_LIMITS = {"ice_ufrag": 7, "ice_password": 31, "fingerprint": 127}
GAME_CANDIDATE_LIMITS = {"sdp_mid": 7, "foundation": 11, "local_endpoint_ip": 39}
GAME_ICE_UFRAG_LEN = 6
GAME_ICE_PWD_LEN = 24
#: GamepadVibrationMessage_V2 motor speeds are absolute levels 0..65535.
MOTOR_MAX = 0xFFFF
RUMBLE_MS_MAX = 3_600_000
#: The firmware's "Haptic watchdog": the motors stop 5 s after the last level written.
HAPTICS_WATCHDOG_S = 5.0
#: How a message goes out on the game session: RTP in WireHeader2 0x66, the bare message in 0x66,
#: SRTP on the UDP flow, or the bare message as a DTLS record without WireHeader2.
GAME_FRAMINGS = ("rtp", "raw", "srtp", "unframed")


#: Common name of the game endpoint's DTLS certificate (only its fingerprint matters to the controller).
GAME_CERT_NAME = "stadia-local-game"


def new_game_key() -> str:
    """A fresh StartGame key (22 characters, within :data:`GAME_KEY_MAX`)."""
    return "local-" + secrets.token_hex(8)


def _fresh_state() -> dict[str, Any]:
    return {
        "state": "idle",
        "key": None,
        "start_game_ticket": None,
        "start_game_sent": 0,
        "result": None,
        "init_requests": 0,
        "init_answered": 0,
        "offer": None,
        "answer": None,
        "rtc_state": None,
        "wire_in": {},
        "input_framing": None,
        "input_payload_type": None,
        "input_messages": 0,
        "input_errors": 0,
        "acks_sent": 0,
        "rpc_in": 0,
        "rpc_out": 0,
        "rtcp_in": 0,
        "gamepad_id": None,
        "series_session_id": None,
        "controller_ack": None,
        "rumble": {"left": 0, "right": 0, "until": None, "sent": 0, "last_seq": None},
    }


class GameSession:
    """Game-phase state of one controller: owned by its :class:`~stadiacast.controller.ControllerSession`.

    ``state`` is the dict published as ``session["game"]`` in ``/_status``; it keeps its
    identity across :meth:`reset` so readers can hold on to it.
    """

    def __init__(self, controller: ControllerSession) -> None:
        self.ctl = controller
        self.cfg = controller.cfg
        self.log = controller.log
        self.protos = controller.protos
        # A second connection with its own UDP flow, certificate, ICE and DTLS; the controller is the DTLS client.
        self.cert = DtlsCertificate(GAME_CERT_NAME)
        self.endpoint = RtcEndpoint(
            cert=self.cert,
            controlling=False,
            dtls_role="server",
            log=self.log,
            on_rtp=self._on_rtp,
            on_rtcp=self._on_rtcp,
            on_app_data=self._on_app_data,
            on_state=self._on_rtc_state,
            ice_ufrag="x" * GAME_ICE_UFRAG_LEN,
            name="game",
            fast_check_interval=self.cfg.ice_fast_checks,
        )
        self.input = InputDecoder(self.protos, on_event=self._on_input_event)
        self.state: dict[str, Any] = _fresh_state()
        self._task: asyncio.Task[None] | None = None  # the delayed StartGame
        self._rumble_task: asyncio.Task[None] | None = None
        self._feedback_seq = 0  # sequence numbers of our InputMessageSeries to the controller
        self._start_game_at: float | None = None  # monotonic time of our last StartGame
        self._init_at: float | None = None  # monotonic time we answered the InitializationRequest

    # ---- lifecycle -----------------------------------------------------------------------
    async def start(self, bind: str, port: int) -> None:
        await self.endpoint.start(bind, port)

    def close(self) -> None:
        self.cancel_tasks()
        self.endpoint.close()

    def cancel_tasks(self) -> None:
        for name in ("_task", "_rumble_task"):
            task = getattr(self, name)
            if task is not None and task is not asyncio.current_task():
                task.cancel()
            setattr(self, name, None)

    def reset(self) -> None:
        """A new boot of the controller: forget the previous game session."""
        self.cancel_tasks()
        self.state.clear()
        self.state.update(_fresh_state())
        self._start_game_at = None
        self._init_at = None
        self._feedback_seq = 0
        self.endpoint.clear_remote()
        self.input.gamepads.clear()

    def status(self) -> dict[str, Any]:
        return {
            "game_rtc_stats": self.endpoint.stats,
            "game_input_stats": dict(self.input.stats),
            "game_udp": [self.cfg.advertise_ip, self.endpoint.local_port],
        }

    # ---- StartGame -----------------------------------------------------------------------
    def maybe_start(self) -> None:
        """Once per boot, ``cfg.start_game_delay`` after READY: DisplayToController #8."""
        if self.cfg.start_game is None or self._task is not None or self.state["start_game_sent"]:
            return
        self._task = asyncio.get_running_loop().create_task(self._start_after(self.cfg.start_game_delay))

    async def _start_after(self, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
            self.send_start_game(self.cfg.start_game)
        except asyncio.CancelledError:
            pass

    def send_start_game(self, key: str | None = None) -> int | None:
        """DisplayToController #8 StartGame {1: key (max 23), 2: 1}.

        READY -> app event START_GAME: BROWSE starts the StreamingClient, which signals the game
        session through the display link (ControllerToDisplay #1 ProxyRequest) and holds this
        request's reply until GAME_STARTED (OK) or GAME_STOPPED (error).
        """
        g = self.state
        if not key or key == "auto":
            key = g["key"] or new_game_key()
        if len(key.encode()) > GAME_KEY_MAX:
            raise ValueError(f"StartGame key is limited to {GAME_KEY_MAX} bytes")
        msg = self.protos.DisplayToController()
        msg.start_game.value = key
        msg.start_game.number = 1
        ticket = self.ctl.send_rpc("request", msg, purpose="start_game")
        if ticket is None:
            return None
        g.update(
            state="start-game-sent",
            key=key,
            start_game_ticket=ticket,
            result=None,
            start_game_sent=g["start_game_sent"] + 1,
            sent_at=now_iso(),
        )
        self._start_game_at = time.monotonic()
        self.log("game_start_requested", key=key, ticket=ticket, display_state=self.ctl.display_state)
        return ticket

    def on_start_game_response(self, ticket: int | None, rpc: dict[str, Any]) -> None:
        g = self.state
        code = rpc.get("code") or 0
        waited = round(time.monotonic() - self._start_game_at, 3) if self._start_game_at is not None else None
        g["result"] = {"code": code, "error_message": rpc.get("error_message") or None, "after_s": waited}
        if code == 0:
            g["state"] = "game-started"
            self.log("game_started", key=g["key"], ticket=ticket, after_s=waited)
        else:
            g["state"] = "game-start-failed"
            self.log(
                "game_start_failed",
                key=g["key"],
                ticket=ticket,
                code=code,
                error_message=rpc.get("error_message") or None,
                after_s=waited,
            )

    def handle_start_game_request(self, req: HttpRequest) -> HttpResponse:
        """``POST /_start_game[?key=K]``: send StartGame now (a manual retry in a live session)."""
        if req.method != "POST":
            return HttpResponse(405, b"", handler="start_game")
        key = parse_qs(req.query).get("key", [None])[0]
        try:
            ticket = self.send_start_game(key)
        except ValueError as e:
            return HttpResponse(400, json.dumps({"error": str(e)}).encode(), "application/json", handler="start_game")
        body = {"sent": ticket is not None, "ticket": ticket, "game": self.state}
        return HttpResponse(
            200 if ticket is not None else 409,
            json.dumps(body, default=str).encode(),
            "application/json",
            handler="start_game",
        )

    # ---- the StreamingClient's RPCs, proxied through the display link --------------------
    def on_proxy_request(self, ticket: int, pr: Any) -> None:
        """ControllerToDisplay #1 ProxyRequest {1: 1, 2: StartGame key, 4: body}.

        ``body`` is an inner RpcMessage request whose payload is a StreamerRequest.  The answer
        is an RpcResponse (outer ticket, OK) carrying DisplayToController #1 {data: inner
        RpcMessage response (inner ticket) with a StreamerResponse}.
        """
        p = self.protos
        status = f"{PKG_RPC}.StatusCode"
        info: dict[str, Any] = {
            "outer_ticket": ticket,
            "f1": pr.f1 if pr.HasField("f1") else None,
            "key": pr.f2 if pr.HasField("f2") else None,
            "f3": pr.f3 if pr.HasField("f3") else None,
            "body_len": len(pr.body),
        }
        try:
            inner = p.parse_rpc(pr.body, p.StreamerRequest)
        except DecodeError as e:
            inner = {"kind": None, "error": str(e)}
        if inner["kind"] != "request" or inner.get("ticket") is None or inner.get("payload") is None:
            self.log(
                "game_proxy_request_invalid",
                **info,
                inner_kind=inner["kind"],
                error=inner.get("error") or inner.get("payload_error"),
                head=pr.body[:48].hex(),
            )
            self.ctl.send_rpc(
                "response",
                None,
                ticket=ticket,
                code=p.enum(status, "INVALID_ARGUMENT"),
                message="Unparsable proxied StreamerRequest",
                purpose="proxy_response",
            )
            return
        sreq, inner_ticket = inner["payload"], inner["ticket"]
        if not sreq.HasField("initialization"):  # the only StreamerRequest variant known
            self.log("game_proxy_request_unknown", **info, inner_ticket=inner_ticket, msg=p.to_dict(sreq))
            inner_resp = p.rpc_response(
                inner_ticket,
                code=p.enum(status, "UNIMPLEMENTED"),
                message="Unimplemented StreamerRequest in local receiver",
            )
        else:
            inner_resp = self._answer_initialization(info, inner_ticket, sreq.initialization)
        out = p.DisplayToController()
        out.proxy_response.data = inner_resp
        self.ctl.send_rpc("response", out, ticket=ticket, purpose="proxy_response")

    def _answer_initialization(self, info: dict[str, Any], inner_ticket: int, init: Any) -> bytes:
        """InitializationRequest {1: 1, 2: RtcSdpOffer (fresh per-session certificate), 3: int, 4: string}.

        The controller offers the game session; answer with the game endpoint's ICE/DTLS
        parameters and its host candidate (an IPv4 literal + port, which the controller parses
        before it opens the new UDP flow).
        """
        p = self.protos
        g = self.state
        g["init_requests"] += 1
        offer = p.RtcSdpOffer()
        try:
            offer.ParseFromString(init.offer)
        except DecodeError as e:
            self.log("game_offer_undecodable", **info, inner_ticket=inner_ticket, error=str(e))
            return p.rpc_response(
                inner_ticket,
                code=p.enum(f"{PKG_RPC}.StatusCode", "INVALID_ARGUMENT"),
                message="Undecodable RtcSdpOffer",
            )
        ep = self.endpoint
        ep.new_local_credentials(GAME_ICE_UFRAG_LEN, GAME_ICE_PWD_LEN)
        ep.controlling = False
        ep.set_remote(offer.ice_ufrag, offer.ice_password, offer.fingerprint or None)
        answer = signaling.build_answer(p, offer, ep, boreal=self.cfg.boreal).sdp_answer
        candidate = signaling.candidate_message(p, self.cfg.advertise_ip or "", ep.local_port or 0).ice_candidate
        too_long = {
            f"answer.{k}": len(getattr(answer, k))
            for k, n in GAME_ANSWER_LIMITS.items()
            if len(getattr(answer, k).encode()) > n
        }
        too_long.update(
            {
                f"candidate.{k}": len(getattr(candidate, k))
                for k, n in GAME_CANDIDATE_LIMITS.items()
                if len(getattr(candidate, k).encode()) > n
            }
        )
        sresp = p.StreamerResponse()
        sresp.initialization.answer = answer.SerializeToString()
        sresp.initialization.candidate = candidate.SerializeToString()
        offer_d = p.to_dict(offer)
        g.update(
            state="init-answered",
            offer=offer_d,
            answer=p.to_dict(answer),
            udp=[candidate.local_endpoint_ip, candidate.local_endpoint_port],
            init={
                "version": init.version if init.HasField("version") else None,
                "f3": init.f3 if init.HasField("f3") else None,
                "f4": init.f4 if init.HasField("f4") else None,
            },
            proxy={k: info[k] for k in ("f1", "key", "f3")},
        )
        g["init_answered"] += 1
        self._init_at = time.monotonic()
        self.log(
            "game_init_answered",
            **info,
            inner_ticket=inner_ticket,
            version=g["init"]["version"],
            init_f3=g["init"]["f3"],
            init_f4=g["init"]["f4"],
            remote_ufrag=offer.ice_ufrag,
            remote_fingerprint=offer.fingerprint,
            raw_dtls=offer.raw_dtls,
            ice_ufrag=answer.ice_ufrag,
            udp=g["udp"],
            too_long=too_long or None,
            offer=offer_d,
            answer=g["answer"],
        )
        return p.rpc_response(inner_ticket, sresp)

    # ---- the game session's own ICE/DTLS -------------------------------------------------
    def _on_rtc_state(self, state: str, info: dict[str, Any]) -> None:
        """DTLS done = OnSessionConnected on the controller, which posts GAME_STARTED: our held
        StartGame request is then answered OK on the display link."""
        g = self.state
        g["rtc_state"] = state
        self.log("game_rtc_state", rtc_state=state, **{k: v for k, v in info.items() if k != "peer_fingerprint"})
        if state == "dtls-connected":
            since = round(time.monotonic() - self._init_at, 3) if self._init_at is not None else None
            g["connected"] = {
                "at": now_iso(),
                "since_init_s": since,
                "role": info.get("role"),
                "fingerprint_verified": info.get("fingerprint_verified"),
                "srtp_profile": info.get("srtp_profile"),
            }
            if g["state"] in ("idle", "start-game-sent", "init-answered"):
                g["state"] = "game-connected"
            self.log(
                "game_connected",
                since_init_s=since,
                role=info.get("role"),
                fingerprint_verified=info.get("fingerprint_verified"),
                peer=self.endpoint.remote.selected if self.endpoint.remote else None,
            )
        elif state == "dtls-closed":
            if self._rumble_task is not None:
                self._rumble_task.cancel()
                self._rumble_task = None
            g["state"] = "game-closed"
            self.log("game_closed", wire_in=g["wire_in"], input_messages=g["input_messages"])

    def _on_rtcp(self, data: bytes) -> None:
        self.log("rtcp", name="game", pt=data[1] if len(data) > 1 else None, len=len(data), head=data[:16].hex())

    def _on_app_data(self, data: bytes) -> None:
        """Game-session DTLS application data: MuxedChannel messages in WireHeader2.

        The id rule of the display link is not applied here, so whatever the controller uses
        shows up: 0x66 "streamer-input" (input out, feedback in) and the RPC channel (0xFFFA;
        0x74/0x75 also routed).
        """
        g = self.state
        try:
            msg_id, payload = parse_wire_message(data, strict_id=False)
        except ValueError as e:
            g["wire_in"]["unframed"] = g["wire_in"].get("unframed", 0) + 1
            if g["wire_in"]["unframed"] <= 3:
                self.log("game_app_unframed", len=len(data), error=str(e), head=data[:48].hex())
            self._on_input(data, "unframed")
            return
        key = f"{msg_id:#06x}"
        g["wire_in"][key] = g["wire_in"].get(key, 0) + 1
        if msg_id == WIRE_ID_RTP:
            self._on_channel_input(payload)
        elif msg_id == WIRE_ID_RPC or msg_id in WIRE_IDS_DC_RPC:
            self._on_rpc(msg_id, payload)
        elif g["wire_in"][key] <= 3:
            self.log(
                "game_wire_message",
                wire_id=key,
                len=len(payload),
                id_valid_on_display_link=wire_id_valid(msg_id),
                head=payload[:48].hex(),
                decoded=decode_unknown(payload),
            )

    def _on_channel_input(self, payload: bytes) -> None:
        """Sub-channel 0x66: an RTP packet (version 2) around an InputProtocolMessage, or the
        bare InputProtocolMessage (its first tag byte is never 0b10xxxxxx)."""
        if len(payload) >= 12 and payload[0] >> 6 == 2:
            if 192 <= payload[1] <= 223:  # RFC 5761 RTCP range
                g = self.state
                g["rtcp_in"] += 1
                if g["rtcp_in"] <= 3:
                    self.log("game_rtcp", pt=payload[1], len=len(payload))
                return
            try:
                pkt = parse_rtp(payload)
            except ValueError:
                pkt = None
            if pkt is not None:
                self._on_input(pkt.payload, "rtp", pkt)
                return
        self._on_input(payload, "raw")

    def _on_rtp(self, pkt: RtpPacket) -> None:
        """SRTP on the game session's UDP flow (if the session does not use raw DTLS)."""
        self._on_input(pkt.payload, "srtp", pkt)

    def _on_input(self, payload: bytes, framing: str, pkt: RtpPacket | None = None) -> None:
        g = self.state
        try:
            decoded = self.input.decode(payload)
        except (DecodeError, ValueError) as e:
            g["input_errors"] += 1
            if g["input_errors"] <= 5:
                self.log(
                    "game_input_undecodable",
                    framing=framing,
                    len=len(payload),
                    error=str(e),
                    head=payload[:48].hex(),
                    decoded=decode_unknown(payload),
                )
            return
        g["input_messages"] += 1
        if g["input_framing"] != framing:
            # an unframed record does not override a framing already seen on 0x66 / SRTP
            keep = framing == "unframed" and g["input_framing"] not in (None, "unframed")
            self.log(
                "game_input_framing",
                framing=framing,
                previous=g["input_framing"],
                kept_previous=keep,
                payload_type=pkt.payload_type if pkt is not None else None,
            )
            if not keep:
                g["input_framing"] = framing
        if pkt is not None:
            g["input_payload_type"] = pkt.payload_type
        if decoded.get("session_id"):
            g["series_session_id"] = decoded["session_id"]
        if decoded.get("ack_sequence_number") is not None:
            g["controller_ack"] = decoded["ack_sequence_number"]
        for ev in decoded.get("events", []):
            if ev.get("type") == "gamepad" and ev.get("id") is not None:
                g["gamepad_id"] = ev["id"]
        if g["input_messages"] == 1:
            self.ctl.mark_input("game")
        self.log(
            "game_input",
            _quiet=g["input_messages"] > 3,
            framing=framing,
            n=g["input_messages"],
            n_events=len(decoded.get("events", [])),
            sequence_number=decoded.get("sequence_number"),
            ack=decoded.get("ack_sequence_number"),
            compressed=decoded.get("compressed", False),
        )
        if self.cfg.ack_mode == "rtp" and decoded.get("sequence_number") is not None:
            ack = self.protos.InputProtocolMessage()
            ack.ack_sequence_number = decoded["sequence_number"]
            if self.send_input_message(ack.SerializeToString()):
                g["acks_sent"] += 1
                g["last_ack"] = decoded["sequence_number"]

    def _on_input_event(self, ev: dict[str, Any]) -> None:
        ev["via"] = "game"
        self.ctl.emit_input_event(ev)

    # ---- sending on the game session -----------------------------------------------------
    def framing(self) -> str:
        """How our messages go out: ``--game-framing``, or (auto) the controller's own input
        framing, RTP-in-0x66 until any is seen."""
        if self.cfg.game_framing != "auto":
            return self.cfg.game_framing
        seen = self.state["input_framing"]
        return seen if seen in GAME_FRAMINGS else "rtp"

    def send_input_message(self, payload: bytes, framing: str | None = None) -> bool:
        """One serialized InputProtocolMessage to the controller (sub-channel 0x66 inside DTLS, or
        SRTP when the session runs without raw DTLS); ``framing`` overrides :meth:`framing`."""
        ep = self.endpoint
        framing = framing or self.framing()
        pt = self.state["input_payload_type"] or self.cfg.input_payload_type
        if framing == "raw":
            return ep.send_wire(WIRE_ID_RTP, payload)
        if framing == "srtp":
            return ep.send_rtp(payload, payload_type=pt)
        if framing == "unframed":
            return ep.send_app_data(payload)
        return ep.send_rtp_wire(payload, payload_type=pt)

    # ---- rumble: InputFeedbackMessage_V2.gamepad_vibration -------------------------------
    def send_vibration(
        self, left: int, right: int, *, gamepad_id: int | None = None, framing: str | None = None
    ) -> int | None:
        """One InputProtocolMessage{input_message_series{messages[{input_feedback_message_v2
        {gamepad_vibration{id, left_motor_speed, right_motor_speed}}}]}} on 0x66.

        Levels are absolute and held by the controller until changed, or until its 5000 ms
        haptics watchdog stops the motors.  Returns the series sequence number, None when the
        game session is not connected.
        """
        p = self.protos
        g = self.state
        msg = p.InputProtocolMessage()
        series = msg.input_message_series
        if g["series_session_id"]:
            series.session_id = g["series_session_id"]
        self._feedback_seq += 1
        series.sequence_number = self._feedback_seq
        series.base_timestamp_us = int(time.time() * 1_000_000)
        slim = series.messages.add()
        slim.relative_time_us = 0
        vib = slim.input_feedback_message_v2.gamepad_vibration
        vib.id = (g["gamepad_id"] or 0) if gamepad_id is None else gamepad_id  # the controller's gamepad id
        vib.left_motor_speed, vib.right_motor_speed = left, right
        framing = framing or self.framing()
        if not self.send_input_message(msg.SerializeToString(), framing):
            self._feedback_seq -= 1
            return None
        r = g["rumble"]
        r["sent"] += 1
        r["last_seq"] = self._feedback_seq
        r["framing"] = framing
        self.log(
            "rumble_sent",
            _quiet=r["sent"] > 3,
            left=left,
            right=right,
            id=vib.id,
            seq=self._feedback_seq,
            framing=framing,
        )
        return self._feedback_seq

    def set_rumble(self, left: int, right: int, ms: float | None = None, framing: str | None = None) -> int | None:
        """Set both motors now.

        Non-zero levels are re-sent every ``cfg.rumble_refresh`` seconds (the watchdog) for ``ms``
        milliseconds, then stopped with 0/0; without ``ms`` they are held until the next call.
        0/0 stops at once.  ``framing`` (one of GAME_FRAMINGS) overrides ``--game-framing`` for
        this rumble.  Everything is validated before anything is sent.
        """
        for name, v in (("left", left), ("right", right)):
            if not isinstance(v, int) or isinstance(v, bool) or not 0 <= v <= MOTOR_MAX:
                raise ValueError(f"{name} must be an integer 0..{MOTOR_MAX}")
        if ms is not None and (
            not isinstance(ms, (int, float))
            or isinstance(ms, bool)
            or not math.isfinite(ms)
            or not 0 < ms <= RUMBLE_MS_MAX
        ):
            raise ValueError(f"ms must be a number in (0, {RUMBLE_MS_MAX}] (leave it out to hold until the next call)")
        if framing is not None and framing not in GAME_FRAMINGS:
            raise ValueError(f"framing must be one of {', '.join(GAME_FRAMINGS)}")
        until = (
            (datetime.datetime.now().astimezone() + datetime.timedelta(milliseconds=ms)).isoformat(
                timespec="milliseconds"
            )
            if ms and (left or right)
            else None
        )
        if self._rumble_task is not None:
            self._rumble_task.cancel()
            self._rumble_task = None
        seq = self.send_vibration(left, right, framing=framing)
        if seq is None:
            return None
        r = self.state["rumble"]
        r.update(left=left, right=right, until=until, set_at=now_iso(), ms=ms)
        if left or right:
            self._rumble_task = asyncio.get_running_loop().create_task(self._rumble_hold(left, right, ms, framing))
        self.log(
            "rumble_set",
            left=left,
            right=right,
            ms=ms,
            seq=seq,
            framing=r.get("framing"),
            refresh_s=self.cfg.rumble_refresh,
        )
        return seq

    async def _rumble_hold(self, left: int, right: int, ms: float | None, framing: str | None = None) -> None:
        loop = asyncio.get_running_loop()
        end = loop.time() + ms / 1000 if ms else None
        n = 0
        try:
            while True:
                wait = self.cfg.rumble_refresh
                if end is not None:
                    wait = min(wait, end - loop.time())
                await asyncio.sleep(max(wait, 0.01))  # always yield, whatever the configuration
                if end is not None and loop.time() >= end - 0.001:
                    break
                n += 1
                d = n % 2 if self.cfg.rumble_dither else 0
                refreshed = self.send_vibration(
                    max(left - d, 0) if left else 0, max(right - d, 0) if right else 0, framing=framing
                )
                if refreshed is None:
                    self.log("rumble_refresh_failed", reason="game session not connected")
                    return
            self.send_vibration(0, 0, framing=framing)
            r = self.state["rumble"]
            r.update(left=0, right=0, until=None)
            self.log("rumble_done", ms=ms, refreshes=n)
        except asyncio.CancelledError:
            pass
        finally:
            if self._rumble_task is asyncio.current_task():
                self._rumble_task = None

    def handle_rumble_request(self, req: HttpRequest) -> HttpResponse:
        """``POST /_rumble {"left": 0..65535, "right": 0..65535, "ms": optional, "framing": optional}``
        (or the same as query parameters; a ``controller`` key is the receiver's routing and ignored
        here); ``GET /_rumble`` shows the current state."""
        g = self.state
        if req.method == "GET":
            body = json.dumps({"rumble": g["rumble"], "game_state": g["state"]}, default=str).encode()
            return HttpResponse(200, body, "application/json", handler="rumble")
        if req.method != "POST":
            return HttpResponse(405, b"", handler="rumble")
        try:
            args = json.loads(req.body) if req.body.strip() else {}
            if not isinstance(args, dict):
                raise ValueError("body must be a JSON object")
            for k, v in parse_qs(req.query).items():
                if k == "controller":  # the receiver's routing key, not a rumble parameter
                    continue
                args.setdefault(k, v[0] if k == "framing" else float(v[0]) if k == "ms" else int(v[0]))
            seq = self.set_rumble(args.get("left", 0), args.get("right", 0), args.get("ms"), args.get("framing"))
        except ValueError as e:  # json.JSONDecodeError is a ValueError
            return HttpResponse(400, json.dumps({"error": str(e)}).encode(), "application/json", handler="rumble")
        out = {
            "sent": seq is not None,
            "seq": seq,
            "framing": g["rumble"].get("framing"),
            "game_state": g["state"],
            "controller_ack": g["controller_ack"],
            "game_wire_in": g["wire_in"],
            "rumble": g["rumble"],
        }
        if seq is None:
            out["error"] = "game session not connected (StartGame first: --start-game or POST /_start_game)"
        return HttpResponse(
            200 if seq is not None else 409, json.dumps(out, default=str).encode(), "application/json", handler="rumble"
        )

    # ---- the game session's RPC channel --------------------------------------------------
    def _on_rpc(self, msg_id: int, payload: bytes) -> None:
        """An RpcMessage on the game session.  Requests are answered per ``--game-rpc-reply`` on
        the mirrored id (0x74 -> 0x75, otherwise the same id)."""
        p = self.protos
        g = self.state
        g["rpc_in"] += 1
        m = p.RpcMessage()
        try:
            m.ParseFromString(payload)
        except DecodeError as e:
            if g["rpc_in"] <= 5:
                self.log(
                    "game_rpc_undecodable",
                    wire_id=f"{msg_id:#06x}",
                    len=len(payload),
                    error=str(e),
                    head=payload[:48].hex(),
                )
            return
        kind = m.WhichOneof("payload")
        body = getattr(m, kind) if kind else None
        ticket = body.ticket if body is not None and body.HasField("ticket") else None
        inner = body.payload if body is not None and body.HasField("payload") else b""
        counts = g.setdefault("rpc_kinds", {})
        counts[kind or "?"] = counts.get(kind or "?", 0) + 1
        self.log(
            "game_rpc_in",
            _quiet=counts[kind or "?"] > 3,
            wire_id=f"{msg_id:#06x}",
            kind=kind,
            ticket=ticket,
            code=body.code if kind == "response" and body is not None else None,
            payload_len=len(inner),
            decoded=decode_unknown(inner) if inner else None,
        )
        if kind != "request" or ticket is None or self.cfg.game_rpc_reply == "none":
            return
        reply_id = WIRE_ID_DC_RPC_DISPLAY if msg_id == WIRE_ID_DC_RPC_CONTROLLER else msg_id
        code = 0 if self.cfg.game_rpc_reply == "ok" else p.enum(f"{PKG_RPC}.StatusCode", "UNIMPLEMENTED")
        data = p.rpc_response(ticket, code=code, message="" if code == 0 else "Unimplemented RPC in local receiver")
        if self.endpoint.send_wire(reply_id, data, strict_id=False):
            g["rpc_out"] += 1
            self.log("game_rpc_out", _quiet=counts[kind] > 3, wire_id=f"{reply_id:#06x}", ticket=ticket, code=code)
