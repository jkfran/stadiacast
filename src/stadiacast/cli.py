"""Command line of the receiver: ``stadiacast`` / ``python -m stadiacast``."""

from __future__ import annotations

import argparse
import asyncio
import json
import signal
import sys
from collections.abc import Sequence
from typing import Any

from stadiacast import __version__
from stadiacast.config import AUDIO_APPLICATIONS, PRESETS, Config
from stadiacast.errors import SetupError
from stadiacast.game_session import GAME_FRAMINGS, GAME_KEY_MAX, HAPTICS_WATCHDOG_S
from stadiacast.helpers import HELPER_FLAG, run_helper
from stadiacast.receiver import Receiver
from stadiacast.rtc import WIRE_ID_DC_RPC_DISPLAY, wire_id_valid

DEFAULT_CERT = "certs/fullchain.pem"
DEFAULT_KEY = "certs/privkey.pem"


DOCS = "https://github.com/jkfran/stadiacast/blob/main/docs"


def _csv(value: str) -> tuple[str, ...]:
    return tuple(p.strip() for p in value.split(",") if p.strip())


def _ports(value: str) -> tuple[int, ...]:
    try:
        ports = tuple(int(p) for p in _csv(value))
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not a comma separated list of port numbers") from None
    if any(not 0 <= p <= 65535 for p in ports):
        raise argparse.ArgumentTypeError(f"{value!r} has a port outside 0 to 65535")
    return ports


def _wire_id(value: str) -> int:
    try:
        return int(value, 0)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not a number (decimal, or hexadecimal as 0x75)") from None


class _ListAudioDevices(argparse.Action):
    """``--audio-devices``: print the capture devices of this computer and exit, like ``--version``.

    The list goes to standard output, one name per line, so it can be piped; a missing extra or
    library is one line on standard error and exit status 2.
    """

    def __call__(self, parser: argparse.ArgumentParser, *_args: Any, **_kw: Any) -> None:
        from stadiacast.capture import list_devices

        try:
            names = list_devices()
        except SetupError as e:  # no audio extra, no PortAudio library
            parser.exit(2, f"stadiacast: {e}\n")
        print("\n".join(names) if names else "no capture devices found")
        parser.exit(0)


def build_parser(preset: str | None = None) -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="stadiacast",
        description="LAN-side receiver for the Stadia Controller (Wi-Fi mode).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Start with --preset live for the flag set proven against the real controller; explicit flags\n"
            "override it.  The setup guide covers the network and the certificate, the protocol notes what\n"
            f"is behind every knob:\n  {DOCS}/setup.md\n  {DOCS}/protocol.md"
        ),
    )
    ap.add_argument(
        "--preset",
        choices=sorted(PRESETS),
        default=None,
        help="apply a flag set proven against the real controller ('live'); explicit flags override it",
    )
    ap.add_argument("-v", "--verbose", action="store_true", help="show the bulky log fields on the console too")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    g = ap.add_argument_group("certificate")
    g.add_argument(
        "--cert",
        default=None,
        help=f"PEM certificate chain (default: {DEFAULT_CERT} when present, else the built-in credential)",
    )
    g.add_argument(
        "--key",
        default=None,
        help=f"PEM private key (default: {DEFAULT_KEY} when present, else the built-in credential)",
    )
    g.add_argument(
        "--self-signed",
        action="store_true",
        help="generate a lab certificate pair in the run directory (tests only: the controller rejects it); "
        "with --cert/--key, create the pair there when it is missing",
    )
    g.add_argument(
        "--clock-pin",
        default=None,
        metavar="auto|real|ISO-DATE",
        help="serve a chosen date/time (Date header + GetCurrentTime replies) instead of real time; "
        "auto = the midpoint of the certificate's validity window",
    )

    g = ap.add_argument_group("listeners")
    g.add_argument("--bind", default="0.0.0.0", help="address the listeners and UDP sockets bind (default: all)")
    g.add_argument("--https-port", type=int, default=443, help="the controller connects to 443 only (default)")
    g.add_argument(
        "--http-ports",
        type=_ports,
        default=(80, 8008),
        help="comma separated plaintext ports, for the control endpoints (default: 80,8008; '' to disable)",
    )
    g.add_argument("--udp-port", type=int, default=40000, help="STUN/DTLS/RTP socket of the display link")
    g.add_argument(
        "--game-udp-port",
        type=int,
        default=40001,
        help="UDP socket of the game-session endpoint answered to the controller's InitializationRequest (0 = any)",
    )
    g.add_argument(
        "--advertise-ip",
        default=None,
        help="this computer's address on the controller's network: written into ICE candidates and the DNS "
        "sink's answers (default: the address of the interface that routes to --controller-ip)",
    )
    g.add_argument(
        "--controller-ip",
        default="192.168.2.2",
        help="an address on the controller's network, the controller's when it is known: marks its lines in "
        "the helpers' logs and tells --advertise-ip which interface is meant (default: 192.168.2.2)",
    )
    g.add_argument("--idle-timeout", type=float, default=30.0, help="seconds an HTTP connection may idle")

    g = ap.add_argument_group("helper processes")
    g.add_argument("--dns-upstream", default=None, help="spawn the DNS sink forwarding to this resolver")
    g.add_argument(
        "--dns-redirect",
        default="time.google.com",
        help="extra comma separated names the spawned DNS sink answers with our IP ('' for none)",
    )
    g.add_argument(
        "--sntp",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="spawn the SNTP server on UDP 123 (answers time.google.com); --no-sntp turns off a preset's",
    )

    g = ap.add_argument_group("boot-time endpoints")
    g.add_argument("--stadiaconfig", default="passthrough", help="passthrough | file:PATH | json:{...}")
    g.add_argument(
        "--clearcut-block",
        choices=("off", "tls", "dns"),
        default="off",
        help="tls: abort the TLS handshake when the SNI is the Clearcut host, so the controller's first "
        "analytics upload fails at once; dns: the DNS sink answers NXDOMAIN (leaks: the firmware falls "
        "back to 8.8.8.8 and reaches the real host)",
    )
    g.add_argument(
        "--clearcut-keepalive",
        action="store_true",
        help="keep the Clearcut connection open after the LogResponse (default: Connection: close)",
    )
    g.add_argument(
        "--rpc-response",
        action="append",
        default=[],
        metavar="METHOD=FILE",
        help="canned response body (raw bytes) for v1:METHOD (or 'gmsg'); overrides the schema",
    )
    g.add_argument(
        "--rpc-fields",
        action="append",
        default=[],
        metavar="METHOD=JSON",
        help="JSON overrides merged into the schema-built response for v1:METHOD",
    )

    g = ap.add_argument_group("signaling")
    g.add_argument(
        "--signaling-mode",
        choices=("auto", "offer", "answer"),
        default="auto",
        help="auto: answer the controller's offer from DiscoverDisplay over GMsg; offer: legacy experiment",
    )
    g.add_argument(
        "--offer-on", default="", help="comma separated extra paths whose response carries our offer bundle (legacy)"
    )
    g.add_argument("--offer-json", default=None, help="JSON file with RtcSdpOffer field overrides")
    g.add_argument("--boreal", choices=("auto", "off"), default="auto", help="off: answer without use_boreal")
    g.add_argument(
        "--gmsg-hold",
        type=float,
        default=0.0,
        metavar="SECONDS",
        help="hold an idle /gmsg poll this long waiting for a message (long-poll emulation)",
    )
    g.add_argument(
        "--gmsg-ack-release",
        type=float,
        default=0.0,
        metavar="SECONDS",
        help=">0: release an idle ACK poll this long after DTLS connects, so the controller's serial HTTP "
        "client serves v1:ResolveDiscovery immediately (0 = full --gmsg-hold)",
    )

    g = ap.add_argument_group("RTC")
    g.add_argument(
        "--dtls-role",
        choices=("auto", "client", "server"),
        default="auto",
        help="server: wait for the controller's ClientHello, as the live preset does; auto: also start the "
        "handshake when none has come (default)",
    )
    g.add_argument(
        "--ice-fast-checks",
        type=float,
        default=0.0,
        metavar="SECONDS",
        help="send our ICE check to the selected pair every SECONDS from pairing until DTLS starts "
        "(default 0: only the 2.5 s keepalive)",
    )
    g.add_argument(
        "--rtcp-interval", type=float, default=1.0, help="seconds between display-side RTCP RRs after DTLS (0 disables)"
    )
    g.add_argument("--ack-mode", choices=("rtp", "none"), default="rtp", help="echo InputProtocolMessage acks")
    g.add_argument(
        "--input-payload-type", type=int, default=96, help="RTP payload type of our input acks (default: 96)"
    )

    g = ap.add_argument_group("headset audio")
    g.add_argument(
        "--audio-source",
        default="",
        help="send audio to the headset jack once the link is READY: '' (off), tone, file:PATH, "
        "system (what this computer plays) or device:NAME (see --audio-devices)",
    )
    g.add_argument("--audio-bitrate", type=int, default=64, help="Opus bitrate in kbit/s")
    g.add_argument(
        "--audio-application",
        choices=AUDIO_APPLICATIONS,
        default="lowdelay",
        help="what the Opus encoder favours: lowdelay for games (the shortest delay), audio for music (the best "
        "quality at the bitrate)",
    )
    g.add_argument(
        "--audio-volume",
        type=float,
        default=1.0,
        help="gain before encoding (1 = as captured; above 1 a limiter guards against clipping)",
    )
    g.add_argument(
        "--audio-follow-volume",
        choices=("on", "off"),
        default="on",
        help="Windows: scale the loopback capture by the volume slider, so the slider, mute and the media keys "
        "reach the headset (off: the mix before the slider)",
    )
    g.add_argument(
        "--audio-devices",
        nargs=0,
        action=_ListAudioDevices,
        help="list the capture devices device:NAME accepts on this computer, then exit",
    )
    g.add_argument(
        "--audio-mode",
        type=int,
        default=2,
        help="SetAudioModeRequest sent before the stream: 2 = WebRTC Opus (the jack), 0 = do not send one",
    )

    g = ap.add_argument_group("virtual gamepad")
    g.add_argument(
        "--gamepad",
        choices=("off", "vigem", "ojd"),
        default="off",
        help="feed input to a virtual pad, one per controller (vigem: Windows; ojd: macOS OpenJoystickDriver); "
        "switches StartGame on so the game's rumble reaches the controller",
    )
    g.add_argument(
        "--gamepad-deadzone",
        choices=("on", "off"),
        default="on",
        help="apply XInput's stick deadzones to the virtual pad (off: raw sticks)",
    )
    g.add_argument(
        "--gamepad-profile",
        choices=("sdl-compatible", "game-controller-compatible", "generic"),
        default="game-controller-compatible",
        help="OJD virtual controller identity (default: game-controller-compatible for Apple's controller framework)",
    )
    g.add_argument(
        "--ojd-path", default="", help="OJD executable (default: find ojd on PATH or the installed macOS app)"
    )

    g = ap.add_argument_group("display link")
    g.add_argument(
        "--display-rpc",
        choices=("on", "off"),
        default="on",
        help="after DTLS, send GetControllerStateRequest and answer the controller's requests",
    )
    g.add_argument(
        "--display-rpc-trigger",
        choices=("resolve", "dtls"),
        default="resolve",
        help="start the display RPCs once v1:ResolveDiscovery is answered (default) or at dtls-connected",
    )
    g.add_argument(
        "--display-rpc-delay",
        type=float,
        default=0.5,
        help="seconds after the trigger before the first GetControllerStateRequest",
    )
    g.add_argument(
        "--display-rpc-fallback",
        type=float,
        default=8.0,
        help="trigger=resolve: start anyway this many seconds after DTLS (0 = never)",
    )
    g.add_argument(
        "--display-rpc-window",
        type=float,
        default=300.0,
        help="re-send GetControllerStateRequest every 1.5 s until answered OK, for at most this long",
    )
    g.add_argument(
        "--rpc-wire-id",
        type=_wire_id,
        default=WIRE_ID_DC_RPC_DISPLAY,
        help="WireHeader2 id of our display RPC records (default 0x75; the controller accepts 1..255; "
        "0xFFFA is dropped)",
    )
    g.add_argument(
        "--time-sync",
        choices=("reply", "unsupported"),
        default="reply",
        help="answer GetCurrentTimeRequest (reply) or with NOT_FOUND, which stops the clock sync",
    )
    g.add_argument(
        "--send-log-report", action="store_true", help="tell the controller display_supports_send_log_report = true"
    )

    g = ap.add_argument_group("game session")
    g.add_argument(
        "--start-game",
        nargs="?",
        const="auto",
        default=None,
        metavar="KEY",
        help="once READY, send DisplayToController #8 StartGame with this game/session key "
        f"(max {GAME_KEY_MAX} chars; no value or 'auto' = a generated key); also POST /_start_game",
    )
    g.add_argument("--start-game-delay", type=float, default=1.0, help="seconds after READY before StartGame")
    g.add_argument(
        "--game-framing",
        choices=("auto", *GAME_FRAMINGS),
        default="auto",
        help="how acks/feedback go out on the game session: auto = like the controller's input (rtp until "
        "seen), rtp = RTP in WireHeader2 0x66, raw = the bare InputProtocolMessage in 0x66, srtp = SRTP on "
        "the UDP flow, unframed = a DTLS record without WireHeader2",
    )
    g.add_argument(
        "--game-rpc-reply",
        choices=("ok", "unimplemented", "none"),
        default="ok",
        help="answer to the controller's requests on the game session's RPC channel",
    )
    g.add_argument(
        "--rumble-refresh",
        type=float,
        default=2.0,
        metavar="SECONDS",
        help="re-send a sustained vibration this often (the controller's haptics watchdog stops the motors "
        f"{HAPTICS_WATCHDOG_S:g} s after the last write)",
    )
    g.add_argument(
        "--rumble-dither",
        action="store_true",
        help="alternate refreshed motor levels by 1, in case an unchanged level does not re-arm the watchdog",
    )

    g = ap.add_argument_group("files")
    g.add_argument("--proto-dir", default=None, help="directory with the .descriptor files (default: bundled)")
    g.add_argument(
        "--log-dir", default=None, help="run directory for server.jsonl and bodies (default captures/<stamp>)"
    )

    if preset:
        ap.set_defaults(**PRESETS[preset])
    return ap


def parse_args(argv: Sequence[str] | None = None) -> Config:
    """Turn the command line into a :class:`Config`; a ``--preset`` sets the defaults first."""
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--preset", choices=sorted(PRESETS), default=None)
    preset = pre.parse_known_args(argv)[0].preset
    ap = build_parser(preset)
    a = ap.parse_args(argv)
    if not 0 < a.rumble_refresh < HAPTICS_WATCHDOG_S:
        ap.error(f"--rumble-refresh must be > 0 and < {HAPTICS_WATCHDOG_S:g} s (the haptics watchdog)")
    if a.start_game and len(a.start_game.encode()) > GAME_KEY_MAX:
        ap.error(f"--start-game: the key is limited to {GAME_KEY_MAX} bytes")
    if not wire_id_valid(a.rpc_wire_id):
        ap.error(f"--rpc-wire-id {a.rpc_wire_id:#x}: the controller drops it (valid: 1..255, 0xfffe, 0xffff)")
    rpc_responses: dict[str, bytes] = {}
    for item in a.rpc_response:
        k, _, v = item.partition("=")
        with open(v, "rb") as fh:
            rpc_responses[k.strip()] = fh.read()
    rpc_fields = {}
    for item in a.rpc_fields:
        k, _, v = item.partition("=")
        try:
            rpc_fields[k.strip()] = json.loads(v)
        except ValueError as e:
            ap.error(f"--rpc-fields {k.strip()}: the value is not JSON ({e})")
    overrides = {}
    if a.offer_json:
        with open(a.offer_json, encoding="utf-8") as fh:
            try:
                overrides = json.load(fh)
            except ValueError as e:
                ap.error(f"--offer-json {a.offer_json}: the file is not JSON ({e})")
    return Config(
        cert=a.cert or DEFAULT_CERT,
        key=a.key or DEFAULT_KEY,
        cert_given=a.cert is not None or a.key is not None,
        self_signed=a.self_signed,
        clock_pin=None if a.clock_pin == "real" else a.clock_pin,
        bind=a.bind,
        https_port=a.https_port,
        http_ports=a.http_ports,
        udp_port=a.udp_port,
        game_udp_port=a.game_udp_port,
        advertise_ip=a.advertise_ip,
        controller_ip=a.controller_ip,
        idle_timeout=a.idle_timeout,
        dns_upstream=a.dns_upstream,
        dns_redirect=_csv(a.dns_redirect),
        sntp=a.sntp,
        stadiaconfig=a.stadiaconfig,
        clearcut_block=a.clearcut_block,
        clearcut_close=not a.clearcut_keepalive,
        rpc_responses=rpc_responses,
        rpc_fields=rpc_fields,
        signaling_mode=a.signaling_mode,
        offer_on=_csv(a.offer_on),
        offer_overrides=overrides,
        boreal=a.boreal,
        gmsg_hold=a.gmsg_hold,
        gmsg_ack_release=a.gmsg_ack_release,
        dtls_role=a.dtls_role,
        ice_fast_checks=a.ice_fast_checks,
        rtcp_interval=a.rtcp_interval,
        ack_mode=a.ack_mode,
        input_payload_type=a.input_payload_type,
        audio_source=a.audio_source,
        audio_bitrate_kbps=a.audio_bitrate,
        audio_application=a.audio_application,
        audio_volume=a.audio_volume,
        audio_follow_volume=a.audio_follow_volume == "on",
        gamepad=a.gamepad,
        gamepad_deadzone=a.gamepad_deadzone == "on",
        gamepad_profile=a.gamepad_profile,
        ojd_path=a.ojd_path,
        audio_mode=a.audio_mode,
        display_rpc=a.display_rpc,
        display_rpc_trigger=a.display_rpc_trigger,
        display_rpc_delay=a.display_rpc_delay,
        display_rpc_fallback=a.display_rpc_fallback,
        display_rpc_window=a.display_rpc_window,
        rpc_wire_id=a.rpc_wire_id,
        time_sync=a.time_sync,
        send_log_report=a.send_log_report,
        start_game=a.start_game,
        start_game_delay=a.start_game_delay,
        game_framing=a.game_framing,
        game_rpc_reply=a.game_rpc_reply,
        rumble_refresh=a.rumble_refresh,
        rumble_dither=a.rumble_dither,
        proto_dir=a.proto_dir,
        log_dir=a.log_dir,
        preset=a.preset,
        verbose=a.verbose,
    )


async def amain(cfg: Config) -> int:
    receiver = Receiver(cfg)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass
    try:
        await receiver.start()
        print(
            f"\nstadiacast {__version__} ready: https :{receiver.http.https_port}  http {receiver.http.http_ports}  "
            f"udp {cfg.advertise_ip}:{cfg.udp_port}/{cfg.game_udp_port} (+2 per controller)  logs {cfg.log_dir}\n"
            "wake the controller(s) now; Ctrl-C to stop.\n",
            flush=True,
        )
        await stop.wait()
    finally:
        # Also on the way out of a start that failed half way, and on Windows, where Ctrl-C cancels
        # this task instead of setting the event: the helpers, the streams and the virtual pads
        # are the receiver's to stop.
        await receiver.stop()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Run the receiver until Ctrl-C or SIGTERM.

    What the user can fix is printed in one line with exit status 2: a missing certificate or
    ``--rpc-response`` file, a port in use, a bad ``--clock-pin`` or ``--rpc-fields`` JSON, a
    missing extra or driver (:class:`~stadiacast.errors.SetupError`).  Anything else is a bug and
    keeps its traceback.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == [HELPER_FLAG]:  # the frozen executable re-launched as one of its helpers
        return run_helper(args[1:])
    try:
        return asyncio.run(amain(parse_args(args)))
    except KeyboardInterrupt:
        return 0
    except (OSError, ValueError, SetupError) as e:
        print(f"stadiacast: {e}", file=sys.stderr)
        return 2
