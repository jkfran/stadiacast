#!/usr/bin/env python3
"""A stand-in for the Stadia controller's network side, used by the tests and
for manual loopback runs against the receiver.

It reproduces what the firmware is known to do:
HTTP/1.1 over TLS with `Host: name:443`, the boot-time requests (generate_204,
stadiaconfig, Omaha update2, Clearcut log/batch, v1:FirmwareVersionCheck,
v1:RegisterController), then WebRTC signaling with the same protobufs,
STUN checks, DTLS-SRTP and RTP carrying InputProtocolMessage.

Signaling modes:
  --mode firmware  (default)  what the controller does:
                   RegisterController, DiscoverDisplay carrying our
                   WebRtcSdpAndIceCandidates offer in field 4 (raw_dtls,
                   delayed_acks), then poll /gmsg until the GmsgEnvelope with
                   the receiver's answer bundle arrives, and ack its id on the
                   next poll.  After DTLS: OnConnected registers the
                   0xFFFA reader and the discovery client posts ResolveDiscovery
                   (S/UNREGISTERING); its 200 ends discovery -> ACCEPT_COMMANDS and
                   the app answers the display's GetControllerStateRequest
                   (ControllerToDisplay #2); then as in R/C/READY send
                   GetCurrentTimeRequest (#18) and a keepalive (#14), and stream
                   input as plain RTP on sub-channel 0x66 inside DTLS
  --mode receiver-offers      legacy experiment (receiver --signaling-mode offer):
                   the receiver's offer arrives in the GMsg envelope, we post a
                   bare answer to an unknown path
  --mode bare-offer           post a bare RtcSdpOffer to an unknown path and
                   consume the answer bundle from the response body

    python3 tests/fake_controller.py --host 127.0.0.1 --https-port 8443 [--mode ...]

(The package must be importable: ``uv sync`` or ``pip install -e .`` first.)
"""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import secrets
import ssl
import sys
import time
import zlib

from stadiacast.game_session import GAME_ANSWER_LIMITS, GAME_CANDIDATE_LIMITS
from stadiacast.protos import PKG_API, Protos
from stadiacast.rtc import (
    HOST_CANDIDATE_PRIORITY,
    WIRE_ID_DC_RPC_CONTROLLER,
    WIRE_ID_RTP,
    WIRE_IDS_DC_RPC,
    DtlsCertificate,
    RtcEndpoint,
    parse_rtp,
    parse_wire_message,
    random_ice_string,
)

OMAHA_REQUEST = (
    '<?xml version="1.0" encoding="UTF-8"?><request protocol="3.0" updater="CarawayUpdater" '
    'updaterversion="0.1.1.0" ismachine="1"><os platform="gotham" version="1.6" arch="arm"/>'
    '<app appid="{fake-app-id}" board="gotham" serial_num="GA00722-FAKE" track="stable-channel" '
    'image="gotham-pvt-a" version="282115" fsi_build="282115"><updatecheck /><ping ad="-1" rd="-1"/>'
    "</app></request>"
)


class FakeController:
    def __init__(
        self,
        host: str,
        https_port: int,
        protos: Protos | None = None,
        *,
        bind_ip: str = "127.0.0.1",
        log=print,
        dtls_role: str = "server",
        controlling: bool = False,
        raw_dtls: bool = False,
        on_connected_delay: float = 0.3,
        handoff_delay: float = 0.0,
        early_rpc: str = "drop",
        game_mode: bool = False,
        game_framing: str = "rtp",
        haptics_watchdog: float = 5.0,
        device_id: str = "fake-device",
        cloud_device_id: str = "fake-cloud",
    ):
        self.host = host
        self.https_port = https_port
        self.protos = protos or Protos()
        self.bind_ip = bind_ip
        self.log = log
        self.cert = DtlsCertificate("fake-stadia-controller")
        self.acks: asyncio.Queue = asyncio.Queue()
        self.rtp_in: asyncio.Queue = asyncio.Queue()
        self.endpoint = RtcEndpoint(
            cert=self.cert,
            controlling=controlling,
            dtls_role=dtls_role,
            log=self._rtc_log,
            on_rtp=self._on_rtp,
            on_app_data=self._on_app_data,
            name="fake-controller",
        )
        # raw_dtls (what the firmware offers): input and RPCs travel as WireHeader2
        # messages in DTLS application data instead of SRTP
        self.raw_dtls = raw_dtls
        # Post-DTLS display connection.  The
        # live controller started ResolveDiscovery ~0.48 s after DTLS; until then
        # (OnConnected) no 0xFFFA reader exists.  early_rpc: what a display RPC
        # arriving in that window does -- "drop" (the demux drops unregistered ids)
        # or "stall" (what one live run showed: OnConnected never ran, no
        # ResolveDiscovery, no RPC answers).
        self.on_connected_delay = on_connected_delay
        self.handoff_delay = handoff_delay  # ResolveDiscovery 200 -> discovery hands the DC to the app
        self.early_rpc = early_rpc
        self.dc_state = "connecting"  # connecting | not_ready | waiting_gcs | waiting_accept | ready
        self.app_has_dc = False
        self.early_rpc_records = 0  # 0xFFFA records that arrived before OnConnected
        self.resolve_status: int | None = None  # HTTP status of the post-DTLS ResolveDiscovery
        self._gcs_queue: list[int] = []  # GetControllerState tickets the app defers until the hand-off
        self._on_connected_task: asyncio.Task | None = None
        self.display_ready = asyncio.Event()  # R/C/READY reached (state 9)
        self.rpc_in: list[dict] = []  # every RpcMessage from the display, parsed
        self._rpc_pending: dict[int, asyncio.Future] = {}
        self._rpc_ticket = 1000
        self.remote_candidates: list[tuple[str, int]] = []
        self.seq = 0
        self.session_id = secrets.token_hex(8)
        self.device_id = device_id
        self.cloud_device_id = cloud_device_id
        self.display_token: str | None = None  # DiscoverDisplayResponse.display_token, echoed as the GMsg subscription
        self.attempt_id = secrets.token_hex(8)
        self.registration = None  # RegisterControllerResponse
        self.discovery = None  # DiscoverDisplayResponse
        self.last_gmsg_id: int | None = None
        self.wire_dropped = 0  # records the firmware's WireHeader2 check would drop
        self.start_game_requests: list[dict] = []  # DisplayToController #8 StartGame {ticket, key, number}
        self._start_game_held: int | None = None  # ticket whose reply waits for GAME_STARTED / GAME_STOPPED
        self.app_state = "browse"  # browse | starting_game | in_game (the Yeti app)
        # Game-streaming session (GothamRtcSession): a second connection with a fresh certificate
        # per session, its own UDP flow and ICE/DTLS, controller = DTLS client.
        self.game_endpoint: RtcEndpoint | None = None
        self.game_answer = None  # RtcSdpAnswer of the InitializationResponse
        self.game_candidate: tuple[str, int] | None = None
        self.game_app_in: list[bytes] = []  # DTLS application data received on the game session
        self._streamer_ticket = 0  # the StreamingClient's own RPC tickets (inner RpcMessage)
        # game_mode: StartGame runs the whole game session like the firmware (InitializationRequest,
        # ICE + DTLS client to the answered endpoint, GAME_STARTED); game_framing: how input goes out
        # on sub-channel 0x66 (rtp = RTP in WireHeader2, raw = the bare InputProtocolMessage)
        self.game_mode = game_mode
        self.game_framing = game_framing
        self.game_error: str | None = None
        self.game_acks: asyncio.Queue = asyncio.Queue()
        self.game_feedback: list = []  # InputMessageSlim.input_feedback_message_v2 received
        self.game_rpc_in: list[tuple[int, dict]] = []  # (wire id, parsed RpcMessage) on the game session
        self._game_task: asyncio.Task | None = None
        # Motors (SetMotors(left, right)) and the
        # 5000 ms FreeRTOS "Haptic watchdog" that stops them when no new level arrives in time
        self.haptics_watchdog = haptics_watchdog
        self.motors = (0, 0)
        self.motor_writes = 0
        self.watchdog_expired = 0
        self.game_feedback_seqs: list[int] = []  # InputMessageSeries.sequence_number of the display's series
        self.game_rx_framing: list[str] = []  # framing of every 0x66 message accepted from the display
        self.game_framing_errors = 0  # 0x66 messages not in our own framing: dropped
        self.on_motors = None  # callback(left, right, why) on every motor change
        self._watchdog_handle = None

    # ---- logging --------------------------------------------------------------
    def _rtc_log(self, event, **kw):
        self.log(f"[fake] {event} " + " ".join(f"{k}={v}" for k, v in kw.items() if k != "name"))

    def _on_app_data(self, data: bytes) -> None:
        try:
            msg_id, payload = parse_wire_message(data)  # incl. the id rule
        except ValueError as e:
            self.wire_dropped += 1  # "Failed to parse WireHeader2." -> dropped, link stays up
            self.log(f"[fake] dropped DTLS app record ({e}): {data[:16].hex()}")
            return
        if msg_id == WIRE_ID_RTP:
            self._on_rtp(parse_rtp(payload))
        elif msg_id in WIRE_IDS_DC_RPC:
            if self.dc_state == "connecting":  # no read callback registered yet: dropped
                self.early_rpc_records += 1
                self.log(f"[fake] RPC record before OnConnected ({self.early_rpc}): {payload[:16].hex()}")
                return
            self._on_rpc(payload)
        else:
            self.log(f"[fake] record with id {msg_id:#06x}: no reader on the display link")

    # ---- post-DTLS: OnConnected -> ResolveDiscovery -> ACCEPT_COMMANDS ----
    async def _on_connected(self) -> None:
        await self.endpoint.connected.wait()
        await asyncio.sleep(self.on_connected_delay)
        if self.early_rpc == "stall" and self.early_rpc_records:
            self.log("[fake] display RPC arrived before OnConnected: stalled (no ResolveDiscovery)")
            return
        self.dc_state = "not_ready"  # the 0xFFFA reader is registered
        self.resolve_status, _ = await self.resolve()  # S/UNREGISTERING on-enter
        if self.resolve_status != 200:
            return
        await asyncio.sleep(self.handoff_delay)
        self._accept_commands()  # UNREGISTERED -> IDLE -> on-exit STARTED

    def _accept_commands(self) -> None:
        """AcceptCommands() (DC event 4), then the DC goes to the app,
        which answers the GetControllerState requests it had queued."""
        self.dc_state = {"not_ready": "waiting_gcs", "waiting_accept": "ready"}.get(self.dc_state, self.dc_state)
        self.app_has_dc = True
        for ticket in self._gcs_queue:
            self._send_controller_state(ticket)
        self._gcs_queue.clear()
        if self.dc_state == "ready":
            self.display_ready.set()

    def _send_controller_state(self, ticket: int) -> None:
        p = self.protos
        reply = p.ControllerToDisplay()
        reply.controller_state.device_id = self.device_id
        reply.controller_state.f20 = 1
        self.endpoint.send_wire(WIRE_ID_DC_RPC_CONTROLLER, p.rpc_response(ticket, reply))

    def _on_get_controller_state(self, ticket: int) -> None:
        """Event 3: 6 NOT_READY -> 8, 7 WAITING_GET_CONTROLLER_STATE -> 9,
        9 READY answers again; in 8 there is no handler and the responder
        destructor replies INTERNAL."""
        p = self.protos
        if self.dc_state == "waiting_accept":
            self.endpoint.send_wire(
                WIRE_ID_DC_RPC_CONTROLLER,
                p.rpc_response(ticket, code=p.enum("gotham.rpc.StatusCode", "INTERNAL"), message=""),
            )
            return
        if self.app_has_dc:
            self._send_controller_state(ticket)
        else:
            self._gcs_queue.append(ticket)
        self.dc_state = {"not_ready": "waiting_accept", "waiting_gcs": "ready"}.get(self.dc_state, self.dc_state)
        if self.dc_state == "ready":
            self.display_ready.set()

    def _on_rpc(self, data: bytes) -> None:
        """display_connection.cc in miniature: GetControllerStateRequest is answered with
        ControllerToDisplay #2 by the state machine above; complete
        our own pending calls."""
        p = self.protos
        rpc = p.parse_rpc(data, p.DisplayToController)
        self.rpc_in.append(rpc)
        if rpc["kind"] == "request":
            msg = rpc["payload"]
            if rpc["ticket"] is None or msg is None:
                return  # "Received RPC message without ticket/payload, dropping"
            if msg.HasField("get_controller_state"):
                self._on_get_controller_state(rpc["ticket"])
            elif msg.HasField("start_game") and self.dc_state == "ready":
                self._on_start_game(rpc["ticket"], msg.start_game)
            else:
                self.endpoint.send_wire(
                    WIRE_ID_DC_RPC_CONTROLLER,
                    p.rpc_response(
                        rpc["ticket"],
                        code=p.enum("gotham.rpc.StatusCode", "UNIMPLEMENTED"),
                        message="Unimplemented RPC in ControllerMessage",
                    ),
                )
        elif rpc["kind"] == "response":
            fut = self._rpc_pending.pop(rpc["ticket"], None)
            if fut is not None and not fut.done():
                fut.set_result(rpc)

    def _on_start_game(self, ticket: int, req) -> None:
        """READY dispatcher slot 5 -> app event 0xb START_GAME: BROWSE moves to
        STARTING_GAME and holds the reply (app+0x617c) until GAME_STARTED / GAME_STOPPED."""
        self.start_game_requests.append({"ticket": ticket, "key": req.value, "number": req.number})
        if self.app_state != "browse":  # only BROWSE handles START_GAME: refused at once
            self.endpoint.send_wire(
                WIRE_ID_DC_RPC_CONTROLLER,
                self.protos.rpc_response(
                    ticket, code=self.protos.enum("gotham.rpc.StatusCode", "FAILED_PRECONDITION"), message=""
                ),
            )
            return
        self.app_state = "starting_game"
        self._start_game_held = ticket
        if self.game_mode:
            self._game_task = asyncio.get_running_loop().create_task(self._run_game_session(req.value))

    async def _run_game_session(self, key: str) -> None:
        """StreamingClient::Start -> InitializationRequest -> BeginStreaming (new UDP flow,
        STUN, DTLS client) -> OnSessionConnected -> GAME_STARTED answers StartGame."""
        try:
            await self.send_initialization_request(key)
            await self.connect_game()
        except Exception as e:
            self.game_error = repr(e)
            self.log(f"[fake] game session failed: {e!r}")
            self.finish_start_game(ok=False, message=str(e))
            return
        self.finish_start_game(ok=True)

    def finish_start_game(self, ok: bool = True, message: str = "") -> bool:
        """GAME_STARTED (OK) or GAME_STOPPED (error) answers the held StartGame request."""
        if self._start_game_held is None:
            return False
        p = self.protos
        code = 0 if ok else p.enum("gotham.rpc.StatusCode", "UNAVAILABLE")
        ticket, self._start_game_held = self._start_game_held, None
        self.app_state = "in_game" if ok else "browse"
        return self.endpoint.send_wire(WIRE_ID_DC_RPC_CONTROLLER, p.rpc_response(ticket, code=code, message=message))

    # ---- game phase: StreamingClient over the display link ---------------------------
    def _new_game_endpoint(self) -> RtcEndpoint:
        if self.game_endpoint is not None:
            self.game_endpoint.close()
        self.game_endpoint = RtcEndpoint(
            cert=DtlsCertificate("fake-game-session"),
            controlling=True,
            dtls_role="client",
            log=self._rtc_log,
            on_app_data=self._on_game_app_data,
            ice_ufrag=random_ice_string(4),
            name="fake-game",
        )
        return self.game_endpoint

    def build_game_offer(self, ep: RtcEndpoint):
        """GenerateInitializationRequest's RtcSdpOffer: the game endpoint's
        credentials and per-session fingerprint."""
        p = self.protos
        o = p.RtcSdpOffer()
        params = ep.local_params()
        o.rtc_session_id = str(secrets.randbits(40))
        o.rtc_session_version = "2"
        o.ice_ufrag, o.ice_password, o.fingerprint = params["ice_ufrag"], params["ice_password"], params["fingerprint"]
        o.cname = "fakegame"
        o.audio_ssrc = 3333
        vf = o.video_format.add()
        vf.codec = p.enum(f"{PKG_API}.RtcSdpVideoCodec", "VIDEO_CODEC_VP9")
        af = o.audio_format.add()
        af.codec, af.channels, af.sample_rate = p.enum(f"{PKG_API}.RtcSdpAudioCodec", "AUDIO_CODEC_OPUS"), 2, 48000
        o.network_type = p.enum(f"{PKG_API}.RtcSdpNetworkType", "NETWORK_TYPE_WIFI")
        o.raw_dtls = self.raw_dtls
        return o

    async def send_initialization_request(self, key: str | None = None, timeout: float = 3.0) -> dict:
        """SendInitializationRequest: StreamerRequest{InitializationRequest}
        in an inner RpcMessage request, carried by ControllerToDisplay #1 ProxyRequest
        {1: 1, 2: the StartGame key, 4: body}; the display's answer is applied like
        HandleInitializationResponse does."""
        p = self.protos
        ep = self._new_game_endpoint()
        await ep.start(self.bind_ip, 0)
        sreq = p.StreamerRequest()
        init = sreq.initialization
        init.version = 1
        init.offer = self.build_game_offer(ep).SerializeToString()
        init.f3, init.f4 = 7, "fake-f4"
        inner_ticket = self._streamer_ticket  # the StreamingClient's RPC core starts at 0 (ticket = next++)
        self._streamer_ticket += 1
        msg = p.ControllerToDisplay()
        pr = msg.proxy_request
        pr.f1 = 1
        pr.f2 = key or (self.start_game_requests[-1]["key"] if self.start_game_requests else "fake-game")
        pr.body = p.rpc_request(inner_ticket, sreq)
        rpc = await self.rpc_call(msg, timeout)
        return self._apply_initialization_response(rpc, inner_ticket, ep)

    def _apply_initialization_response(self, rpc: dict, inner_ticket: int, ep: RtcEndpoint) -> dict:
        p = self.protos
        if rpc["code"]:
            raise RuntimeError(f"Proxy request failed: status {rpc['code']} {rpc['error_message']!r}")
        out = rpc["payload"]
        if out is None:
            raise RuntimeError("Null proxy response")
        if not out.HasField("proxy_response"):
            raise RuntimeError("Payload type is not proxy response")
        if not out.proxy_response.HasField("data"):
            raise RuntimeError("Null payload")
        if not out.proxy_response.data:
            raise RuntimeError("Empty proxy response from display client")
        inner = p.parse_rpc(out.proxy_response.data, p.StreamerResponse)
        if inner["kind"] != "response" or inner["ticket"] != inner_ticket:
            raise RuntimeError(
                f"inner RpcMessage: {inner['kind']} ticket {inner['ticket']}, want response {inner_ticket}"
            )
        if inner["code"]:
            raise RuntimeError(f"InitializationRequest failed: status {inner['code']} {inner['error_message']!r}")
        if inner["payload"] is None or not inner["payload"].HasField("initialization"):
            raise RuntimeError("StreamerResponse without InitializationResponse")
        resp = inner["payload"].initialization
        answer, cand = p.RtcSdpAnswer(), p.RtcIceCandidate()
        answer.ParseFromString(resp.answer)
        cand.ParseFromString(resp.candidate)
        for obj, limits in ((answer, GAME_ANSWER_LIMITS), (cand, GAME_CANDIDATE_LIMITS)):
            for name, n in limits.items():
                if len(getattr(obj, name).encode()) > n:
                    raise RuntimeError(f"decode failed: string overflow ({name} > {n})")
        try:
            ip = str(ipaddress.ip_address(cand.local_endpoint_ip))  # an IP literal
        except ValueError:
            raise RuntimeError("Invalid endpoint ip") from None
        if not 0 < cand.local_endpoint_port < 65536:
            raise RuntimeError("Invalid endpoint port")
        ep.set_remote(answer.ice_ufrag, answer.ice_password, answer.fingerprint)
        self.game_answer, self.game_candidate = answer, (ip, cand.local_endpoint_port)
        return {"rpc": rpc, "inner": inner, "answer": answer, "candidate": cand}

    async def connect_game(self, timeout: float = 5.0) -> None:
        """BeginStreaming: STUN to the answered candidate, then DTLS as client."""
        ep = self.game_endpoint
        if ep is None or self.game_candidate is None:
            raise RuntimeError("no InitializationResponse applied")
        ep.add_remote_candidate(*self.game_candidate)
        await asyncio.wait_for(ep.connected.wait(), timeout)

    def send_game_input(self, **kw) -> int:
        """An InputProtocolMessage on the game session's sub-channel 0x66."""
        payload = self.gamepad_payload(**kw)
        ep = self.game_endpoint
        if self.game_framing == "raw":
            ok = ep.send_wire(WIRE_ID_RTP, payload)
        else:
            ok = ep.send_rtp_wire(payload, payload_type=96, marker=True, extensions=[(3, self.seq.to_bytes(2, "big"))])
        if not ok:
            raise RuntimeError("game session not connected")
        return self.seq

    def send_game_rpc(self, wire_id: int, ticket: int = 1) -> bool:
        """An RpcRequest (empty payload) on the game session's RPC channel."""
        return self.game_endpoint.send_wire(wire_id, self.protos.rpc_request(ticket, b""), strict_id=False)

    async def wait_game_ack(self, timeout: float = 3.0) -> int:
        return await asyncio.wait_for(self.game_acks.get(), timeout)

    def _set_motors(self, left: int, right: int) -> None:
        if self.on_motors and (left, right) != self.motors:
            self.on_motors(left, right, "write")
        self.motors = (left, right)
        self.motor_writes += 1
        if self._watchdog_handle is not None:
            self._watchdog_handle.cancel()
            self._watchdog_handle = None
        if left or right:
            self._watchdog_handle = asyncio.get_running_loop().call_later(self.haptics_watchdog, self._watchdog_fired)

    def _watchdog_fired(self) -> None:
        self._watchdog_handle = None
        self.watchdog_expired += 1
        self.motors = (0, 0)
        if self.on_motors:
            self.on_motors(0, 0, "watchdog")
        self.log("[fake] Haptics watchdog expired: Stopping motor")

    def _on_game_app_data(self, data: bytes) -> None:
        self.game_app_in.append(data)
        try:
            msg_id, payload = parse_wire_message(data, strict_id=False)
        except ValueError:
            return
        if msg_id == WIRE_ID_RTP:
            # strict: the 0x66 reader takes the framing this session uses, nothing else
            looks_rtp = len(payload) >= 12 and payload[0] >> 6 == 2
            if looks_rtp != (self.game_framing == "rtp"):
                self.game_framing_errors += 1
                self.log(f"[fake] 0x66 message in the wrong framing (session uses {self.game_framing}): dropped")
                return
            self.game_rx_framing.append(self.game_framing)
            if looks_rtp:
                payload = parse_rtp(payload).payload
            m = self.protos.InputProtocolMessage()
            m.ParseFromString(payload)
            if m.HasField("ack_sequence_number"):
                self.game_acks.put_nowait(m.ack_sequence_number)
            if m.HasField("input_message_series"):
                self.game_feedback_seqs.append(m.input_message_series.sequence_number)
            for slim in m.input_message_series.messages:
                if slim.HasField("input_feedback_message_v2"):
                    fb = slim.input_feedback_message_v2
                    self.game_feedback.append(fb)
                    if fb.HasField("gamepad_vibration"):  # game_feedback_relay.cc -> haptics
                        self._set_motors(fb.gamepad_vibration.left_motor_speed, fb.gamepad_vibration.right_motor_speed)
        else:
            rpc = self.protos.RpcMessage()
            rpc.ParseFromString(payload)
            self.game_rpc_in.append((msg_id, self.protos.to_dict(rpc)))

    def send_input_rpc(
        self, buttons: int = 0, lx: int = 0, ly: int = 0, rx: int = 0, ry: int = 0, lt: int = 0, rt: int = 0
    ) -> bool:
        """DisplayConnection::HandleInput: the input state as an RpcNotification carrying
        ControllerToDisplay #3 (f1 buttons, f2-f5 sticks, f6/f7 triggers), READY only."""
        p = self.protos
        msg = p.ControllerToDisplay()
        m = msg.message3
        m.f1, m.f2, m.f3, m.f4, m.f5, m.f6, m.f7 = buttons, lx, ly, rx, ry, lt, rt
        self._rpc_ticket += 1
        return self.endpoint.send_wire(WIRE_ID_DC_RPC_CONTROLLER, p.rpc_notification(self._rpc_ticket, msg))

    async def rpc_call(self, msg, timeout: float = 3.0) -> dict:
        """Send a ControllerToDisplay as an RpcRequest and wait for the display's response."""
        self._rpc_ticket += 1
        fut = asyncio.get_running_loop().create_future()
        self._rpc_pending[self._rpc_ticket] = fut
        if not self.endpoint.send_wire(WIRE_ID_DC_RPC_CONTROLLER, self.protos.rpc_request(self._rpc_ticket, msg)):
            raise RuntimeError("no DTLS session")
        return await asyncio.wait_for(fut, timeout)

    async def get_current_time(self) -> dict:
        """READY's periodic GetCurrentTimeRequest (#18) and the checks
        HandleGetDisplayTimeResponse / remote_clock.cc apply to the answer."""
        p = self.protos
        req = p.ControllerToDisplay()
        req.get_current_time.controller_time_us = t0 = int(time.time() * 1_000_000)
        rpc = await self.rpc_call(req)
        if rpc["code"] != 0:
            raise RuntimeError(f"Display time update failed: status {rpc['code']} {rpc['error_message']!r}")
        if rpc["payload"] is None:
            raise RuntimeError("Display time update failed: missing payload")
        if not rpc["payload"].HasField("current_time"):
            raise RuntimeError("Display time update failed: unexpected payload type")
        cur = rpc["payload"].current_time
        rtt = int(time.time() * 1_000_000) - cur.controller_time_us
        if rtt < 0:
            raise RuntimeError("remote_clock.cc: CHECK failed (negative RTT)")
        return {"t0": t0, "echo": cur.controller_time_us, "display_time_us": cur.display_time_us, "rtt_us": rtt}

    async def keepalive(self) -> dict:
        """READY's 1.5 s keepalive request: an empty ControllerToDisplay #14."""
        req = self.protos.ControllerToDisplay()
        req.keepalive.SetInParent()
        return await self.rpc_call(req)

    def _on_rtp(self, pkt):
        self.rtp_in.put_nowait(pkt)
        try:
            m = self.protos.InputProtocolMessage()
            m.ParseFromString(pkt.payload)
            if m.HasField("ack_sequence_number"):
                self.acks.put_nowait(m.ack_sequence_number)
        except Exception:
            pass

    # ---- HTTP/1.1 client shaped like the firmware's -------------------------
    async def request(
        self,
        method: str,
        hostname: str,
        path: str,
        body: bytes = b"",
        content_type: str | None = None,
        headers: list | None = None,
        port: int | None = None,
        tls: bool = True,
    ):
        port = port or self.https_port
        ctx = None
        if tls:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        reader, writer = await asyncio.open_connection(
            self.host, port, ssl=ctx, server_hostname=hostname if tls else None
        )
        lines = [f"{method} {path} HTTP/1.1", f"Host: {hostname}:{port}"]
        if content_type:
            lines.append(f"Content-Type: {content_type}")
        for k, v in headers or []:
            lines.append(f"{k}: {v}")
        lines.append(f"Content-Length: {len(body)}")
        lines.append("Connection: close")
        writer.write(("\r\n".join(lines) + "\r\n\r\n").encode() + body)
        await writer.drain()
        head = await reader.readuntil(b"\r\n\r\n")
        status_line, *hdr_lines = head.decode("latin-1").split("\r\n")
        status = int(status_line.split(" ")[1])
        hdrs = {}
        for ln in hdr_lines:
            if ":" in ln:
                k, _, v = ln.partition(":")
                hdrs[k.strip().lower()] = v.strip()
        clen = int(hdrs.get("content-length", "0") or 0)
        resp_body = await reader.readexactly(clen) if clen else b""
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return status, hdrs, resp_body

    async def boot(self) -> dict:
        """The controller's boot-time sequence, plus the two RPCs."""
        out: dict = {}
        st, _, _ = await self.request("GET", "connectivitycheck.gstatic.com", "/generate_204")
        out["generate_204"] = st
        st, _, body = await self.request("GET", "clients3.google.com", "/cast/chromecast/device/stadiaconfig?d=fake")
        out["stadiaconfig"] = (st, len(body))
        st, _, body = await self.request(
            "POST",
            "update.googleapis.com",
            "/service/update2",
            OMAHA_REQUEST.encode(),
            "text/xml",
            [("X-Goog-Update-Updater", "CarawayUpdater-0.1.1.0")],
        )
        out["omaha"] = (st, body.decode("utf-8", "replace"))
        batch = self.protos.BatchedLogRequest()
        lr = batch.log_request.add()
        lr.log_source = 485
        lr.client_info.client_type = 11
        lr.client_info.yeti_client_info.build_label = "fake-282115"
        ev = lr.log_event.add()
        ev.event_time_ms = int(time.time() * 1000)
        ev.event_code = 1
        st, _, body = await self.request(
            "POST", "play.googleapis.com", "/log/batch", batch.SerializeToString(), "application/octet-stream"
        )
        lresp = self.protos.LogResponse()
        lresp.ParseFromString(body)
        out["clearcut"] = (st, lresp.next_request_wait_millis)
        p = self.protos
        fvc = p.FirmwareVersionCheckRequest()
        fvc.version = "282115"
        st, _, body = await self.request(
            "POST",
            "cloudcast-pa.googleapis.com",
            "/v1:FirmwareVersionCheck?key=FAKEKEY",
            fvc.SerializeToString(),
            "application/x-protobuf",
            self.rpc_headers(),
        )
        fvr = p.FirmwareVersionCheckResponse()
        fvr.ParseFromString(body)
        out["firmware_version_check"] = (st, fvr.requires_update)
        reg = p.RegisterControllerRequest()
        reg.device_id, reg.cloud_device_id, reg.attempt_id = self.device_id, self.cloud_device_id, self.attempt_id
        reg.client_flag = False
        st, _, body = await self.request(
            "POST",
            "cloudcast-pa.googleapis.com",
            "/v1:RegisterController?key=FAKEKEY",
            reg.SerializeToString(),
            "application/x-protobuf",
            self.rpc_headers(),
        )
        self.registration = p.RegisterControllerResponse()
        self.registration.ParseFromString(body)  # DecodeError when the required poll_interval_ms is missing
        if self.registration.poll_interval_ms <= 0:
            raise RuntimeError("Missing required field(s): poll_interval_ms must be > 0")
        if self.registration.HasField("display"):  # autolinking boot: the display token comes with the registration
            self.display_token = self.registration.display.display_token
        out["register_controller"] = (st, p.to_dict(self.registration))
        return out

    # ---- firmware flow: offer in DiscoverDisplay, answer via GMsg --------------
    def rpc_headers(self) -> list:
        return [("controller-device-id", self.device_id), ("controller-cloud-device-id", self.cloud_device_id)]

    def candidate_message(self):
        p = self.protos
        msg = p.WebRtcSdp()
        msg.type = p.ICE_CANDIDATE
        c = msg.ice_candidate
        c.sdp_mid, c.sdp_mline_index, c.foundation, c.priority = "0", 0, "1", HOST_CANDIDATE_PRIORITY
        c.local_endpoint_ip = self.bind_ip if self.bind_ip not in ("0.0.0.0", "") else "127.0.0.1"
        c.local_endpoint_port = self.endpoint.local_port
        return msg

    def offer_bundle(self) -> bytes:
        """DiscoverDisplayRequest.sdp_offer_bundle: WebRtcSdpAndIceCandidates{sdp: offer, ice_candidate: [ours]}."""
        b = self.protos.WebRtcSdpAndIceCandidates()
        b.sdp.CopyFrom(self.build_offer())
        b.ice_candidate.append(self.candidate_message())
        return b.SerializeToString()

    async def discover(self, keys=(1, 2, 3, 4)):
        p = self.protos
        req = p.DiscoverDisplayRequest()
        req.device_id, req.attempt_id, req.app_id = self.device_id, self.attempt_id, "stadia"
        req.typed_keys.key.extend(keys)
        req.sdp_offer_bundle = self.offer_bundle()
        req.client_flag = False
        st, _, body = await self.request(
            "POST",
            "cloudcast-pa.googleapis.com",
            "/v1:DiscoverDisplay?key=FAKEKEY",
            req.SerializeToString(),
            "application/x-protobuf",
            self.rpc_headers(),
        )
        if st != 200:
            raise RuntimeError(f"DiscoverDisplay: HTTP {st}")
        if not body:
            raise RuntimeError("No response data.")
        self.discovery = p.DiscoverDisplayResponse()
        self.discovery.ParseFromString(body)
        if not self.discovery.display_token:
            raise RuntimeError("Missing required field(s): display_token")
        self.display_token = self.discovery.display_token
        return self.discovery

    async def resolve(self):
        p = self.protos
        req = p.ResolveDiscoveryRequest()
        req.device_id = self.device_id
        req.display_id = self.discovery.display_id if self.discovery else ""
        req.flag3 = 0
        st, _, body = await self.request(
            "POST",
            "cloudcast-pa.googleapis.com",
            "/v1:ResolveDiscovery?key=FAKEKEY",
            req.SerializeToString(),
            "application/x-protobuf",
            self.rpc_headers(),
        )
        return st, body

    async def poll_gmsg(self):
        """One GMsg poll.  Returns (None, None) on an empty body (firmware: event 10,
        poll again) or (GmsgEnvelope, WebRtcSdpAndIceCandidates) and remembers the id
        so the next poll acks it."""
        p = self.protos
        poll = p.GmsgPollRequest()
        # The firmware polls with the display token it was given (confirmed live); before any discovery
        # it has only the registration token.
        poll.subscription = self.display_token or (
            self.registration.registration_token if self.registration else "unregistered"
        )
        if self.last_gmsg_id is not None:
            poll.last_message.id = self.last_gmsg_id
            poll.ack = True
        st, _, body = await self.request(
            "POST", "cloudcast-gmsg-prod.googleapis.com", "/gmsg", poll.SerializeToString(), "application/x-protobuf"
        )
        if st != 200:
            raise RuntimeError(f"gmsg: HTTP {st}")
        if not body:
            return None, None
        env = p.GmsgEnvelope()
        env.ParseFromString(body)
        if env.message.type != 42:
            raise RuntimeError(f"GmsgMessage.type={env.message.type}, firmware wants 42")
        cmd = p.gmsg_command(env)
        if cmd is None or cmd.HasField("error_code"):
            raise RuntimeError("GMsg cmd returns error.")
        self.last_gmsg_id = env.message.id
        bundle = p.parse_sdp_bundle(cmd.payload)
        self._take_candidates(bundle.ice_candidate)
        if bundle.sdp.type == p.SDP_ANSWER:
            a = bundle.sdp.sdp_answer
            self.endpoint.set_remote(a.ice_ufrag, a.ice_password, a.fingerprint)
        return env, bundle

    # ---- signaling, controller answers (firmware-evidence direction) --------
    async def fetch_offer(self):
        """Legacy (receiver --signaling-mode offer): the receiver's offer bundle arrives
        as the GMsg payload."""
        _env, bundle = await self.poll_gmsg()
        if bundle is None or bundle.sdp.type != self.protos.SDP_OFFER:
            raise RuntimeError("no offer in the GMsg response")
        return bundle

    def _take_candidates(self, msgs) -> None:
        for c in msgs:
            if c.type == self.protos.ICE_CANDIDATE and c.ice_candidate.local_endpoint_ip:
                cand = (c.ice_candidate.local_endpoint_ip, c.ice_candidate.local_endpoint_port)
                if cand not in self.remote_candidates:
                    self.remote_candidates.append(cand)

    def build_answer(self, offer):
        p = self.protos
        a = p.RtcSdpAnswer()
        params = self.endpoint.local_params()
        a.ice_ufrag, a.ice_password, a.fingerprint = params["ice_ufrag"], params["ice_password"], params["fingerprint"]
        a.use_boreal = offer.boreal_available
        a.boreal_transport = p.enum(f"{PKG_API}.BorealTransportProtocol", "BOREAL_SRTP")
        if offer.video_format:
            a.video_format.CopyFrom(offer.video_format[0])
        if offer.audio_format:
            a.audio_format.CopyFrom(offer.audio_format[0])
        a.client_type = 0
        a.build_number = "fake-controller-282115"
        a.network_type = p.enum(f"{PKG_API}.RtcSdpNetworkType", "NETWORK_TYPE_WIFI")
        a.use_rtp_timestamps = offer.use_rtp_timestamps
        msg = p.WebRtcSdp()
        msg.type = p.SDP_ANSWER
        msg.sdp_answer.CopyFrom(a)
        return msg

    async def send_answer(self, bundle, path: str = "/signaling", nested: bool = True):
        offer = bundle.sdp.sdp_offer
        answer = self.build_answer(offer)
        raw = answer.SerializeToString()
        if nested:  # wrap in an unknown envelope, like a real RPC request would
            raw = b"\x0a\x04ctrl" + b"\x22" + _varint(len(raw)) + raw
        st, _, body = await self.request("POST", "cloudcast-pa.googleapis.com", path, raw, "application/x-protobuf")
        if st != 200:
            raise RuntimeError(f"answer post: HTTP {st}")
        if body:
            m = self.protos.WebRtcSdp()
            m.ParseFromString(body)
            self._take_candidates([m])
        self.endpoint.set_remote(offer.ice_ufrag, offer.ice_password, offer.fingerprint)
        return answer

    # ---- signaling, controller offers (task-brief direction) -----------------
    def build_offer(self):
        p = self.protos
        o = p.RtcSdpOffer()
        params = self.endpoint.local_params()
        o.rtc_session_id = str(secrets.randbits(40))
        o.rtc_session_version = "2"
        o.ice_ufrag, o.ice_password, o.fingerprint = params["ice_ufrag"], params["ice_password"], params["fingerprint"]
        o.cname = "fakecname"
        o.audio_ssrc, o.video_ssrc = 1111, 2222
        o.boreal_available = True
        o.boreal_supported_transports.append(p.enum(f"{PKG_API}.BorealTransportProtocol", "BOREAL_SRTP"))
        vf = o.video_format.add()
        vf.codec = p.enum(f"{PKG_API}.RtcSdpVideoCodec", "VIDEO_CODEC_VP9")
        af = o.audio_format.add()
        af.codec, af.channels, af.sample_rate = p.enum(f"{PKG_API}.RtcSdpAudioCodec", "AUDIO_CODEC_OPUS"), 2, 48000
        o.network_type = p.enum(f"{PKG_API}.RtcSdpNetworkType", "NETWORK_TYPE_WIFI")
        if self.raw_dtls:
            o.raw_dtls = o.delayed_acks = True
        msg = p.WebRtcSdp()
        msg.type = p.SDP_OFFER
        msg.sdp_offer.CopyFrom(o)
        return msg

    async def post_offer(self, path: str = "/signaling"):
        offer = self.build_offer()
        st, _, body = await self.request(
            "POST", "cloudcast-pa.googleapis.com", path, offer.SerializeToString(), "application/x-protobuf"
        )
        if st != 200:
            raise RuntimeError(f"offer post: HTTP {st}")
        bundle = self.protos.WebRtcSdpAndIceCandidates()
        bundle.ParseFromString(body)
        if bundle.sdp.type != self.protos.SDP_ANSWER:
            raise RuntimeError("no answer in response")
        a = bundle.sdp.sdp_answer
        self._take_candidates(bundle.ice_candidate)
        self.endpoint.set_remote(a.ice_ufrag, a.ice_password, a.fingerprint)
        return bundle

    # ---- media ----------------------------------------------------------------
    async def start_udp(self) -> int:
        return await self.endpoint.start(self.bind_ip, 0)

    def connect(self) -> None:
        for ip, port in self.remote_candidates:
            self.endpoint.add_remote_candidate(ip, port)
        if self.raw_dtls and self._on_connected_task is None:
            self._on_connected_task = asyncio.get_running_loop().create_task(self._on_connected())

    async def wait_connected(self, timeout: float = 5.0) -> None:
        await asyncio.wait_for(self.endpoint.connected.wait(), timeout)

    def gamepad_payload(self, *, gid=0, buttons=0, lx=0, ly=0, rx=0, ry=0, lt=0, rt=0, compressed=False) -> bytes:
        p = self.protos
        self.seq += 1
        series = p.InputMessageSeries()
        series.session_id = self.session_id
        series.sequence_number = self.seq
        series.base_timestamp_us = int(time.monotonic() * 1e6)
        slim = series.messages.add()
        slim.relative_time_us = 0
        g = slim.gamepad_message_v2
        g.id, g.buttons = gid, buttons
        g.axis_left_x, g.axis_left_y, g.axis_right_x, g.axis_right_y = lx, ly, rx, ry
        g.trigger_left, g.trigger_right = lt, rt
        msg = p.InputProtocolMessage()
        if compressed:
            raw = series.SerializeToString()
            msg.compressed_series = zlib.compress(raw)
            msg.uncompressed_size = len(raw)
        else:
            msg.input_message_series.CopyFrom(series)
        return msg.SerializeToString()

    def send_gamepad(self, pt: int = 96, **kw) -> int:
        payload = self.gamepad_payload(**kw)
        ext = [(3, self.seq.to_bytes(2, "big"))]  # client-input-sequence (id 3 in the default offer)
        send = self.endpoint.send_rtp_wire if self.raw_dtls else self.endpoint.send_rtp
        if not send(payload, payload_type=pt, marker=True, extensions=ext):
            raise RuntimeError("not connected")
        return self.seq

    async def wait_ack(self, timeout: float = 3.0) -> int:
        return await asyncio.wait_for(self.acks.get(), timeout)

    def close(self) -> None:
        for task in (self._on_connected_task, self._game_task):
            if task is not None:
                task.cancel()
        if self._watchdog_handle is not None:
            self._watchdog_handle.cancel()
        self.endpoint.close()
        if self.game_endpoint is not None:
            self.game_endpoint.close()


def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


async def run_cli(args) -> int:
    we_offer = args.mode in ("firmware", "bare-offer")
    fc = FakeController(
        args.host,
        args.https_port,
        bind_ip=args.bind_ip,
        dtls_role="client" if we_offer else "server",
        controlling=we_offer,
        raw_dtls=args.mode == "firmware",
        game_mode=args.game > 0,
        game_framing=args.game_framing,
        haptics_watchdog=args.haptics_watchdog,
    )
    t0 = time.monotonic()
    fc.on_motors = lambda left, right, why: print(
        f"[{time.monotonic() - t0:7.3f}] motors left={left} right={right} ({why})", flush=True
    )
    print("boot:", await fc.boot())
    await fc.start_udp()
    if args.mode == "firmware":
        print("DiscoverDisplayResponse:", fc.protos.to_dict(await fc.discover()))
        for _ in range(20):
            env, bundle = await fc.poll_gmsg()
            if bundle is not None:
                print(f"GMsg #{env.message.id}: answer from receiver:", fc.protos.to_dict(bundle.sdp.sdp_answer))
                break
            await asyncio.sleep(0.5)
        else:
            raise RuntimeError("no GMsg answer within 10 s")
        await fc.poll_gmsg()  # acks the message
    elif args.mode == "receiver-offers":
        bundle = await fc.fetch_offer()
        print("offer from receiver (GMsg):", fc.protos.to_dict(bundle.sdp.sdp_offer))
        await fc.send_answer(bundle)
    else:
        bundle = await fc.post_offer()
        print("answer from receiver:", fc.protos.to_dict(bundle.sdp.sdp_answer))
    print("candidates:", fc.remote_candidates)
    fc.connect()
    await fc.wait_connected(10)
    print("DTLS-SRTP connected:", fc.endpoint.srtp_in.profile)
    if fc.raw_dtls:
        await asyncio.wait_for(fc.display_ready.wait(), 10)
        print(f"ResolveDiscovery after DTLS: HTTP {fc.resolve_status}; GetControllerStateRequest answered -> READY")
        print("GetCurrentTimeRequest ->", await fc.get_current_time())
        print("keepalive -> code", (await fc.keepalive())["code"])
    if fc.raw_dtls and args.game > 0:
        return await run_game(fc, args, t0)
    for buttons in (0x10, 0x30, 0x20, 0x0):
        seq = fc.send_gamepad(buttons=buttons, lx=-1000 if buttons else 0)
        print(f"sent series {seq} buttons=0x{buttons:x}; ack ->", await fc.wait_ack(3))
        await asyncio.sleep(0.1)
    fc.close()
    return 0


async def run_game(fc: FakeController, args, t0: float) -> int:
    """--game SECONDS: stay in READY (keepalive 1.5 s, time 3 s) waiting for the receiver's
    StartGame, run the game session, send input on it, then keep the session up for
    SECONDS printing every motor change (drive it with POST /_rumble)."""

    async def ready_traffic():
        n = 0
        while True:
            await asyncio.sleep(1.5)
            n += 1
            await fc.keepalive()
            if n % 2 == 0:
                await fc.get_current_time()

    traffic = asyncio.get_running_loop().create_task(ready_traffic())
    try:
        for _ in range(200):
            if fc.app_state != "browse" and fc.start_game_requests:
                break
            await asyncio.sleep(0.1)
        else:
            raise RuntimeError("no StartGame from the receiver within 20 s (is --start-game set?)")
        print(f"[{time.monotonic() - t0:7.3f}] StartGame key={fc.start_game_requests[0]['key']!r}", flush=True)
        for _ in range(100):
            if fc.app_state == "in_game" or fc.game_error:
                break
            await asyncio.sleep(0.1)
        if fc.app_state != "in_game":
            raise RuntimeError(f"game session did not start: {fc.game_error}")
        print(
            f"[{time.monotonic() - t0:7.3f}] GAME_STARTED: game session {fc.game_candidate} "
            f"({args.game_framing} framing on 0x66)",
            flush=True,
        )
        for buttons in (0x1000, 0x0):
            seq = fc.send_game_input(buttons=buttons)
            print(
                f"[{time.monotonic() - t0:7.3f}] game input series {seq} buttons=0x{buttons:x}; ack ->",
                await fc.wait_game_ack(3),
                flush=True,
            )
        print(f"[{time.monotonic() - t0:7.3f}] holding the game session for {args.game:g} s", flush=True)
        await asyncio.sleep(args.game)
        print(
            f"[{time.monotonic() - t0:7.3f}] done: {fc.motor_writes} motor writes, "
            f"{fc.watchdog_expired} watchdog stops, {fc.game_framing_errors} framing errors",
            flush=True,
        )
    finally:
        traffic.cancel()
        fc.close()
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--https-port", type=int, default=443)
    ap.add_argument("--bind-ip", default="0.0.0.0")
    ap.add_argument("--mode", choices=("firmware", "receiver-offers", "bare-offer"), default="firmware")
    ap.add_argument(
        "--game",
        type=float,
        default=0,
        metavar="SECONDS",
        help="firmware mode: wait for the receiver's StartGame (run it with --start-game), run the "
        "game session and keep it up this long, printing motor changes (POST /_rumble)",
    )
    ap.add_argument("--game-framing", choices=("rtp", "raw"), default="rtp")
    ap.add_argument("--haptics-watchdog", type=float, default=5.0)
    sys.exit(asyncio.run(run_cli(ap.parse_args())))
