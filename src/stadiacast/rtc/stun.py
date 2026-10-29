"""STUN binding requests and responses (the RFC 5389 / RFC 8445 subset ICE connectivity checks use).

Both peers send binding requests with ``USERNAME``, ``MESSAGE-INTEGRITY`` (HMAC-SHA1 over the
message with the peer's ICE password) and ``FINGERPRINT``; the controller's own checks carry
``PRIORITY`` and the ICE role attribute.  The first peer address whose check verifies is selected.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import socket
import string
import struct
import zlib
from dataclasses import dataclass, field

from stadiacast.rtc.rtp import pad4

STUN_MAGIC = 0x2112A442
STUN_MAGIC_BYTES = STUN_MAGIC.to_bytes(4, "big")
BINDING_REQUEST = 0x0001
BINDING_INDICATION = 0x0011
BINDING_SUCCESS = 0x0101
BINDING_ERROR = 0x0111
ATTR_MAPPED_ADDRESS = 0x0001
ATTR_USERNAME = 0x0006
ATTR_MESSAGE_INTEGRITY = 0x0008
ATTR_ERROR_CODE = 0x0009
ATTR_UNKNOWN_ATTRIBUTES = 0x000A
ATTR_XOR_MAPPED_ADDRESS = 0x0020
ATTR_PRIORITY = 0x0024
ATTR_USE_CANDIDATE = 0x0025
ATTR_SOFTWARE = 0x8022
ATTR_FINGERPRINT = 0x8028
ATTR_ICE_CONTROLLED = 0x8029
ATTR_ICE_CONTROLLING = 0x802A
HOST_CANDIDATE_PRIORITY = (126 << 24) | (65535 << 8) | 255  # 2130706431


@dataclass
class StunMessage:
    msg_type: int
    txid: bytes = field(default_factory=lambda: secrets.token_bytes(12))
    attrs: list[tuple[int, bytes]] = field(default_factory=list)  # (type, value) in wire order
    raw: bytes | None = None
    mi_off: int | None = None
    fp_off: int | None = None

    def add(self, t: int, v: bytes) -> StunMessage:
        self.attrs.append((t, v))
        return self

    def get(self, t: int) -> bytes | None:
        for a, v in self.attrs:
            if a == t:
                return v
        return None

    @staticmethod
    def _pack_attrs(attrs: list[tuple[int, bytes]]) -> bytes:
        out = bytearray()
        for t, v in attrs:
            out += struct.pack("!HH", t, len(v)) + v + b"\0" * pad4(len(v))
        return bytes(out)

    def _hdr(self, length: int) -> bytes:
        return struct.pack("!HHI12s", self.msg_type, length, STUN_MAGIC, self.txid)

    def encode(self, integrity_key: bytes | None = None, fingerprint: bool = True) -> bytes:
        attrs = [(t, v) for t, v in self.attrs if t not in (ATTR_MESSAGE_INTEGRITY, ATTR_FINGERPRINT)]
        body = self._pack_attrs(attrs)
        if integrity_key is not None:
            mac = hmac.new(integrity_key, self._hdr(len(body) + 24) + body, hashlib.sha1).digest()
            body += self._pack_attrs([(ATTR_MESSAGE_INTEGRITY, mac)])
        if fingerprint:
            crc = (zlib.crc32(self._hdr(len(body) + 8) + body) ^ 0x5354554E) & 0xFFFFFFFF
            body += self._pack_attrs([(ATTR_FINGERPRINT, struct.pack("!I", crc))])
        return self._hdr(len(body)) + body

    @classmethod
    def parse(cls, data: bytes) -> StunMessage:
        if len(data) < 20 or data[0] & 0xC0:
            raise ValueError("not a STUN message")
        msg_type, length, magic, txid = struct.unpack_from("!HHI12s", data, 0)
        if magic != STUN_MAGIC:
            raise ValueError("bad magic cookie")
        if length % 4 or 20 + length > len(data):
            raise ValueError("bad length")
        msg = cls(msg_type, txid, [], raw=bytes(data[: 20 + length]))
        off = 20
        end = 20 + length
        while off + 4 <= end:
            t, ln = struct.unpack_from("!HH", data, off)
            v = bytes(data[off + 4 : off + 4 + ln])
            if len(v) != ln:
                raise ValueError("truncated attribute")
            if t == ATTR_MESSAGE_INTEGRITY and msg.mi_off is None:
                msg.mi_off = off
            if t == ATTR_FINGERPRINT:
                msg.fp_off = off
            msg.attrs.append((t, v))
            off += 4 + ln + pad4(ln)
        return msg

    def check_fingerprint(self) -> bool:
        fp = self.get(ATTR_FINGERPRINT)
        if self.fp_off is None or self.raw is None or fp is None:
            return False
        crc = (zlib.crc32(self._hdr(self.fp_off - 20 + 8) + self.raw[20 : self.fp_off]) ^ 0x5354554E) & 0xFFFFFFFF
        return bool(crc == struct.unpack("!I", fp)[0])

    def check_integrity(self, key: bytes) -> bool:
        mi = self.get(ATTR_MESSAGE_INTEGRITY)
        if self.mi_off is None or self.raw is None or mi is None:
            return False
        mac = hmac.new(key, self._hdr(self.mi_off - 20 + 24) + self.raw[20 : self.mi_off], hashlib.sha1).digest()
        return hmac.compare_digest(mac, mi)

    @staticmethod
    def xor_address(ip: str, port: int) -> bytes:
        packed = socket.inet_aton(ip)
        xaddr = bytes(a ^ b for a, b in zip(packed, STUN_MAGIC_BYTES, strict=False))
        return struct.pack("!BBH", 0, 1, port ^ (STUN_MAGIC >> 16)) + xaddr

    @staticmethod
    def parse_xor_address(v: bytes) -> tuple[str, int]:
        if len(v) < 8 or v[1] != 1:
            raise ValueError("only IPv4 XOR-MAPPED-ADDRESS supported")
        port = struct.unpack("!H", v[2:4])[0] ^ (STUN_MAGIC >> 16)
        ip = socket.inet_ntoa(bytes(a ^ b for a, b in zip(v[4:8], STUN_MAGIC_BYTES, strict=False)))
        return ip, port

    def error_code(self) -> tuple[int, str] | None:
        v = self.get(ATTR_ERROR_CODE)
        if not v or len(v) < 4:
            return None
        code = (v[2] & 0x7) * 100 + v[3]
        return code, v[4:].decode("utf-8", "replace")


def random_ice_string(n: int) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(n))
