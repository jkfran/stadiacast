"""One UDP socket speaking ICE/STUN, DTLS-SRTP and RTP with a single peer: :class:`RtcEndpoint`.

Role handling is flexible on purpose, because the protobuf signaling carries no ``a=setup`` or
ICE-role attribute:

* STUN: the endpoint answers binding requests and sends its own checks; ``controlling`` decides
  whether it nominates (``USE-CANDIDATE``).
* DTLS: ``dtls_role`` is ``client``, ``server`` or ``auto``.  In ``auto`` the endpoint becomes the
  server when a ClientHello arrives first, otherwise it starts as client ``dtls_client_delay``
  seconds after the ICE pair is verified.

Application callbacks (RTP, RTCP, DTLS application data, state changes) are isolated from the
datagram path: an exception in one is logged and never takes the socket down.
"""

from __future__ import annotations

import asyncio
import secrets
import struct
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, cast

from OpenSSL import SSL

from stadiacast.rtc.dtls import DEFAULT_SRTP_PROFILES, DtlsCertificate, DtlsSession, normalize_fingerprint
from stadiacast.rtc.rtp import RtpPacket, build_rtp, parse_rtp
from stadiacast.rtc.srtp import SrtpContext, SrtpError
from stadiacast.rtc.stun import (
    ATTR_ERROR_CODE,
    ATTR_FINGERPRINT,
    ATTR_ICE_CONTROLLED,
    ATTR_ICE_CONTROLLING,
    ATTR_MESSAGE_INTEGRITY,
    ATTR_PRIORITY,
    ATTR_USE_CANDIDATE,
    ATTR_USERNAME,
    ATTR_XOR_MAPPED_ADDRESS,
    BINDING_ERROR,
    BINDING_INDICATION,
    BINDING_REQUEST,
    BINDING_SUCCESS,
    HOST_CANDIDATE_PRIORITY,
    STUN_MAGIC_BYTES,
    StunMessage,
    random_ice_string,
)
from stadiacast.rtc.wire import WIRE_ID_RTP, pack_wire_message

#: A peer address as asyncio hands it to us.
Addr = tuple[str, int]


def _noop(*_args: Any, **_kwargs: Any) -> None:
    """Default for every optional callback."""


def classify_datagram(data: bytes) -> str:
    """RFC 7983 demultiplexing: stun / dtls / rtp / rtcp / unknown."""
    if len(data) < 2:
        return "unknown"
    b = data[0]
    if b < 4 and len(data) >= 20 and data[4:8] == STUN_MAGIC_BYTES:
        return "stun"
    if 20 <= b <= 63:
        return "dtls"
    if 128 <= b <= 191:
        return "rtcp" if 192 <= data[1] <= 223 else "rtp"
    return "unknown"


@dataclass
class RemotePeer:
    ufrag: str
    pwd: str
    fingerprint: str | None = None
    candidates: list[Addr] = field(default_factory=list)
    selected: Addr | None = None
    nominated: bool = False
    verified_in: bool = False
    verified_out: bool = False


class RtcEndpoint(asyncio.DatagramProtocol):
    """One UDP socket speaking STUN, DTLS-SRTP and RTP with a single peer."""

    def __init__(
        self,
        *,
        cert: DtlsCertificate,
        controlling: bool = True,
        dtls_role: str = "auto",
        log: Callable[..., Any] | None = None,
        on_rtp: Callable[[RtpPacket], None] | None = None,
        on_rtcp: Callable[[bytes], None] | None = None,
        on_app_data: Callable[[bytes], None] | None = None,
        on_state: Callable[[str, dict[str, Any]], None] | None = None,
        ice_ufrag: str | None = None,
        ice_pwd: str | None = None,
        local_ssrc: int | None = None,
        dtls_client_delay: float = 0.5,
        allow_plain_rtp: bool = False,
        name: str = "rtc",
        srtp_profiles: str = DEFAULT_SRTP_PROFILES,
        fast_check_interval: float = 0.0,
        fast_check_window: float = 10.0,
    ) -> None:
        if dtls_role not in ("auto", "client", "server"):
            raise ValueError(dtls_role)
        self.cert = cert
        self.controlling = controlling
        self.dtls_role = dtls_role
        self.log: Callable[..., Any] = log or _noop
        self.on_rtp: Callable[[RtpPacket], None] = on_rtp or _noop
        self.on_rtcp: Callable[[bytes], None] = on_rtcp or _noop
        self.on_app_data: Callable[[bytes], None] = on_app_data or _noop
        self.on_state: Callable[[str, dict[str, Any]], None] = on_state or _noop
        self.ufrag = ice_ufrag or random_ice_string(8)
        self.pwd = ice_pwd or random_ice_string(24)
        self.local_ssrc = local_ssrc or secrets.randbits(32) or 1
        self.dtls_client_delay = dtls_client_delay
        # Our checks to the selected pair go out every 2.5 s (keepalive).  With fast_check_interval > 0
        # they go out that often from pair selection until DTLS starts (for at most fast_check_window
        # seconds): in every logged session the controller's first DTLS flight followed our 2.5 s check
        # by ~0.12 s.
        self.fast_check_interval = fast_check_interval
        self.fast_check_window = fast_check_window
        self._fast_until = 0.0
        self.allow_plain_rtp = allow_plain_rtp
        self.name = name
        self.srtp_profiles = srtp_profiles
        self.tiebreaker = secrets.token_bytes(8)
        self.transport: asyncio.DatagramTransport | None = None
        self.local_port: int | None = None
        self.remote: RemotePeer | None = None
        self.dtls: DtlsSession | None = None
        self.srtp_in: SrtpContext | None = None
        self.srtp_out: SrtpContext | None = None
        self.connected = asyncio.Event()  # set on dtls-connected; recreated in start()
        self.tx_seq = secrets.randbits(16)
        self._checks: dict[bytes, tuple[Addr, float]] = {}  # txid -> (address, sent at)
        self._early_peers: list[Addr] = []
        self._pending_candidates: list[Addr] = []
        self._dtls_start_handle: asyncio.TimerHandle | None = None
        self._dtls_timer_handle: asyncio.TimerHandle | None = None
        self._keepalive_handle: asyncio.TimerHandle | None = None
        self._last_addr: Addr | None = None
        self.stats: dict[str, int] = {
            "stun_in": 0,
            "stun_out": 0,
            "dtls_in": 0,
            "rtp_in": 0,
            "rtcp_in": 0,
            "rtp_out": 0,
            "srtp_errors": 0,
            "unknown": 0,
        }

    # ---- asyncio plumbing ------------------------------------------------
    async def start(self, bind_ip: str = "0.0.0.0", port: int = 0) -> int:
        loop = asyncio.get_running_loop()
        self.connected = asyncio.Event()
        try:
            await loop.create_datagram_endpoint(lambda: self, local_addr=(bind_ip, port))
        except OSError as e:
            raise OSError(f"cannot bind UDP {bind_ip}:{port} ({self.name}): {e}") from e
        assert self.local_port is not None
        return self.local_port

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = cast(asyncio.DatagramTransport, transport)
        self.local_port = transport.get_extra_info("sockname")[1]
        self.log(
            "rtc_listening",
            name=self.name,
            port=self.local_port,
            ufrag=self.ufrag,
            fingerprint=self.cert.fingerprint,
            controlling=self.controlling,
            dtls_role=self.dtls_role,
        )

    def error_received(self, exc: Exception) -> None:
        self.log("rtc_socket_error", name=self.name, error=repr(exc))

    def close(self) -> None:
        for h in (self._dtls_start_handle, self._dtls_timer_handle, self._keepalive_handle):
            if h:
                h.cancel()
        self._dtls_start_handle = self._dtls_timer_handle = self._keepalive_handle = None
        if self.transport:
            self.transport.close()
            self.transport = None

    def _send(self, data: bytes, addr: Addr) -> None:
        if self.transport:
            self.transport.sendto(data, addr)

    # ---- signaling-facing API -------------------------------------------
    def local_params(self) -> dict[str, str]:
        return {"ice_ufrag": self.ufrag, "ice_password": self.pwd, "fingerprint": self.cert.fingerprint}

    def new_local_credentials(self, ufrag_len: int = 8, pwd_len: int = 24) -> dict[str, str]:
        """Fresh ICE ufrag/password for a new session on the same socket (the game
        session's answer allows at most 7 / 31 characters)."""
        self.ufrag = random_ice_string(ufrag_len)
        self.pwd = random_ice_string(pwd_len)
        return self.local_params()

    def set_remote(self, ufrag: str, pwd: str, fingerprint: str | None = None) -> None:
        """New remote description: resets ICE/DTLS/SRTP state."""
        self._reset_media()
        self.remote = RemotePeer(ufrag, pwd, fingerprint)
        self.log("rtc_remote_set", name=self.name, ufrag=ufrag, fingerprint=fingerprint)
        for addr in self._early_peers:
            self.add_remote_candidate(*addr)
        self._early_peers.clear()
        for addr in self._pending_candidates:
            self.add_remote_candidate(*addr)
        self._pending_candidates.clear()

    def clear_remote(self) -> None:
        """Forget the peer (a new boot): stops the keepalive checks to its address."""
        self._reset_media()
        if self._keepalive_handle:
            self._keepalive_handle.cancel()
            self._keepalive_handle = None
        self.remote = None
        self._early_peers.clear()
        self._pending_candidates.clear()

    def add_remote_candidate(self, ip: str, port: int) -> None:
        addr = (ip, int(port))
        if self.remote is None:
            if addr not in self._pending_candidates:
                self._pending_candidates.append(addr)
            self.log("rtc_candidate_pending", name=self.name, ip=ip, port=port)
            return
        if addr not in self.remote.candidates:
            self.remote.candidates.append(addr)
            self.log("rtc_remote_candidate", name=self.name, ip=ip, port=port)
        self._send_check(addr, attempt=0)

    def _reset_media(self) -> None:
        for h in (self._dtls_start_handle, self._dtls_timer_handle):
            if h:
                h.cancel()
        self._dtls_start_handle = self._dtls_timer_handle = None
        self.dtls = None
        self.srtp_in = self.srtp_out = None
        self._checks.clear()
        if self.connected:
            self.connected.clear()

    # ---- STUN --------------------------------------------------------------
    def _send_check(self, addr: Addr, attempt: int = 0) -> None:
        if not self.remote:
            return
        msg = StunMessage(BINDING_REQUEST)
        msg.add(ATTR_USERNAME, f"{self.remote.ufrag}:{self.ufrag}".encode())
        msg.add(ATTR_PRIORITY, struct.pack("!I", HOST_CANDIDATE_PRIORITY))
        if self.controlling:
            msg.add(ATTR_ICE_CONTROLLING, self.tiebreaker)
            msg.add(ATTR_USE_CANDIDATE, b"")
        else:
            msg.add(ATTR_ICE_CONTROLLED, self.tiebreaker)
        self._checks[msg.txid] = (addr, time.monotonic())
        self._send(msg.encode(self.remote.pwd.encode()), addr)
        self.stats["stun_out"] += 1
        if attempt < 7 and not self.remote.verified_out:
            loop = asyncio.get_running_loop()
            loop.call_later(0.5 * (attempt + 1), self._retry_check, addr, attempt + 1)

    def _retry_check(self, addr: Addr, attempt: int) -> None:
        if self.remote and not self.remote.verified_out and addr in self.remote.candidates:
            self._send_check(addr, attempt)

    def _keepalive(self) -> None:
        self._keepalive_handle = None
        if self.remote and self.remote.selected and self.transport:
            self._send_check(self.remote.selected, attempt=99)
            self._keepalive_handle = asyncio.get_running_loop().call_later(self._keepalive_delay(), self._keepalive)

    def _keepalive_delay(self) -> float:
        if self.fast_check_interval > 0 and self.dtls is None and time.monotonic() < self._fast_until:
            return self.fast_check_interval
        return 2.5

    def _schedule_keepalive(self) -> None:
        if self._keepalive_handle is None:
            self._fast_until = time.monotonic() + self.fast_check_window
            self._keepalive_handle = asyncio.get_running_loop().call_later(self._keepalive_delay(), self._keepalive)

    def _handle_stun(self, data: bytes, addr: Addr) -> None:
        self.stats["stun_in"] += 1
        try:
            msg = StunMessage.parse(data)
        except ValueError as e:
            self.log("stun_unparsable", name=self.name, peer=addr, error=str(e), head=data[:32].hex())
            return
        if msg.get(ATTR_FINGERPRINT) is not None and not msg.check_fingerprint():
            self.log("stun_bad_fingerprint", name=self.name, peer=addr)
            return
        if msg.msg_type == BINDING_REQUEST:
            user = msg.get(ATTR_USERNAME) or b""
            if not user.startswith(self.ufrag.encode() + b":"):
                self.log(
                    "stun_request_wrong_username", name=self.name, peer=addr, username=user.decode("utf-8", "replace")
                )
                err = StunMessage(BINDING_ERROR, msg.txid).add(ATTR_ERROR_CODE, b"\0\0\x04\x01Unauthorized")
                self._send(err.encode(None), addr)
                return
            if not msg.check_integrity(self.pwd.encode()):
                self.log("stun_request_bad_integrity", name=self.name, peer=addr)
                return
            resp = StunMessage(BINDING_SUCCESS, msg.txid).add(ATTR_XOR_MAPPED_ADDRESS, StunMessage.xor_address(*addr))
            self._send(resp.encode(self.pwd.encode()), addr)
            self.stats["stun_out"] += 1
            self._last_addr = addr
            if self.remote is None:
                if addr not in self._early_peers:
                    self._early_peers.append(addr)
                self.log(
                    "stun_request_before_remote",
                    name=self.name,
                    peer=addr,
                    remote_ufrag=user.split(b":", 1)[1].decode("utf-8", "replace"),
                )
                return
            first = self.remote.selected is None
            if first:
                self.remote.selected = addr
                self.log("rtc_pair_selected", name=self.name, peer=addr, how="incoming binding request")
                self.on_state("ice-selected", {"peer": addr})
            self.remote.verified_in = True
            if msg.get(ATTR_USE_CANDIDATE) is not None:
                self.remote.nominated = True
            if addr not in self.remote.candidates:
                self.remote.candidates.append(addr)
            if not self.remote.verified_out:
                self._send_check(addr, attempt=99)
            if first:
                self._schedule_keepalive()
            self._maybe_start_dtls()
        elif msg.msg_type == BINDING_SUCCESS:
            pend = self._checks.pop(msg.txid, None)
            if pend is None:
                self.log("stun_unexpected_response", name=self.name, peer=addr)
                return
            if self.remote is None:
                return
            if msg.get(ATTR_MESSAGE_INTEGRITY) is not None and not msg.check_integrity(self.remote.pwd.encode()):
                self.log("stun_response_bad_integrity", name=self.name, peer=addr)
                return
            mapped = None
            xa = msg.get(ATTR_XOR_MAPPED_ADDRESS)
            if xa:
                try:
                    mapped = StunMessage.parse_xor_address(xa)
                except ValueError:
                    pass
            newly = not self.remote.verified_out
            self.remote.verified_out = True
            if self.remote.selected is None:
                self.remote.selected = addr
                self.log("rtc_pair_selected", name=self.name, peer=addr, how="binding response", mapped=mapped)
                self.on_state("ice-selected", {"peer": addr, "mapped": mapped})
                self._schedule_keepalive()
            elif newly:
                self.log(
                    "rtc_pair_verified",
                    name=self.name,
                    peer=addr,
                    rtt_ms=round((time.monotonic() - pend[1]) * 1000, 1),
                    mapped=mapped,
                )
            self._maybe_start_dtls()
        elif msg.msg_type == BINDING_ERROR:
            self._checks.pop(msg.txid, None)
            self.log("stun_error_response", name=self.name, peer=addr, error=msg.error_code())
        elif msg.msg_type == BINDING_INDICATION:
            self.log("stun_indication", name=self.name, peer=addr)
        else:
            self.log("stun_other", name=self.name, peer=addr, msg_type=msg.msg_type)

    # ---- DTLS --------------------------------------------------------------
    def _maybe_start_dtls(self) -> None:
        if self.dtls or not self.remote or self.remote.selected is None:
            return
        if not (self.remote.verified_in or self.remote.verified_out):
            return
        if self.dtls_role == "server" or self._dtls_start_handle is not None:
            return
        delay = 0.0 if self.dtls_role == "client" else self.dtls_client_delay
        self._dtls_start_handle = asyncio.get_running_loop().call_later(delay, self._start_dtls_client)

    def _start_dtls_client(self) -> None:
        self._dtls_start_handle = None
        if self.dtls or not self.remote or not self.remote.selected:
            return
        self.dtls = DtlsSession(self.cert, "client", self.srtp_profiles)
        self.log(
            "dtls_start", name=self.name, role="client", peer=self.remote.selected, stun_out=self.stats["stun_out"]
        )
        self.on_state("dtls-start", {"role": "client"})
        self._send_dtls(self.dtls.start())
        self._arm_dtls_timer()

    def _send_dtls(self, datagrams: list[bytes]) -> None:
        addr = (self.remote.selected if self.remote and self.remote.selected else None) or self._last_addr
        if addr is None:
            return
        for d in datagrams:
            self._send(d, addr)

    def _arm_dtls_timer(self) -> None:
        if self._dtls_timer_handle:
            self._dtls_timer_handle.cancel()
            self._dtls_timer_handle = None
        if self.dtls and not self.dtls.handshake_complete:
            t = self.dtls.timeout()
            if t is not None:
                self._dtls_timer_handle = asyncio.get_running_loop().call_later(max(t, 0.05), self._dtls_timeout)

    def _dtls_timeout(self) -> None:
        self._dtls_timer_handle = None
        if self.dtls and not self.dtls.handshake_complete:
            out = self.dtls.handle_timeout()
            if out:
                self.log("dtls_retransmit", name=self.name, datagrams=len(out))
            self._send_dtls(out)
            self._arm_dtls_timer()

    def _handle_dtls(self, data: bytes, addr: Addr) -> None:
        self.stats["dtls_in"] += 1
        self._last_addr = addr
        if self.dtls is None:
            if data[0] != 22:  # not a handshake record: nothing to attach it to
                self.log("dtls_record_without_session", name=self.name, peer=addr, content_type=data[0])
                return
            if self.dtls_role == "client":
                self.log("dtls_clienthello_but_role_client", name=self.name, peer=addr)
                return
            if self._dtls_start_handle:
                self._dtls_start_handle.cancel()
                self._dtls_start_handle = None
            self.dtls = DtlsSession(self.cert, "server", self.srtp_profiles)
            if self.remote and self.remote.selected is None:
                # DTLS before any STUN (the game session starts its handshake unconditionally):
                # pair with the sender and run our checks/keepalive as for an ICE-selected pair
                self.remote.selected = addr
                self.log("rtc_pair_selected", name=self.name, peer=addr, how="dtls before stun")
                self.on_state("ice-selected", {"peer": addr})
                self._send_check(addr, attempt=0)
                self._schedule_keepalive()
            self.log("dtls_start", name=self.name, role="server", peer=addr, stun_out=self.stats["stun_out"])
            self.on_state("dtls-start", {"role": "server"})
        was_complete = self.dtls.handshake_complete
        try:
            out, app = self.dtls.feed(data)
        except SSL.Error as e:
            self.stats["srtp_errors"] += 1
            self.log("dtls_error", name=self.name, peer=addr, error=repr(e)[:300])
            return
        self._send_dtls(out)
        self._arm_dtls_timer()
        if self.dtls.handshake_complete and not was_complete:
            self._on_dtls_complete()
        for chunk in app:
            self._callback(self.on_app_data, chunk)
        if self.dtls.closed:
            self.log("dtls_closed_by_peer", name=self.name)
            self.on_state("dtls-closed", {})

    def _on_dtls_complete(self) -> None:
        assert self.dtls is not None
        expected = normalize_fingerprint(self.remote.fingerprint if self.remote else None)
        got = normalize_fingerprint(self.dtls.peer_fingerprint)
        fp_ok = expected is None or expected == got
        try:
            keys = self.dtls.export_srtp_keys()
        except RuntimeError as e:  # no use_srtp: application data still works, SRTP does not
            keys = None
            self.log("dtls_without_srtp", name=self.name, error=str(e))
        if keys is not None:
            if self.dtls.role == "client":
                in_key, in_salt, out_key, out_salt = (
                    keys.server_key,
                    keys.server_salt,
                    keys.client_key,
                    keys.client_salt,
                )
            else:
                in_key, in_salt, out_key, out_salt = (
                    keys.client_key,
                    keys.client_salt,
                    keys.server_key,
                    keys.server_salt,
                )
            self.srtp_in = SrtpContext(keys.profile, in_key, in_salt)
            self.srtp_out = SrtpContext(keys.profile, out_key, out_salt)
        info = {
            "role": self.dtls.role,
            "srtp_profile": keys.profile if keys else None,
            "cipher": self.dtls.cipher,
            "peer_fingerprint": self.dtls.peer_fingerprint,
            "fingerprint_verified": fp_ok,
        }
        self.log("dtls_connected", name=self.name, **info)
        if not fp_ok:
            self.log("dtls_fingerprint_mismatch", name=self.name, expected=expected, got=got)
            self.on_state("dtls-fingerprint-mismatch", info)
        self.on_state("dtls-connected", info)
        if self.connected:
            self.connected.set()

    # ---- RTP ---------------------------------------------------------------
    def _handle_rtp(self, data: bytes, addr: Addr, kind: str) -> None:
        self.stats[f"{kind}_in"] += 1
        if self.srtp_in is None:
            if self.allow_plain_rtp and kind == "rtp":
                try:
                    pkt = parse_rtp(data)
                except ValueError as e:
                    self.log("rtp_unparsable", name=self.name, error=str(e))
                    return
                self._callback(self.on_rtp, pkt)
                return
            if self.stats[f"{kind}_in"] <= 3:
                self.log("rtp_before_srtp_keys", name=self.name, peer=addr, kind=kind, len=len(data))
            return
        try:
            plain = self.srtp_in.unprotect_rtcp(data) if kind == "rtcp" else parse_rtp(self.srtp_in.unprotect_rtp(data))
        except (SrtpError, ValueError) as e:
            self.stats["srtp_errors"] += 1
            if self.stats["srtp_errors"] <= 20:
                self.log("srtp_error", name=self.name, peer=addr, kind=kind, error=str(e))
            return
        self._callback(self.on_rtcp if kind == "rtcp" else self.on_rtp, plain)

    def _callback(self, fn: Callable[[Any], None], arg: Any) -> None:
        """Application callbacks must never break the datagram path."""
        try:
            fn(arg)
        except Exception as e:
            self.log(
                "callback_error",
                name=self.name,
                callback=getattr(fn, "__name__", repr(fn)),
                error=repr(e),
                tb=traceback.format_exc()[-600:],
            )

    def send_rtp(
        self,
        payload: bytes,
        payload_type: int = 96,
        marker: bool = False,
        extensions: list[tuple[int, bytes]] | None = None,
        timestamp: int | None = None,
    ) -> bool:
        if not (self.srtp_out and self.remote and self.remote.selected):
            return False
        self.tx_seq = (self.tx_seq + 1) & 0xFFFF
        ts = timestamp if timestamp is not None else int(time.monotonic() * 90000) & 0xFFFFFFFF
        pkt = build_rtp(payload_type, self.tx_seq, ts, self.local_ssrc, payload, marker, extensions)
        self._send(self.srtp_out.protect_rtp(pkt), self.remote.selected)
        self.stats["rtp_out"] += 1
        return True

    def send_rtcp_rr(self) -> bool:
        """Send a minimal empty RTCP Receiver Report (PT=201, no report blocks).

        Standard WebRTC receiver traffic: the display is the receiver of the
        controller's mic-audio and input streams, so it reports on them.
        """
        if not (self.srtp_out and self.remote and self.remote.selected):
            return False
        rr = struct.pack("!BBHI", 0x80 | 0, 201, 1, self.local_ssrc)
        self._send(self.srtp_out.protect_rtcp(rr), self.remote.selected)
        self.stats["rtcp_out"] = self.stats.get("rtcp_out", 0) + 1
        return True

    def send_app_data(self, data: bytes) -> bool:
        if not (self.dtls and self.dtls.handshake_complete):
            return False
        self._send_dtls(self.dtls.send_app(data))
        self.stats["dtls_app_out"] = self.stats.get("dtls_app_out", 0) + 1
        return True

    def send_wire(self, message_id: int, payload: bytes, strict_id: bool = True) -> bool:
        """One WireHeader2 message in one DTLS application-data record."""
        return self.send_app_data(pack_wire_message(message_id, payload, strict_id))

    def send_rtp_wire(
        self,
        payload: bytes,
        payload_type: int = 96,
        marker: bool = False,
        extensions: list[tuple[int, bytes]] | None = None,
        timestamp: int | None = None,
    ) -> bool:
        """raw_dtls counterpart of send_rtp: a plain RTP packet on sub-channel 0x66."""
        if not (self.dtls and self.dtls.handshake_complete):
            return False
        self.tx_seq = (self.tx_seq + 1) & 0xFFFF
        ts = timestamp if timestamp is not None else int(time.monotonic() * 90000) & 0xFFFFFFFF
        pkt = build_rtp(payload_type, self.tx_seq, ts, self.local_ssrc, payload, marker, extensions)
        if not self.send_wire(WIRE_ID_RTP, pkt):
            return False
        self.stats["rtp_out"] += 1
        return True

    # ---- demux ---------------------------------------------------------------
    def datagram_received(self, data: bytes, addr: tuple[str | Any, int]) -> None:
        kind = classify_datagram(data)
        if kind == "stun":
            self._handle_stun(data, addr)
        elif kind == "dtls":
            self._handle_dtls(data, addr)
        elif kind in ("rtp", "rtcp"):
            self._handle_rtp(data, addr, kind)
        else:
            self.stats["unknown"] += 1
            if self.stats["unknown"] <= 10:
                self.log("udp_unknown", name=self.name, peer=addr, len=len(data), head=data[:24].hex())
