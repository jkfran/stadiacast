"""WebRTC signaling messages in the controller's protobuf dialect.

The controller is the WebRTC *offerer*: its ``DiscoverDisplayRequest`` carries a serialized
``WebRtcSdpAndIceCandidates`` (type ``SDP_OFFER``) and the display's answer bundle reaches it
as the GMsg payload.  The functions here build the offer / answer / candidate messages for
both the display link and the game session; the receiver decides when to send them.
"""

from __future__ import annotations

import json
import re
import secrets
from typing import Any

from google.protobuf import json_format

from stadiacast import SERVER_NAME, __version__
from stadiacast.protos import PKG_API, Protos
from stadiacast.rtc import HOST_CANDIDATE_PRIORITY, RtcEndpoint

# RTP header extensions the firmware knows.
EXT_TWCC_V1 = "http://www.ietf.org/id/draft-holmer-rmcat-transport-wide-cc-extensions-01"
EXT_PLAYOUT_DELAY = "http://www.webrtc.org/experiments/rtp-hdrext/playout-delay"
EXT_CLIENT_INPUT_SEQ = "http://www.webrtc.org/experiments/rtp-hdrext/client-input-sequence"
EXT_AUDIO_LEVEL = "urn:ietf:params:rtp-hdrext:ssrc-audio-level"
EXT_TWCC_V2 = "http://www.webrtc.org/experiments/rtp-hdrext/transport-wide-cc-02"
EXT_COLOR_SPACE = "http://www.webrtc.org/experiments/rtp-hdrext/color-space"

#: The RtcSdpOffer the receiver sends when it is the offerer (legacy ``--signaling-mode offer``).
DEFAULT_OFFER: dict[str, Any] = {
    "rtc_session_version": "2",
    "video_extensions": [
        {"id": 1, "uri": EXT_TWCC_V1},
        {"id": 2, "uri": EXT_PLAYOUT_DELAY},
        {"id": 3, "uri": EXT_CLIENT_INPUT_SEQ},
        {"id": 5, "uri": EXT_TWCC_V2},
        {"id": 6, "uri": EXT_COLOR_SPACE},
    ],
    "audio_extensions": [
        {"id": 1, "uri": EXT_TWCC_V1},
        {"id": 2, "uri": EXT_PLAYOUT_DELAY},
        {"id": 3, "uri": EXT_CLIENT_INPUT_SEQ},
        {"id": 4, "uri": EXT_AUDIO_LEVEL},
        {"id": 5, "uri": EXT_TWCC_V2},
    ],
    "boreal_available": True,
    "boreal_supported_transports": ["BOREAL_SRTP"],
    "video_format": [
        {
            "codec": "VIDEO_CODEC_H264",
            "color_space": "VIDEO_COLOR_SPACE_SRGB",
            "resolution": {"width": 1280, "height": 720},
        }
    ],
    "audio_format": [{"codec": "AUDIO_CODEC_OPUS", "channels": 2, "sample_rate": 48000}],
    "raw_dtls": False,
    "delayed_acks": False,
    "network_type": "NETWORK_TYPE_ETHERNET",
    "use_rtp_timestamps": False,
}

_RAW_CANDIDATE = re.compile(r"\s(\d+\.\d+\.\d+\.\d+)\s+(\d+)\s")


def build_offer(protos: Protos, endpoint: RtcEndpoint, overrides: dict[str, Any] | None = None) -> Any:
    """A ``WebRtcSdp{SDP_OFFER}`` with the endpoint's ICE credentials and DTLS fingerprint."""
    d = json.loads(json.dumps(DEFAULT_OFFER))
    d.update(overrides or {})
    offer = json_format.ParseDict(d, protos.RtcSdpOffer())
    params = endpoint.local_params()
    offer.rtc_session_id = str(secrets.randbits(63))
    offer.ice_ufrag = params["ice_ufrag"]
    offer.ice_password = params["ice_password"]
    offer.fingerprint = params["fingerprint"]
    if not offer.cname:
        offer.cname = secrets.token_hex(8)
    for f in ("audio_ssrc", "video_ssrc", "flexfec_ssrc", "rtx_ssrc", "stream_connect_metadata_ssrc"):
        if not getattr(offer, f):
            setattr(offer, f, secrets.randbits(31) | 1)
    msg = protos.WebRtcSdp()
    msg.type = protos.SDP_OFFER
    msg.sdp_offer.CopyFrom(offer)
    return msg


def build_answer(protos: Protos, offer: Any, endpoint: RtcEndpoint, *, boreal: str = "auto") -> Any:
    """A ``WebRtcSdp{SDP_ANSWER}`` to ``offer`` (an RtcSdpOffer) for ``endpoint``.

    Echoes what must match the offer (raw_dtls, delayed_acks, rtp timestamps, the first
    H264/VP9 video format and the first audio format).  ``boreal="off"`` answers without
    ``use_boreal`` even when the offer advertises it.
    """
    a = protos.RtcSdpAnswer()
    params = endpoint.local_params()
    a.ice_ufrag, a.ice_password, a.fingerprint = params["ice_ufrag"], params["ice_password"], params["fingerprint"]
    srtp = protos.enum(f"{PKG_API}.BorealTransportProtocol", "BOREAL_SRTP")
    if offer.boreal_available and boreal != "off":
        a.use_boreal = True
        supported = list(offer.boreal_supported_transports)
        a.boreal_transport = srtp if (srtp in supported or not supported) else supported[0]
    wanted = {
        protos.enum(f"{PKG_API}.RtcSdpVideoCodec", "VIDEO_CODEC_H264"),
        protos.enum(f"{PKG_API}.RtcSdpVideoCodec", "VIDEO_CODEC_VP9"),
    }
    for vf in offer.video_format:
        if vf.codec in wanted:
            a.video_format.CopyFrom(vf)
            break
    else:
        if offer.video_format:
            a.video_format.CopyFrom(offer.video_format[0])
    if offer.audio_format:
        a.audio_format.CopyFrom(offer.audio_format[0])
    a.raw_dtls = offer.raw_dtls
    a.delayed_acks = offer.delayed_acks
    a.disable_game_stats = True
    a.disable_frame_tokens = True
    a.build_number = f"{SERVER_NAME}/{__version__}"
    a.network_type = protos.enum(f"{PKG_API}.RtcSdpNetworkType", "NETWORK_TYPE_ETHERNET")
    a.use_rtp_timestamps = offer.use_rtp_timestamps
    msg = protos.WebRtcSdp()
    msg.type = protos.SDP_ANSWER
    msg.sdp_answer.CopyFrom(a)
    return msg


def candidate_message(protos: Protos, ip: str, port: int) -> Any:
    """A ``WebRtcSdp{ICE_CANDIDATE}`` for our single host candidate."""
    msg = protos.WebRtcSdp()
    msg.type = protos.ICE_CANDIDATE
    c = msg.ice_candidate
    c.sdp_mid = "0"
    c.sdp_mline_index = 0
    c.foundation = "1"
    c.priority = HOST_CANDIDATE_PRIORITY
    c.local_endpoint_ip = ip
    c.local_endpoint_port = port
    return msg


def bundle(protos: Protos, sdp_msg: Any, candidate: Any) -> bytes:
    """Serialize ``WebRtcSdpAndIceCandidates{sdp, ice_candidate: [candidate]}``."""
    b = protos.WebRtcSdpAndIceCandidates()
    b.sdp.CopyFrom(sdp_msg)
    b.ice_candidate.append(candidate)
    return bytes(b.SerializeToString())


def candidate_address(sdp_msg: Any) -> tuple[str, int] | None:
    """``(ip, port)`` of a ``WebRtcSdp{ICE_CANDIDATE}``, from the structured fields or the raw SDP line."""
    c = sdp_msg.ice_candidate
    ip, port = c.local_endpoint_ip, c.local_endpoint_port
    if not ip and sdp_msg.raw_value:
        m = _RAW_CANDIDATE.search(" " + sdp_msg.raw_value + " ")
        if m:
            ip, port = m.group(1), int(m.group(2))
    return (ip, int(port)) if ip and port else None
