"""Unit tests for stadiacast.rtc: SRTP, STUN, RTP, a DTLS loopback and the ICE check cadence (no network)."""

from __future__ import annotations

import asyncio
import os

import pytest

from stadiacast.rtc import (
    ATTR_FINGERPRINT,
    ATTR_MESSAGE_INTEGRITY,
    ATTR_PRIORITY,
    ATTR_USERNAME,
    ATTR_XOR_MAPPED_ADDRESS,
    BINDING_REQUEST,
    BINDING_SUCCESS,
    SRTP_PROFILES,
    DtlsCertificate,
    DtlsSession,
    RtcEndpoint,
    SrtpContext,
    SrtpError,
    StunMessage,
    aes_ctr_keystream,
    build_rtp,
    classify_datagram,
    parse_rtp,
    srtp_kdf,
)

# ---- SRTP ---------------------------------------------------------------------


def test_srtp_kdf_rfc3711_appendix_b3():
    master_key = bytes.fromhex("E1F97A0D3E018BE0D64FA32C06DE4139")
    master_salt = bytes.fromhex("0EC675AD498AFEEBB6960B3AABE6")
    assert srtp_kdf(master_key, master_salt, 0, 16).hex().upper() == "C61E7A93744F39EE10734AFE3FF7A087"
    assert srtp_kdf(master_key, master_salt, 2, 14).hex().upper() == "30CBBC08863D8C85D49DB34A9AE1"
    assert srtp_kdf(master_key, master_salt, 1, 20).hex().upper() == "CEBE321F6FF7716B6FD4AB49AF256A156D38BAA4"


def test_aes_cm_keystream_rfc3711_appendix_b2():
    key = bytes.fromhex("2B7E151628AED2A6ABF7158809CF4F3C")
    salt = bytes.fromhex("F0F1F2F3F4F5F6F7F8F9FAFBFCFD")
    iv = SrtpContext._cm_iv(salt, 0, 0)
    ks = aes_ctr_keystream(key, iv, 48).hex().upper()
    assert ks == ("E03EAD0935C95E80E166B16DD92B4EB4D23513162B02D0F72A43A2FE4A5F97AB41E95B3BB0A2E8DD477901E4FCA894C0")


@pytest.mark.parametrize("profile", list(SRTP_PROFILES))
def test_srtp_roundtrip_and_tamper(profile):
    _, klen, slen, _, tag = SRTP_PROFILES[profile]
    key, salt = os.urandom(klen), os.urandom(slen)
    tx, rx = SrtpContext(profile, key, salt), SrtpContext(profile, key, salt)
    for seq in (65530, 65531, 65535, 0, 1, 2):  # crosses the ROC boundary
        pkt = build_rtp(96, seq, 1000 + seq, 0xDEADBEEF, b"payload-%d" % seq, extensions=[(3, b"\x00\x07")])
        prot = tx.protect_rtp(pkt)
        assert len(prot) == len(pkt) + tag
        assert rx.unprotect_rtp(prot) == pkt
    bad = bytearray(tx.protect_rtp(build_rtp(96, 3, 1003, 0xDEADBEEF, b"x")))
    bad[-1] ^= 1
    with pytest.raises(SrtpError):
        rx.unprotect_rtp(bytes(bad))
    rtcp = bytes([0x80, 200, 0, 6]) + (0xDEADBEEF).to_bytes(4, "big") + os.urandom(20)
    assert rx.unprotect_rtcp(tx.protect_rtcp(rtcp)) == rtcp


# ---- STUN ---------------------------------------------------------------------

RFC5769_REQUEST = bytes.fromhex(
    "000100582112a442b7e7a701bc34d686fa87dfae"
    "802200105354554e207465737420636c69656e74"
    "002400046e0001ff"
    "80290008932ff9b151263b36"
    "000600096576746a3a68367659202020"
    "000800149aeaa70cbfd8cb56781ef2b5b2d3f249c1b571a2"
    "80280004e57a3bcf"
)


def test_stun_rfc5769_vector_parses_and_verifies():
    msg = StunMessage.parse(RFC5769_REQUEST)
    assert msg.msg_type == BINDING_REQUEST
    assert msg.get(ATTR_USERNAME) == b"evtj:h6vY"
    assert msg.check_fingerprint()
    assert msg.check_integrity(b"VOkJxbRl1RmTxUk/WvJxBt")
    assert not msg.check_integrity(b"wrong")


def test_stun_roundtrip():
    req = StunMessage(BINDING_REQUEST).add(ATTR_USERNAME, b"abcd:efgh").add(ATTR_PRIORITY, b"\x00\x00\x00\x01")
    data = req.encode(b"secret")
    assert classify_datagram(data) == "stun"
    back = StunMessage.parse(data)
    assert back.check_fingerprint()
    assert back.check_integrity(b"secret")
    assert back.get(ATTR_MESSAGE_INTEGRITY)
    assert back.get(ATTR_FINGERPRINT)
    resp = StunMessage(BINDING_SUCCESS, back.txid).add(
        ATTR_XOR_MAPPED_ADDRESS, StunMessage.xor_address("192.168.2.2", 54237)
    )
    rb = StunMessage.parse(resp.encode(b"secret"))
    assert rb.txid == req.txid
    assert StunMessage.parse_xor_address(rb.get(ATTR_XOR_MAPPED_ADDRESS)) == ("192.168.2.2", 54237)


# ---- RTP ----------------------------------------------------------------------


def test_rtp_build_parse():
    pkt = build_rtp(97, 1234, 90000, 0x01020304, b"hello", marker=True, extensions=[(3, b"\x12\x34"), (5, b"\x01")])
    assert classify_datagram(pkt) == "rtp"
    p = parse_rtp(pkt)
    assert (p.payload_type, p.seq, p.timestamp, p.ssrc, p.marker) == (97, 1234, 90000, 0x01020304, True)
    assert p.extensions == [(3, b"\x12\x34"), (5, b"\x01")]
    assert p.payload == b"hello"
    assert classify_datagram(bytes([0x80, 200, 0, 1]) + b"\0" * 8) == "rtcp"
    assert classify_datagram(bytes([22, 254, 253]) + b"\0" * 20) == "dtls"


# ---- DTLS loopback -----------------------------------------------------------


def test_dtls_loopback_exports_matching_srtp_keys():
    a, b = DtlsCertificate("a"), DtlsCertificate("b")
    cli, srv = DtlsSession(a, "client"), DtlsSession(b, "server")
    pending = [(srv, d) for d in cli.start()]
    for _ in range(50):
        if not pending:
            break
        nxt = []
        for target, d in pending:
            out, _ = target.feed(d)
            other = cli if target is srv else srv
            nxt += [(other, o) for o in out]
        pending = nxt
    assert cli.handshake_complete
    assert srv.handshake_complete
    kc, ks = cli.export_srtp_keys(), srv.export_srtp_keys()
    assert kc == ks
    assert kc.profile == "SRTP_AES128_CM_SHA1_80"
    assert cli.peer_fingerprint.replace(":", "") == b.fingerprint.replace(":", "")
    c2s, s_rx = (
        SrtpContext(kc.profile, kc.client_key, kc.client_salt),
        SrtpContext(ks.profile, ks.client_key, ks.client_salt),
    )
    pkt = build_rtp(96, 1, 1, 7, b"input")
    assert s_rx.unprotect_rtp(c2s.protect_rtp(pkt)) == pkt


# ---- ICE check cadence before DTLS ----------------------------------------------


class _CaptureTransport:
    def __init__(self):
        self.sent = []

    def sendto(self, data, addr):
        self.sent.append((data, addr))

    def get_extra_info(self, key):
        return ("127.0.0.1", 40000) if key == "sockname" else None

    def close(self):
        pass


def _binding_requests_sent_after_pairing(fast_check_interval: float, wait: float) -> int:
    async def run() -> int:
        ep = RtcEndpoint(
            cert=DtlsCertificate(), controlling=False, dtls_role="server", fast_check_interval=fast_check_interval
        )
        ep.connected = asyncio.Event()
        tr = _CaptureTransport()
        ep.connection_made(tr)
        ep.set_remote("ctrl", "controller-password-0123", None)
        peer = ("192.168.2.2", 49153)
        req = StunMessage(BINDING_REQUEST).add(ATTR_USERNAME, f"{ep.ufrag}:ctrl".encode())
        ep.datagram_received(req.encode(ep.pwd.encode()), peer)  # selects the pair
        await asyncio.sleep(wait)
        ep.close()
        return sum(
            1
            for data, addr in tr.sent
            if addr == peer
            and classify_datagram(data) == "stun"
            and StunMessage.parse(data).msg_type == BINDING_REQUEST
        )

    return asyncio.run(run())


def test_fast_ice_checks_until_dtls():
    # default: one check right after pairing, the next only with the 2.5 s keepalive
    assert _binding_requests_sent_after_pairing(0.0, 0.5) == 1
    # fast: a check every 0.1 s while no DTLS session exists
    assert _binding_requests_sent_after_pairing(0.1, 0.55) >= 5
