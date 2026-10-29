"""Minimal SNTP (RFC 4330) responder for the hotspot rig.

The firmware syncs its clock from ``time.google.com`` (SNTP), and the cloudcast RPCs need a wall
clock (the signed request headers carry iat/exp).  Point ``time.google.com`` at this host (the
DNS sink does that with ``--redirect time.google.com``) and run this on UDP 123; every request is
answered with this host's clock and logged as one JSONL record.

    python -m stadiacast.helpers.sntp_server --log captures/x/sntp.jsonl

macOS lets an unprivileged process bind port 123, so no root is required there.
"""

from __future__ import annotations

import argparse
import socket
import struct
import time
from collections.abc import Sequence

from stadiacast.log import EventLog

NTP_EPOCH_OFFSET = 2208988800  # 1900-01-01 -> 1970-01-01
NTP_PACKET_LEN = 48


def ntp_timestamp(t: float) -> bytes:
    """A 64-bit NTP timestamp (seconds since 1900, 32.32 fixed point) for the Unix time ``t``."""
    secs = int(t) + NTP_EPOCH_OFFSET
    frac = int((t - int(t)) * (1 << 32))
    return struct.pack("!II", secs & 0xFFFFFFFF, frac & 0xFFFFFFFF)


def build_reply(req: bytes, recv_time: float) -> bytes:
    """A stratum-1 server reply to the client packet ``req`` received at ``recv_time``."""
    li_vn_mode = req[0]
    vn = (li_vn_mode >> 3) & 7 or 4
    mode = 4  # server
    header = (0 << 6) | (vn << 3) | mode
    stratum = 1
    poll = req[2] if len(req) > 2 else 6
    precision = -20
    root_delay = root_disp = 0
    ref_id = b"LOCL"
    now = time.time()
    ref_ts = ntp_timestamp(now - 1)
    orig_ts = req[40:48] if len(req) >= NTP_PACKET_LEN else b"\0" * 8  # client's transmit timestamp
    recv_ts = ntp_timestamp(recv_time)
    xmit_ts = ntp_timestamp(now)
    return (
        struct.pack("!BBBb", header, stratum, poll, precision)
        + struct.pack("!II", root_delay, root_disp)
        + ref_id
        + ref_ts
        + orig_ts
        + recv_ts
        + xmit_ts
    )


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--listen", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=123)
    ap.add_argument("--controller-ip", default=None, help="mark requests from this address in the log")
    ap.add_argument("--log", required=True, help="JSONL log file")
    a = ap.parse_args(argv)
    log = EventLog(a.log)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind((a.listen, a.port))
    log("start", listen=f"{a.listen}:{a.port}")
    try:
        while True:
            try:
                data, addr = s.recvfrom(512)
            except OSError as e:
                log("recv_error", error=repr(e))
                continue
            t = time.time()
            if len(data) < NTP_PACKET_LEN:
                log("short_request", src=f"{addr[0]}:{addr[1]}", len=len(data), hex=data.hex())
                continue
            reply = build_reply(data, t)
            try:
                s.sendto(reply, addr)
                err = None
            except OSError as e:
                err = repr(e)
            log(
                "request",
                src=f"{addr[0]}:{addr[1]}",
                from_controller=addr[0] == a.controller_ip,
                vn=(data[0] >> 3) & 7,
                mode=data[0] & 7,
                client_xmit_hex=data[40:48].hex(),
                answered_with=time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + "Z",
                error=err,
            )
    except KeyboardInterrupt:
        pass
    finally:
        s.close()
        log.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
