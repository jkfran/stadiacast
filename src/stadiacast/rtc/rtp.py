"""RTP packets (RFC 3550): parse and build, including one-byte header extensions (RFC 8285)."""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Any


def pad4(n: int) -> int:
    """Bytes needed to pad ``n`` to a multiple of four (STUN attributes, RTP extensions)."""
    return (4 - n % 4) % 4


@dataclass
class RtpPacket:
    version: int
    padding: bool
    marker: bool
    payload_type: int
    seq: int
    timestamp: int
    ssrc: int
    csrcs: list[int]
    ext_profile: int | None
    extensions: list[tuple[int, bytes]]  # (id, data), one-byte or two-byte header form
    payload: bytes
    header_len: int
    raw: bytes

    def summary(self) -> dict[str, Any]:
        return {
            "pt": self.payload_type,
            "seq": self.seq,
            "rtp_ts": self.timestamp,
            "ssrc": f"{self.ssrc:08x}",
            "marker": self.marker,
            "ext": [(i, v.hex()) for i, v in self.extensions],
            "payload_len": len(self.payload),
        }


def parse_header_extensions(profile: int, data: bytes) -> list[tuple[int, bytes]]:
    """The ``(id, data)`` pairs of an RTP header extension block (RFC 8285 one- and two-byte forms)."""
    out: list[tuple[int, bytes]] = []
    i = 0
    if profile == 0xBEDE:
        while i < len(data):
            b = data[i]
            i += 1
            if b == 0:
                continue
            eid, ln = b >> 4, (b & 0x0F) + 1
            if eid == 15:
                break
            out.append((eid, data[i : i + ln]))
            i += ln
    elif (profile & 0xFFF0) == 0x1000:
        while i + 1 < len(data):
            eid = data[i]
            if eid == 0:
                i += 1
                continue
            ln = data[i + 1]
            out.append((eid, data[i + 2 : i + 2 + ln]))
            i += 2 + ln
    else:
        out.append((0, data))
    return out


def parse_rtp(data: bytes) -> RtpPacket:
    if len(data) < 12:
        raise ValueError("short RTP packet")
    b0, b1 = data[0], data[1]
    if b0 >> 6 != 2:
        raise ValueError("RTP version != 2")
    cc = b0 & 0x0F
    seq, ts, ssrc = struct.unpack_from("!HII", data, 2)
    off = 12
    csrcs = [struct.unpack_from("!I", data, off + 4 * i)[0] for i in range(cc)]
    off += 4 * cc
    ext_profile: int | None = None
    extensions: list[tuple[int, bytes]] = []
    if b0 & 0x10:
        ext_profile, ext_len = struct.unpack_from("!HH", data, off)
        ext_data = data[off + 4 : off + 4 + 4 * ext_len]
        off += 4 + 4 * ext_len
        extensions = parse_header_extensions(ext_profile, ext_data)
    if off > len(data):
        raise ValueError("RTP header longer than packet")
    payload = data[off:]
    if b0 & 0x20 and payload:
        pad = payload[-1]
        if 0 < pad <= len(payload):
            payload = payload[:-pad]
    return RtpPacket(
        2,
        bool(b0 & 0x20),
        bool(b1 & 0x80),
        b1 & 0x7F,
        seq,
        ts,
        ssrc,
        csrcs,
        ext_profile,
        extensions,
        payload,
        off,
        data,
    )


def build_rtp(
    payload_type: int,
    seq: int,
    timestamp: int,
    ssrc: int,
    payload: bytes,
    marker: bool = False,
    extensions: list[tuple[int, bytes]] | None = None,
) -> bytes:
    b0 = 0x80 | (0x10 if extensions else 0)
    b1 = (0x80 if marker else 0) | (payload_type & 0x7F)
    hdr = struct.pack("!BBHII", b0, b1, seq & 0xFFFF, timestamp & 0xFFFFFFFF, ssrc & 0xFFFFFFFF)
    if extensions:
        body = bytearray()
        for eid, v in extensions:
            if not 1 <= eid <= 14 or not 1 <= len(v) <= 16:
                raise ValueError("one-byte header extension needs id 1..14 and 1..16 bytes")
            body += bytes([(eid << 4) | (len(v) - 1)]) + v
        body += b"\0" * pad4(len(body))
        hdr += struct.pack("!HH", 0xBEDE, len(body) // 4) + bytes(body)
    return hdr + payload
