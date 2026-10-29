"""SRTP and SRTCP (RFC 3711 AES-CM/HMAC-SHA1 and RFC 7714 AES-GCM), keyed from the DTLS exporter.

The controller offers ``SRTP_AES128_CM_SHA1_80``.  With ``raw_dtls`` sessions (what the firmware
actually uses) no SRTP flows: traffic rides as DTLS application data instead.  The profiles are
implemented for completeness and for the fake controller.
"""

from __future__ import annotations

import hashlib
import hmac
import struct

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

#: profile name -> (cipher family, key length, salt length, auth key length, tag length)
SRTP_PROFILES: dict[str, tuple[str, int, int, int, int]] = {
    "SRTP_AES128_CM_SHA1_80": ("aes_cm", 16, 14, 20, 10),
    "SRTP_AES128_CM_SHA1_32": ("aes_cm", 16, 14, 20, 4),
    "SRTP_AEAD_AES_128_GCM": ("aes_gcm", 16, 12, 0, 16),
    "SRTP_AEAD_AES_256_GCM": ("aes_gcm", 32, 12, 0, 16),
}


class SrtpError(Exception):
    pass


def aes_ctr_keystream(key: bytes, iv: bytes, n: int) -> bytes:
    enc = Cipher(algorithms.AES(key), modes.CTR(iv)).encryptor()
    return enc.update(b"\0" * n) + enc.finalize()


def srtp_kdf(master_key: bytes, master_salt: bytes, label: int, length: int) -> bytes:
    """RFC 3711 §4.3.1 with key_derivation_rate 0 (libsrtp semantics: the label
    is XORed into byte 7 of the salt, which is zero-padded to 14 bytes)."""
    if length == 0:
        return b""
    x = int.from_bytes(master_salt.ljust(14, b"\0"), "big") ^ (label << 48)
    return aes_ctr_keystream(master_key, (x << 16).to_bytes(16, "big"), length)


def rtp_header_length(data: bytes) -> int:
    if len(data) < 12:
        raise SrtpError("short RTP packet")
    cc = data[0] & 0x0F
    n = 12 + 4 * cc
    if data[0] & 0x10:
        if len(data) < n + 4:
            raise SrtpError("short RTP extension")
        n += 4 + 4 * struct.unpack_from("!H", data, n + 2)[0]
    if n > len(data):
        raise SrtpError("RTP header longer than packet")
    return n


class SrtpContext:
    """One direction of SRTP/SRTCP (keys for the sender of the packets)."""

    def __init__(self, profile: str, master_key: bytes, master_salt: bytes) -> None:
        self.profile = profile
        self.cipher, klen, slen, alen, self.tag_len = SRTP_PROFILES[profile]
        if len(master_key) != klen or len(master_salt) != slen:
            raise SrtpError(f"master key/salt length mismatch for {profile}")

        def kdf(label: int, n: int) -> bytes:
            return srtp_kdf(master_key, master_salt, label, n)

        self.rtp_key, self.rtp_auth, self.rtp_salt = kdf(0, klen), kdf(1, alen), kdf(2, slen)
        self.rtcp_key, self.rtcp_auth, self.rtcp_salt = kdf(3, klen), kdf(4, alen), kdf(5, slen)
        self.rx: dict[int, list[int]] = {}  # ssrc -> [roc, s_l]
        self.tx: dict[int, list[int]] = {}  # ssrc -> [roc, last_seq] (-1 before the first packet)
        self.rtcp_tx_index = 0

    # -- index handling (RFC 3711 §3.3.1) --
    def _rx_roc(self, ssrc: int, seq: int) -> int:
        st = self.rx.get(ssrc)
        if st is None:
            return 0
        roc, s_l = st
        if s_l < 32768:
            return (roc - 1) & 0xFFFFFFFF if seq - s_l > 32768 else roc
        return (roc + 1) & 0xFFFFFFFF if s_l - 32768 > seq else roc

    def _rx_update(self, ssrc: int, roc: int, seq: int) -> None:
        st = self.rx.get(ssrc)
        if st is None:
            self.rx[ssrc] = [roc, seq]
            return
        if roc == st[0] and seq > st[1]:
            st[1] = seq
        elif roc == (st[0] + 1) & 0xFFFFFFFF:
            st[0], st[1] = roc, seq

    @staticmethod
    def _cm_iv(salt: bytes, ssrc: int, index: int) -> bytes:
        return ((int.from_bytes(salt, "big") << 16) ^ (ssrc << 64) ^ (index << 16)).to_bytes(16, "big")

    @staticmethod
    def _gcm_rtp_iv(salt: bytes, ssrc: int, roc: int, seq: int) -> bytes:
        raw = struct.pack("!HIIH", 0, ssrc, roc, seq)
        return bytes(a ^ b for a, b in zip(raw, salt, strict=False))

    @staticmethod
    def _gcm_rtcp_iv(salt: bytes, ssrc: int, index: int) -> bytes:
        raw = struct.pack("!HIHI", 0, ssrc, 0, index & 0x7FFFFFFF)
        return bytes(a ^ b for a, b in zip(raw, salt, strict=False))

    # -- RTP --
    def unprotect_rtp(self, pkt: bytes) -> bytes:
        hdr_len = rtp_header_length(pkt)
        seq = struct.unpack_from("!H", pkt, 2)[0]
        ssrc = struct.unpack_from("!I", pkt, 8)[0]
        roc = self._rx_roc(ssrc, seq)
        if self.cipher == "aes_cm":
            if len(pkt) < hdr_len + self.tag_len:
                raise SrtpError("packet shorter than auth tag")
            body, tag = pkt[: -self.tag_len], pkt[-self.tag_len :]
            mac = hmac.new(self.rtp_auth, body + struct.pack("!I", roc), hashlib.sha1).digest()[: self.tag_len]
            if not hmac.compare_digest(mac, tag):
                raise SrtpError(f"SRTP authentication failed (ssrc={ssrc:08x} seq={seq})")
            iv = self._cm_iv(self.rtp_salt, ssrc, (roc << 16) | seq)
            dec = Cipher(algorithms.AES(self.rtp_key), modes.CTR(iv)).decryptor()
            plain = dec.update(body[hdr_len:]) + dec.finalize()
            self._rx_update(ssrc, roc, seq)
            return body[:hdr_len] + plain
        aad, ct = pkt[:hdr_len], pkt[hdr_len:]
        try:
            plain = AESGCM(self.rtp_key).decrypt(self._gcm_rtp_iv(self.rtp_salt, ssrc, roc, seq), ct, aad)
        except InvalidTag:
            raise SrtpError(f"SRTP GCM authentication failed (ssrc={ssrc:08x} seq={seq})") from None
        self._rx_update(ssrc, roc, seq)
        return aad + plain

    def protect_rtp(self, pkt: bytes) -> bytes:
        hdr_len = rtp_header_length(pkt)
        seq = struct.unpack_from("!H", pkt, 2)[0]
        ssrc = struct.unpack_from("!I", pkt, 8)[0]
        st = self.tx.setdefault(ssrc, [0, -1])
        if st[1] >= 0 and seq < st[1]:
            st[0] = (st[0] + 1) & 0xFFFFFFFF
        st[1] = seq
        roc = st[0]
        if self.cipher == "aes_cm":
            iv = self._cm_iv(self.rtp_salt, ssrc, (roc << 16) | seq)
            enc = Cipher(algorithms.AES(self.rtp_key), modes.CTR(iv)).encryptor()
            body = pkt[:hdr_len] + enc.update(pkt[hdr_len:]) + enc.finalize()
            tag = hmac.new(self.rtp_auth, body + struct.pack("!I", roc), hashlib.sha1).digest()[: self.tag_len]
            return body + tag
        aad = pkt[:hdr_len]
        return aad + AESGCM(self.rtp_key).encrypt(self._gcm_rtp_iv(self.rtp_salt, ssrc, roc, seq), pkt[hdr_len:], aad)

    # -- RTCP --
    def unprotect_rtcp(self, pkt: bytes) -> bytes:
        if len(pkt) < 8 + 4 + self.tag_len:
            raise SrtpError("short SRTCP packet")
        ssrc = struct.unpack_from("!I", pkt, 4)[0]
        if self.cipher == "aes_cm":
            body, tag = pkt[: -self.tag_len], pkt[-self.tag_len :]
            mac = hmac.new(self.rtcp_auth, body, hashlib.sha1).digest()[: self.tag_len]
            if not hmac.compare_digest(mac, tag):
                raise SrtpError("SRTCP authentication failed")
            e_idx = struct.unpack("!I", body[-4:])[0]
            enc_part = body[8:-4]
            if not e_idx >> 31:
                return body[:-4]
            iv = self._cm_iv(self.rtcp_salt, ssrc, e_idx & 0x7FFFFFFF)
            dec = Cipher(algorithms.AES(self.rtcp_key), modes.CTR(iv)).decryptor()
            return body[:8] + dec.update(enc_part) + dec.finalize()
        e_idx = struct.unpack("!I", pkt[-4:])[0]
        aad = pkt[:8] + pkt[-4:]
        ct = pkt[8:-4]
        try:
            plain = AESGCM(self.rtcp_key).decrypt(self._gcm_rtcp_iv(self.rtcp_salt, ssrc, e_idx), ct, aad)
        except InvalidTag:
            raise SrtpError("SRTCP GCM authentication failed") from None
        return pkt[:8] + plain

    def protect_rtcp(self, pkt: bytes) -> bytes:
        ssrc = struct.unpack_from("!I", pkt, 4)[0]
        self.rtcp_tx_index = (self.rtcp_tx_index + 1) & 0x7FFFFFFF
        e_idx = struct.pack("!I", 0x80000000 | self.rtcp_tx_index)
        if self.cipher == "aes_cm":
            iv = self._cm_iv(self.rtcp_salt, ssrc, self.rtcp_tx_index)
            enc = Cipher(algorithms.AES(self.rtcp_key), modes.CTR(iv)).encryptor()
            body = pkt[:8] + enc.update(pkt[8:]) + enc.finalize() + e_idx
            return body + hmac.new(self.rtcp_auth, body, hashlib.sha1).digest()[: self.tag_len]
        aad = pkt[:8] + e_idx
        ct = AESGCM(self.rtcp_key).encrypt(self._gcm_rtcp_iv(self.rtcp_salt, ssrc, self.rtcp_tx_index), pkt[8:], aad)
        return pkt[:8] + ct + e_idx
