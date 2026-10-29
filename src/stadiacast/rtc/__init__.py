"""The RTC side of the controller's sessions: ICE/STUN, DTLS-SRTP, RTP and WireHeader2 framing.

What the controller does on the wire: STUN binding requests *and* responses with
``MESSAGE-INTEGRITY`` and ``FINGERPRINT`` (first valid peer address wins), DTLS 1.2 with the
``use_srtp`` extension offering ``SRTP_AES128_CM_SHA1_80`` and keys exported with the standard
``EXTRACTOR-dtls_srtp`` label, then either SRTP or, with ``raw_dtls`` (what the firmware offers),
WireHeader2 messages as DTLS application data.  :class:`RtcEndpoint` is the matching peer,
built on the standard library, ``cryptography`` and ``pyOpenSSL``; the submodules hold one
protocol each.
"""

from stadiacast.rtc.dtls import (
    DEFAULT_SRTP_PROFILES,
    DtlsCertificate,
    DtlsSession,
    SrtpKeys,
    normalize_fingerprint,
    split_dtls_records,
)
from stadiacast.rtc.endpoint import RemotePeer, RtcEndpoint, classify_datagram
from stadiacast.rtc.rtp import RtpPacket, build_rtp, parse_header_extensions, parse_rtp
from stadiacast.rtc.srtp import SRTP_PROFILES, SrtpContext, SrtpError, aes_ctr_keystream, rtp_header_length, srtp_kdf
from stadiacast.rtc.stun import (
    ATTR_ERROR_CODE,
    ATTR_FINGERPRINT,
    ATTR_ICE_CONTROLLED,
    ATTR_ICE_CONTROLLING,
    ATTR_MAPPED_ADDRESS,
    ATTR_MESSAGE_INTEGRITY,
    ATTR_PRIORITY,
    ATTR_SOFTWARE,
    ATTR_UNKNOWN_ATTRIBUTES,
    ATTR_USE_CANDIDATE,
    ATTR_USERNAME,
    ATTR_XOR_MAPPED_ADDRESS,
    BINDING_ERROR,
    BINDING_INDICATION,
    BINDING_REQUEST,
    BINDING_SUCCESS,
    HOST_CANDIDATE_PRIORITY,
    STUN_MAGIC,
    STUN_MAGIC_BYTES,
    StunMessage,
    random_ice_string,
)
from stadiacast.rtc.wire import (
    WIRE_HEADER_LEN,
    WIRE_ID_DC_RPC_CONTROLLER,
    WIRE_ID_DC_RPC_DISPLAY,
    WIRE_ID_RPC,
    WIRE_ID_RTP,
    WIRE_IDS_DC_RPC,
    pack_wire_message,
    parse_wire_message,
    wire_id_valid,
)

__all__ = [
    "ATTR_ERROR_CODE",
    "ATTR_FINGERPRINT",
    "ATTR_ICE_CONTROLLED",
    "ATTR_ICE_CONTROLLING",
    "ATTR_MAPPED_ADDRESS",
    "ATTR_MESSAGE_INTEGRITY",
    "ATTR_PRIORITY",
    "ATTR_SOFTWARE",
    "ATTR_UNKNOWN_ATTRIBUTES",
    "ATTR_USERNAME",
    "ATTR_USE_CANDIDATE",
    "ATTR_XOR_MAPPED_ADDRESS",
    "BINDING_ERROR",
    "BINDING_INDICATION",
    "BINDING_REQUEST",
    "BINDING_SUCCESS",
    "DEFAULT_SRTP_PROFILES",
    "HOST_CANDIDATE_PRIORITY",
    "SRTP_PROFILES",
    "STUN_MAGIC",
    "STUN_MAGIC_BYTES",
    "WIRE_HEADER_LEN",
    "WIRE_IDS_DC_RPC",
    "WIRE_ID_DC_RPC_CONTROLLER",
    "WIRE_ID_DC_RPC_DISPLAY",
    "WIRE_ID_RPC",
    "WIRE_ID_RTP",
    "DtlsCertificate",
    "DtlsSession",
    "RemotePeer",
    "RtcEndpoint",
    "RtpPacket",
    "SrtpContext",
    "SrtpError",
    "SrtpKeys",
    "StunMessage",
    "aes_ctr_keystream",
    "build_rtp",
    "classify_datagram",
    "normalize_fingerprint",
    "pack_wire_message",
    "parse_header_extensions",
    "parse_rtp",
    "parse_wire_message",
    "random_ice_string",
    "rtp_header_length",
    "split_dtls_records",
    "srtp_kdf",
    "wire_id_valid",
]
