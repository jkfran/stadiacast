"""stadiacast.protos and stadiacast.input_protocol: schema bytes, the GMsg and RPC envelopes, signaling
detection, the wire dumper and the input-stream decoder (no network)."""

from __future__ import annotations

import zlib

import pytest
from google.protobuf.message import EncodeError

from stadiacast.input_protocol import InputDecoder
from stadiacast.protos import GMSG_COMMAND_TYPE, Protos, decode_unknown, wire_fields
from stadiacast.rtc import WIRE_ID_DC_RPC_DISPLAY, WIRE_ID_RPC, pack_wire_message, parse_wire_message, wire_id_valid


def test_discovery_schemas():
    p = Protos()
    # RegisterControllerResponse: field 2 is required by the firmware handler.
    r = p.RegisterControllerResponse()
    r.registration_id = "x"
    with pytest.raises(EncodeError):
        r.SerializeToString()
    r.poll_interval_ms = 1000
    r.registration_token = "tok"
    assert [(f, w) for f, w, _ in wire_fields(r.SerializeToString())] == [(1, 2), (2, 0), (3, 2)]
    partial = p.RegisterControllerResponse()
    partial.ParseFromString(b"\x0a\x01x")  # a body without the required field is "not initialized"
    assert not partial.IsInitialized()
    assert not partial.HasField("poll_interval_ms")
    # DiscoverDisplayResponse: field 4 (max 63) required non-empty.
    d = p.DiscoverDisplayResponse()
    d.display_id, d.display_token = "disp", "tok"
    assert [(f, w) for f, w, _ in wire_fields(d.SerializeToString())] == [(1, 2), (4, 2)]
    # DiscoverDisplayRequest: offer bundle in field 4, typed keys in oneof field 3.
    q = p.DiscoverDisplayRequest()
    q.device_id, q.app_id, q.attempt_id = "dev", "stadia", "att"
    q.typed_keys.key.extend([1, 2])
    q.sdp_offer_bundle = b"\x0a\x00"
    fields = [(f, w) for f, w, _ in wire_fields(q.SerializeToString())]
    assert set(fields) == {(1, 2), (3, 2), (4, 2), (6, 2), (7, 2)}
    assert p.parse_rpc_request("DiscoverDisplay", q.SerializeToString()).typed_keys.key == [1, 2]
    # GMsg envelope: message{id, type=42, extension 269197544{payload}}.
    raw = p.gmsg_envelope(b"\x0a\x02hi", 7)
    outer = wire_fields(raw)
    assert [(f, w) for f, w, _ in outer] == [(1, 2)]
    inner = [(f, w) for f, w, _ in wire_fields(outer[0][2])]
    assert inner == [(1, 0), (2, 0), (269197544, 2)]
    env = p.GmsgEnvelope()
    env.ParseFromString(raw)
    assert env.message.id == 7
    assert env.message.type == GMSG_COMMAND_TYPE
    assert p.gmsg_command(env).payload == b"\x0a\x02hi"
    assert p.gmsg_command(p.GmsgEnvelope()) is None
    # GMsg poll with an ack.
    poll = p.GmsgPollRequest()
    poll.subscription, poll.ack = "tok", True
    poll.last_message.id = 7
    assert [(f, w) for f, w, _ in wire_fields(poll.SerializeToString())] == [(1, 2), (2, 2), (4, 0)]
    # ResolveDiscovery has no response schema (body ignored by the firmware).
    assert p.rpc_response_class("ResolveDiscovery") is None


def test_wire_header_and_rpc_envelope_bytes():
    # WireHeader2: 0, 0, id LE16, total length LE24 incl. header, 0
    rec = pack_wire_message(WIRE_ID_DC_RPC_DISPLAY, b"\x01\x02")
    assert rec == bytes.fromhex("000075000a000000") + b"\x01\x02"
    assert parse_wire_message(rec) == (0x75, b"\x01\x02")
    assert parse_wire_message(b"\x0f\xff" + rec[2:7] + b"\xff" + rec[8:]) == (0x75, b"\x01\x02")  # ignored bits
    for bad in (rec[:7], b"\x10" + rec[1:], rec + b"\x00"):
        with pytest.raises(ValueError):
            parse_wire_message(bad)
    # ids 1..255 and the keepalive ids 0xfffe/0xffff only.  0xFFFA (the
    # game-phase muxed RPC id) is dropped by the display link (seen live: 200 requests, 0 replies)
    assert [i for i in (0, 1, 0x74, 0x75, 0xFF, 0x100, 0xFFFA, 0xFFFD, 0xFFFE, 0xFFFF) if wire_id_valid(i)] == [
        1,
        0x74,
        0x75,
        0xFF,
        0xFFFE,
        0xFFFF,
    ]
    with pytest.raises(ValueError):
        pack_wire_message(WIRE_ID_RPC, b"\x01\x02")
    with pytest.raises(ValueError):
        parse_wire_message(bytes.fromhex("0000faff0a000000") + b"\x01\x02")
    # RpcMessage{request{ticket 1, payload DisplayToController{#2 {f2: false}}}}
    p = Protos()
    msg = p.DisplayToController()
    msg.get_controller_state.display_supports_send_log_report = False
    assert p.rpc_request(1, msg).hex() == "0a080801120412021000"
    # the controller's responder shape: ticket, code, message always; payload when present
    reply = p.DisplayToController()
    reply.current_time.controller_time_us, reply.current_time.display_time_us = 5, 6
    raw = p.rpc_response(7, reply)
    assert [(f, w) for f, w, _ in wire_fields(wire_fields(raw)[0][2])] == [(1, 0), (2, 0), (3, 2), (4, 2)]
    rpc = p.parse_rpc(raw, p.DisplayToController)
    assert (rpc["kind"], rpc["ticket"], rpc["code"]) == ("response", 7, 0)
    assert rpc["payload"].current_time.controller_time_us == 5
    # ControllerToDisplay: GetCurrentTimeRequest is oneof variant 18, keepalive 14
    req = p.ControllerToDisplay()
    req.get_current_time.controller_time_us = 1
    assert wire_fields(req.SerializeToString())[0][0] == 18
    ka = p.ControllerToDisplay()
    ka.keepalive.SetInParent()
    assert ka.SerializeToString() == b"\x72\x00"


def test_input_decoder_events_and_compression():
    p = Protos()
    dec = InputDecoder(p)

    def series(seq, buttons, lx=0, compressed=False):
        s = p.InputMessageSeries()
        s.session_id, s.sequence_number, s.base_timestamp_us = "sess", seq, 1_000_000
        m = s.messages.add()
        m.relative_time_us = 500
        g = m.gamepad_message_v2
        g.id, g.buttons, g.axis_left_x = 0, buttons, lx
        msg = p.InputProtocolMessage()
        if compressed:
            raw = s.SerializeToString()
            msg.compressed_series, msg.uncompressed_size = zlib.compress(raw), len(raw)
        else:
            msg.input_message_series.CopyFrom(s)
        return msg.SerializeToString()

    r1 = dec.decode(series(1, 0x10, lx=-300))
    assert r1["sequence_number"] == 1
    assert r1["events"][0]["pressed"] == [4]
    assert r1["events"][0]["t_us"] == 1_000_500
    r2 = dec.decode(series(2, 0x30, compressed=True))
    assert r2["compressed"]
    assert r2["events"][0]["pressed"] == [5]
    assert r2["events"][0]["released"] == []
    assert "lx" in r2["events"][0]["axes_changed"]  # -300 -> 0
    r3 = dec.decode(series(3, 0x00))
    assert r3["events"][0]["released"] == [4, 5]
    assert r3["events"][0]["axes_changed"] == []
    ack = p.InputProtocolMessage()
    ack.ack_sequence_number = 3
    assert dec.decode(ack.SerializeToString())["ack_sequence_number"] == 3


def test_sniff_signaling_nested_and_decode_unknown():
    p = Protos()
    m = p.WebRtcSdp()
    m.type = p.SDP_ANSWER
    m.sdp_answer.ice_ufrag, m.sdp_answer.ice_password = "u1", "p1"
    raw = m.SerializeToString()
    envelope = b"\x0a\x03abc" + b"\x1a" + bytes([len(raw)]) + raw
    kind, found = p.sniff_signaling(envelope)
    assert kind == "sdp"
    assert found.sdp_answer.ice_ufrag == "u1"
    assert p.sniff_signaling(b"\x08\x01") is None
    d = decode_unknown(envelope)
    assert d[0]["str"] == "abc"
    assert d[1]["msg"][0]["varint"] == 2
