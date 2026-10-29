"""stadiacast.helpers.sntp_server: NTP timestamps and the shape of a server reply (no socket)."""

from __future__ import annotations

import struct
import time

from stadiacast.helpers.sntp_server import NTP_EPOCH_OFFSET, NTP_PACKET_LEN, build_reply, ntp_timestamp


def test_ntp_timestamp_is_seconds_since_1900_in_32_32_fixed_point():
    assert ntp_timestamp(0.0) == struct.pack("!II", NTP_EPOCH_OFFSET, 0)
    secs, frac = struct.unpack("!II", ntp_timestamp(1.5))
    assert (secs, frac) == (NTP_EPOCH_OFFSET + 1, 1 << 31)


def test_reply_echoes_the_client_transmit_timestamp_and_carries_the_host_clock():
    client_xmit = bytes(range(8))
    request = bytes([(4 << 3) | 3]) + b"\x00\x06" + b"\x00" * 37 + client_xmit  # v4, mode 3 (client), poll 6
    assert len(request) == NTP_PACKET_LEN
    before = time.time()
    reply = build_reply(request, before)
    after = time.time()
    assert len(reply) == NTP_PACKET_LEN
    assert reply[0] == (4 << 3) | 4  # no leap warning, version 4, mode 4 (server)
    assert reply[1] == 1  # stratum 1
    assert reply[2] == 6  # the client's poll interval, echoed
    assert reply[12:16] == b"LOCL"
    assert reply[24:32] == client_xmit  # originate timestamp
    assert reply[32:40] == ntp_timestamp(before)  # receive timestamp
    xmit_secs = struct.unpack("!I", reply[40:44])[0] - NTP_EPOCH_OFFSET
    assert int(before) <= xmit_secs <= int(after) + 1
    # a request that does not say its version is answered as version 4
    assert build_reply(b"\x03" + request[1:], before)[0] == (4 << 3) | 4
