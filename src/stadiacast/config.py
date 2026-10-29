"""Receiver configuration: the :class:`Config` dataclass, the proven flag presets and shared constants."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from stadiacast.rtc import WIRE_ID_DC_RPC_DISPLAY

#: Where the controller uploads Clearcut analytics (SNI ``play.googleapis.com``).
#: ``--clearcut-block`` targets exactly these names.
CLEARCUT_HOSTS: tuple[str, ...] = ("play.googleapis.com",)

#: Controller address of the reference rig (macOS Internet Sharing hotspot, Mac = 192.168.2.1).
DEFAULT_CONTROLLER_IP = "192.168.2.2"


@dataclass
class Config:
    """Everything the receiver needs to run.  Built by :func:`stadiacast.cli.parse_args`.

    Field comments give the accepted values; the CLI help (``stadiacast --help``) explains
    what each knob does to the controller.
    """

    # -- certificate -----------------------------------------------------------------------
    cert: str
    key: str
    self_signed: bool = False  # generate a lab pair when cert/key are missing (tests only: the controller rejects it)
    clock_pin: str | None = None  # None | "auto" (midpoint of the cert window) | ISO date served to the controller

    # -- listeners -------------------------------------------------------------------------
    bind: str = "0.0.0.0"
    https_port: int = 443
    http_ports: tuple[int, ...] = (80, 8008)
    udp_port: int = 40000  # STUN/DTLS/RTP socket of the display link
    game_udp_port: int = 40001  # UDP socket of the game-session endpoint
    advertise_ip: str | None = None  # IP written into ICE candidates (None: the interface routing to controller_ip)
    controller_ip: str = DEFAULT_CONTROLLER_IP
    idle_timeout: float = 30.0  # seconds an HTTP connection may sit idle between requests

    # -- helper processes ------------------------------------------------------------------
    dns_upstream: str | None = None  # spawn the DNS sink forwarding to this resolver
    dns_redirect: tuple[str, ...] = ("time.google.com",)  # extra names the DNS sink points at us
    sntp: bool = False  # spawn the SNTP server on UDP 123

    # -- boot-time endpoints ---------------------------------------------------------------
    stadiaconfig: str = "passthrough"  # passthrough | file:PATH | json:{...}
    clearcut_block: str = "off"  # off | tls (abort the TLS handshake for CLEARCUT_HOSTS) | dns (NXDOMAIN)
    clearcut_close: bool = True  # close the Clearcut connection after the LogResponse
    rpc_responses: dict[str, bytes] = field(default_factory=dict)  # v1:METHOD -> canned body (overrides the schema)
    rpc_fields: dict[str, dict[str, Any]] = field(default_factory=dict)  # v1:METHOD -> response field overrides

    # -- signaling -------------------------------------------------------------------------
    signaling_mode: str = "auto"  # auto | offer | answer
    offer_on: tuple[str, ...] = ()  # legacy: paths whose response carries our offer bundle
    offer_overrides: dict[str, Any] = field(default_factory=dict)  # merged into the RtcSdpOffer we build
    boreal: str = "auto"  # auto (echo the offer's boreal availability) | off
    gmsg_hold: float = 0.0  # seconds to hold an idle /gmsg poll (long-poll emulation)
    gmsg_ack_release: float = 0.0  # >0: release an idle ACK poll this long after DTLS connects

    # -- RTC -------------------------------------------------------------------------------
    dtls_role: str = "auto"  # auto | client | server
    ice_fast_checks: float = 0.0  # seconds between our ICE checks from pairing until DTLS (0: 2.5 s keepalive only)
    rtcp_interval: float = 1.0  # display-side RTCP RR cadence after DTLS (0 disables)
    ack_mode: str = "rtp"  # rtp | none: echo InputProtocolMessage{ack_sequence_number}
    input_payload_type: int = 96

    # -- display link (post-DTLS RPCs) -----------------------------------------------------
    display_rpc: str = "on"  # on | off
    display_rpc_trigger: str = "resolve"  # resolve (after v1:ResolveDiscovery) | dtls (at dtls-connected)
    display_rpc_delay: float = 0.5  # seconds after the trigger before the first GetControllerStateRequest
    display_rpc_fallback: float = 8.0  # trigger=resolve: start anyway this long after DTLS (0 = never)
    display_rpc_window: float = 300.0  # keep re-sending GetControllerStateRequest this long until answered OK
    display_rpc_interval: float = 1.5  # seconds between GetControllerStateRequest re-sends
    rpc_wire_id: int = WIRE_ID_DC_RPC_DISPLAY  # WireHeader2 id of our display RPCs (1..255 or a keepalive id)
    time_sync: str = "reply"  # reply | unsupported (NOT_FOUND stops the controller's clock sync)
    send_log_report: bool = False  # GetControllerStateRequest.display_supports_send_log_report

    # -- game session ----------------------------------------------------------------------
    start_game: str | None = None  # StartGame key sent once READY (None = off; "auto" = generated)
    start_game_delay: float = 1.0  # seconds after READY before StartGame
    game_framing: str = "auto"  # auto | rtp | raw | srtp | unframed
    game_rpc_reply: str = "ok"  # ok | unimplemented | none
    rumble_refresh: float = 2.0  # re-send a sustained vibration this often (haptics watchdog: 5 s)
    rumble_dither: bool = False  # alternate refreshed levels by 1

    # -- misc ------------------------------------------------------------------------------
    proto_dir: str | None = None  # directory with the .descriptor files (None: the package's schemas/)
    log_dir: str | None = None  # None: captures/<stamp>
    preset: str | None = None  # name of the preset applied (logged at startup)
    verbose: bool = False
    echo: bool = True  # console echo of the event log


#: Flag sets proven against the real controller.  Explicit flags override a preset.
#:
#: ``live``: the configuration proven end to end against firmware ``ota.282115``: the
#: controller stays on, ResolveDiscovery fires, the display RPC handshake reaches READY and
#: input arrives.  The two settings that matter most are ``clearcut_block="tls"`` (every wake
#: otherwise powers off ~0.3 s after its first Clearcut upload) and ``rpc_wire_id=0x75``
#: (0xFFFA is dropped by the controller on the display link).
PRESETS: dict[str, dict[str, Any]] = {
    "live": {
        "gmsg_hold": 20.0,
        "dtls_role": "server",
        "sntp": True,
        "boreal": "off",
        "rtcp_interval": 1.0,
        "display_rpc_trigger": "dtls",
        "display_rpc_delay": 0.6,
        "ice_fast_checks": 1.0,
        "clearcut_block": "tls",
        "rpc_wire_id": WIRE_ID_DC_RPC_DISPLAY,
    },
}
