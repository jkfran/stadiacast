"""Runtime access to the protobuf schemas the controller speaks.

The ``.descriptor`` files under ``schemas/`` are serialized ``FileDescriptorProto`` blobs loaded
into one ``DescriptorPool`` per :class:`Protos` instance, so no ``protoc`` step is needed; the
``.proto`` next to each is the readable rendering (``schemas/README.md`` says which is the
authority for which schema).  Message classes are created dynamically, which is why they are
typed as ``Any`` here: field access on them cannot be checked statically.

Also here:

* the discovery RPC request / response types and the GMsg push envelope (the controller
  payload rides in protobuf extension 269197544 and the message type must be 42);
* the display-link RPC envelope (``RpcMessage`` request / response / notification, WireHeader2
  ids ``0x74`` / ``0x75``);
* :meth:`Protos.sniff_signaling`, which finds a ``WebRtcSdp`` or a bundle inside an HTTP body;
* ``wire_fields`` / ``decode_unknown``, a schema-less protobuf wire dumper for logging bodies
  whose schema is unknown.

:mod:`stadiacast.input_protocol` decodes the ``InputProtocolMessage`` stream.
"""

from __future__ import annotations

import os
from typing import Any

from google.protobuf import descriptor_pb2, descriptor_pool, json_format, message_factory
from google.protobuf.message import DecodeError

DESCRIPTOR_FILES = (
    "webrtc_sdp.descriptor",
    "input.descriptor",
    "yeti_clientanalytics.descriptor",
    "discovery_api.descriptor",
    "rpc_message.descriptor",
    "controller_message.descriptor",
    "streamer_message.descriptor",
)
PKG_API = "chrome.cloudcast.proto.api.common"
PKG_STREAMING = "chrome.cloudcast.streaming"
PKG_ANALYTICS = "chrome.cloudcast.proto.common"
PKG_DISCO = "gotham.discovery"  # our name: schemas/discovery_api.proto
PKG_RPC = "gotham.rpc"  # our name: the protobuf-lite envelope, schemas/rpc_message.proto
PKG_DISPLAY = "gotham.display"  # our name: schemas/controller_message.proto
PKG_STREAMER = "gotham.streamer"  # our name: the game-phase StreamingClient RPC, schemas/streamer_message.proto

#: ``GmsgMessage.type`` the controller requires before it processes the payload.
GMSG_COMMAND_TYPE = 42

#: ``v1:<Method>`` -> (request type, response type); None = the controller ignores the body.
RPC_TYPES: dict[str, tuple[str, str | None]] = {
    "FirmwareVersionCheck": ("FirmwareVersionCheckRequest", "FirmwareVersionCheckResponse"),
    "RegisterController": ("RegisterControllerRequest", "RegisterControllerResponse"),
    "DiscoverDisplay": ("DiscoverDisplayRequest", "DiscoverDisplayResponse"),
    "ResolveDiscovery": ("ResolveDiscoveryRequest", None),
}


def default_proto_dir() -> str:
    """The package's ``schemas/`` directory."""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "schemas")


class Protos:
    """Message classes for the schemas (build one per process)."""

    def __init__(self, proto_dir: str | None = None) -> None:
        self.proto_dir = proto_dir or default_proto_dir()
        self.pool = descriptor_pool.DescriptorPool()
        for fn in DESCRIPTOR_FILES:
            fdp = descriptor_pb2.FileDescriptorProto()
            with open(os.path.join(self.proto_dir, fn), "rb") as fh:
                fdp.ParseFromString(fh.read())
            # webrtc_sdp.proto imports nanopb.proto for code-generation options
            # only; the options are not needed to (de)serialize messages.
            del fdp.dependency[:]
            self.pool.AddSerializedFile(fdp.SerializeToString())
        g = self.message_class
        self.WebRtcSdp = g(f"{PKG_API}.WebRtcSdp")
        self.WebRtcSdpAndIceCandidates = g(f"{PKG_API}.WebRtcSdpAndIceCandidates")
        self.RtcSdpOffer = g(f"{PKG_API}.RtcSdpOffer")
        self.RtcSdpAnswer = g(f"{PKG_API}.RtcSdpAnswer")
        self.RtcIceCandidate = g(f"{PKG_API}.RtcIceCandidate")
        self.InputProtocolMessage = g(f"{PKG_STREAMING}.InputProtocolMessage")
        self.InputMessageSeries = g(f"{PKG_STREAMING}.InputMessageSeries")
        self.BatchedLogRequest = g(f"{PKG_ANALYTICS}.BatchedLogRequest")
        self.LogResponse = g(f"{PKG_ANALYTICS}.LogResponse")
        self.SDP_OFFER = self.enum(f"{PKG_API}.WebRtcSdp.Type", "SDP_OFFER")
        self.SDP_ANSWER = self.enum(f"{PKG_API}.WebRtcSdp.Type", "SDP_ANSWER")
        self.ICE_CANDIDATE = self.enum(f"{PKG_API}.WebRtcSdp.Type", "ICE_CANDIDATE")
        # discovery API (schemas/discovery_api.proto)
        self.FirmwareVersionCheckRequest = g(f"{PKG_DISCO}.FirmwareVersionCheckRequest")
        self.FirmwareVersionCheckResponse = g(f"{PKG_DISCO}.FirmwareVersionCheckResponse")
        self.RegisterControllerRequest = g(f"{PKG_DISCO}.RegisterControllerRequest")
        self.RegisterControllerResponse = g(f"{PKG_DISCO}.RegisterControllerResponse")
        self.DiscoverDisplayRequest = g(f"{PKG_DISCO}.DiscoverDisplayRequest")
        self.DiscoverDisplayResponse = g(f"{PKG_DISCO}.DiscoverDisplayResponse")
        self.ResolveDiscoveryRequest = g(f"{PKG_DISCO}.ResolveDiscoveryRequest")
        self.GmsgPollRequest = g(f"{PKG_DISCO}.GmsgPollRequest")
        self.GmsgEnvelope = g(f"{PKG_DISCO}.GmsgEnvelope")
        self.ControllerCommand = g(f"{PKG_DISCO}.ControllerCommand")
        self.controller_command_ext = self.pool.FindExtensionByName(f"{PKG_DISCO}.controller_command")
        # display link after DTLS
        self.RpcMessage = g(f"{PKG_RPC}.RpcMessage")
        self.DisplayToController = g(f"{PKG_DISPLAY}.DisplayToController")
        self.ControllerToDisplay = g(f"{PKG_DISPLAY}.ControllerToDisplay")
        self.StreamerRequest = g(f"{PKG_STREAMER}.StreamerRequest")
        self.StreamerResponse = g(f"{PKG_STREAMER}.StreamerResponse")

    def message_class(self, full_name: str) -> Any:
        """The dynamically generated class of one message type (``package.Name``)."""
        return message_factory.GetMessageClass(self.pool.FindMessageTypeByName(full_name))

    # ---- discovery API helpers ---------------------------------------------------
    def rpc_request_class(self, method: str) -> Any | None:
        names = RPC_TYPES.get(method)
        return self.message_class(f"{PKG_DISCO}.{names[0]}") if names else None

    def rpc_response_class(self, method: str) -> Any | None:
        names = RPC_TYPES.get(method)
        return self.message_class(f"{PKG_DISCO}.{names[1]}") if names and names[1] else None

    def parse_rpc_request(self, method: str, body: bytes) -> Any | None:
        """Decode a `v1:<method>` request body; None when the method is unknown."""
        cls = self.rpc_request_class(method)
        if cls is None:
            return None
        m = cls()
        m.ParseFromString(body)  # DecodeError propagates
        return m

    def parse_sdp_bundle(self, data: bytes) -> Any:
        """`DiscoverDisplayRequest.sdp_offer_bundle` / `ControllerCommand.payload` are a
        serialized WebRtcSdpAndIceCandidates."""
        b = self.WebRtcSdpAndIceCandidates()
        b.ParseFromString(data)
        return b

    def gmsg_envelope(self, payload: bytes, msg_id: int, error_code: int | None = None) -> bytes:
        """Server -> controller GMsg carrying `payload` (a WebRtcSdpAndIceCandidates)."""
        env = self.GmsgEnvelope()
        env.message.id = msg_id
        env.message.type = GMSG_COMMAND_TYPE
        cmd = env.message.Extensions[self.controller_command_ext]
        cmd.payload = payload
        if error_code is not None:
            cmd.error_code = error_code
        return bytes(env.SerializeToString())

    def gmsg_command(self, env: Any) -> Any | None:
        """The ControllerCommand extension of a parsed GmsgEnvelope, or None."""
        if not env.HasField("message") or not env.message.HasExtension(self.controller_command_ext):
            return None
        return env.message.Extensions[self.controller_command_ext]

    # ---- display link RPC envelope (WireHeader2 ids 0x74 / 0x75) -------------------
    def rpc_request(self, ticket: int, payload: Any) -> bytes:
        """RpcMessage{request{ticket, payload}}; `payload` is a message or its bytes."""
        m = self.RpcMessage()
        m.request.ticket = ticket
        m.request.payload = payload if isinstance(payload, bytes) else payload.SerializeToString()
        return bytes(m.SerializeToString())

    def rpc_notification(self, ticket: int, payload: Any) -> bytes:
        m = self.RpcMessage()
        m.notification.ticket = ticket
        m.notification.payload = payload if isinstance(payload, bytes) else payload.SerializeToString()
        return bytes(m.SerializeToString())

    def rpc_response(self, ticket: int, payload: Any = None, code: int = 0, message: str = "") -> bytes:
        """RpcMessage{response{...}} shaped like the controller's own responder:
        ticket, code and error_message always, payload only if any."""
        m = self.RpcMessage()
        m.response.ticket = ticket
        m.response.code = code
        m.response.error_message = message
        if payload is not None:
            m.response.payload = payload if isinstance(payload, bytes) else payload.SerializeToString()
        return bytes(m.SerializeToString())

    def parse_rpc(self, data: bytes, payload_type: Any) -> dict[str, Any]:
        """Decode an RpcMessage and its payload (`payload_type` = the
        ControllerMessage class the sender uses).  Returns {kind, ticket, code,
        error_message, payload (message or None), payload_error, raw}."""
        m = self.RpcMessage()
        m.ParseFromString(data)  # DecodeError propagates
        kind = m.WhichOneof("payload")
        out: dict[str, Any] = {"kind": kind, "ticket": None, "payload": None, "payload_error": None, "raw": m}
        if kind is None:
            return out
        body = getattr(m, kind)
        out["ticket"] = body.ticket if body.HasField("ticket") else None
        if kind == "response":
            out["code"] = body.code
            out["error_message"] = body.error_message
        if body.HasField("payload"):
            msg = payload_type()
            try:
                msg.ParseFromString(body.payload)
                out["payload"] = msg
            except DecodeError as e:
                out["payload_error"] = str(e)
                out["payload_raw"] = body.payload
        return out

    def enum(self, full_name: str, value_name: str) -> int:
        return self.pool.FindEnumTypeByName(full_name).values_by_name[value_name].number

    def enum_name(self, full_name: str, number: int) -> str:
        v = self.pool.FindEnumTypeByName(full_name).values_by_number.get(number)
        return v.name if v else str(number)

    @staticmethod
    def to_dict(msg: Any) -> dict[str, Any]:
        """A JSON-friendly dict of a message, with the ``.proto`` field names."""
        return json_format.MessageToDict(msg, preserving_proto_field_name=True)

    # ---- signaling detection -------------------------------------------------
    def sdp_plausible(self, m: Any) -> bool:
        if m.type == self.SDP_OFFER:
            return bool(m.sdp_offer.ice_ufrag)
        if m.type == self.SDP_ANSWER:
            return bool(m.sdp_answer.ice_ufrag)
        if m.type == self.ICE_CANDIDATE:
            return bool(m.ice_candidate.local_endpoint_ip or m.raw_value)
        return False

    def sniff_signaling(self, data: bytes, depth: int = 0) -> tuple[str, Any] | None:
        """Return ("bundle"|"sdp", message) when `data`, or a length-delimited
        field nested inside it (the RPC envelope is unknown), is a plausible
        WebRtcSdp / WebRtcSdpAndIceCandidates. None otherwise."""
        if not data:
            return None
        for kind, cls in (("bundle", self.WebRtcSdpAndIceCandidates), ("sdp", self.WebRtcSdp)):
            m = cls()
            try:
                m.ParseFromString(data)
            except DecodeError:
                continue
            if kind == "bundle":
                if m.HasField("sdp") and self.sdp_plausible(m.sdp):
                    return kind, m
            elif self.sdp_plausible(m):
                return kind, m
        if depth < 3:
            try:
                fields = wire_fields(data)
            except ValueError:
                return None
            for _, wt, v in fields:
                if wt == 2 and len(v) > 8:
                    r = self.sniff_signaling(v, depth + 1)
                    if r:
                        return r
        return None


# ---------------------------------------------------------------------------
# Schema-less wire dumper
# ---------------------------------------------------------------------------


def _varint(b: bytes, i: int) -> tuple[int, int]:
    shift = val = 0
    while True:
        if i >= len(b):
            raise ValueError("truncated varint")
        c = b[i]
        i += 1
        val |= (c & 0x7F) << shift
        shift += 7
        if not c & 0x80:
            return val, i
        if shift > 70:
            raise ValueError("varint too long")


def wire_fields(data: bytes) -> list[tuple[int, int, Any]]:
    """Split one protobuf message into (field_number, wire_type, value) triples.
    Raises ValueError when the bytes are not a well-formed message."""
    out: list[tuple[int, int, Any]] = []
    i = 0
    while i < len(data):
        tag, i = _varint(data, i)
        f, wt = tag >> 3, tag & 7
        v: Any
        if f == 0:
            raise ValueError("field number 0")
        if wt == 0:
            v, i = _varint(data, i)
        elif wt == 1:
            v = data[i : i + 8]
            i += 8
            if len(v) < 8:
                raise ValueError("truncated fixed64")
        elif wt == 2:
            ln, i = _varint(data, i)
            v = data[i : i + ln]
            i += ln
            if len(v) < ln:
                raise ValueError("truncated length-delimited field")
        elif wt == 5:
            v = data[i : i + 4]
            i += 4
            if len(v) < 4:
                raise ValueError("truncated fixed32")
        else:
            raise ValueError(f"unsupported wire type {wt}")
        out.append((f, wt, v))
    return out


def printable(b: bytes) -> str | None:
    """``b`` decoded as text when it is printable UTF-8, else None."""
    try:
        s = b.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if all(ch.isprintable() or ch in "\r\n\t" for ch in s):
        return s
    return None


def decode_unknown(data: bytes, depth: int = 0, max_depth: int = 4) -> list[dict[str, Any]] | None:
    """Best-effort structural dump of a protobuf message with unknown schema."""
    try:
        fields = wire_fields(data)
    except ValueError:
        return None
    out: list[dict[str, Any]] = []
    for f, wt, v in fields:
        if wt == 0:
            out.append({"f": f, "varint": v})
        elif wt == 1:
            out.append({"f": f, "fixed64": int.from_bytes(v, "little"), "hex": v.hex()})
        elif wt == 5:
            out.append({"f": f, "fixed32": int.from_bytes(v, "little"), "hex": v.hex()})
        else:
            item: dict[str, Any] = {"f": f, "len": len(v)}
            text = printable(v) if v else ""
            nested = decode_unknown(v, depth + 1, max_depth) if (v and depth < max_depth) else None
            if text is not None and (nested is None or len(v) < 4 or (text.isascii() and text.strip())):
                item["str"] = text
            elif nested is not None:
                item["msg"] = nested
            else:
                item["hex"] = v[:64].hex() + ("…" if len(v) > 64 else "")
            out.append(item)
    return out
