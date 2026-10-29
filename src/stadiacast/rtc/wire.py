"""WireHeader2: the message framing inside DTLS application data of ``raw_dtls`` sessions.

Every DTLS record is one message: an 8-byte header (version nibble, 16-bit message id, 24-bit
total length) and the payload.  The id selects the sub-channel: ``0x66`` carries input, ``0x74`` /
``0x75`` the display-link RPCs, ``0xFFFA`` the game session's own RPC channel.
"""

from __future__ import annotations

import struct

WIRE_HEADER_LEN = 8
WIRE_ID_RTP = 0x66  # "streamer-input" id of the game-phase session (not the display link)
WIRE_ID_RPC = 0xFFFA  # RPC id of the game-phase session on its MuxedChannel.
# INVALID on the display link: the controller rejects it there and drops the
# record ("Failed to parse WireHeader2."): seen live as 200 requests without a reply.
WIRE_ID_DC_RPC_CONTROLLER = 0x74  # id the controller uses when it is
WIRE_ID_DC_RPC_DISPLAY = 0x75  # ... the DTLS client (seen live: its DisconnectRequest); 0x75 = the other side
WIRE_IDS_DC_RPC = (WIRE_ID_DC_RPC_CONTROLLER, WIRE_ID_DC_RPC_DISPLAY)


def wire_id_valid(message_id: int) -> bool:
    """A WireHeader2 id is accepted only if it is 1..255 or one of the
    keepalive ids 0xFFFE / 0xFFFF.  The display link has no per-id demux beyond this:
    the payload of any valid id goes to the single reader (HandleIncomingRpcMessage)."""
    return 1 <= message_id <= 0xFF or message_id in (0xFFFE, 0xFFFF)


def pack_wire_message(message_id: int, payload: bytes, strict_id: bool = True) -> bytes:
    """WireHeader2 + payload, as the controller writes it:
    byte 0 = 0 (version nibble 0), byte 1 = 0, bytes 2-3 = message id (LE),
    bytes 4-6 = total length incl. the header (LE, 24 bit), byte 7 = 0.
    Refuses ids the controller would drop (wire_id_valid) unless strict_id is False
    (the game session's 0xFFFA RPC channel, whose acceptance is not verified)."""
    if strict_id and not wire_id_valid(message_id):
        raise ValueError(f"WireHeader2 id {message_id:#x} is dropped by the controller")
    total = WIRE_HEADER_LEN + len(payload)
    if total > 0xFFFFFF:
        raise ValueError("WireHeader2 total length is 24 bits")
    return struct.pack("<BBHI", 0, 0, message_id & 0xFFFF, total) + payload


def parse_wire_message(record: bytes, strict_id: bool = True) -> tuple[int, bytes]:
    """(message_id, payload) of one DTLS application-data record.  Mirrors the
    controller's parser: version nibble (byte 0 >> 4)
    must be 0, total length >= 8 and equal to the record length (one message per
    record, no reassembly); bytes 1 and 7 and the low nibble of byte 0 are
    ignored; the id must pass wire_id_valid unless strict_id is False."""
    if len(record) < WIRE_HEADER_LEN:
        raise ValueError(f"record of {len(record)} bytes is shorter than a WireHeader2")
    if record[0] >> 4:
        raise ValueError(f"Invalid header version {record[0] >> 4}")
    message_id = struct.unpack_from("<H", record, 2)[0]
    total = struct.unpack_from("<I", record, 4)[0] & 0xFFFFFF
    if total < WIRE_HEADER_LEN or total != len(record):
        raise ValueError(f"Failed to parse WireHeader2: total_length {total} != record length {len(record)}")
    if strict_id and not wire_id_valid(message_id):
        raise ValueError(f"Failed to parse WireHeader2: message id {message_id:#06x}")
    return message_id, record[WIRE_HEADER_LEN:]
