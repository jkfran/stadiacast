"""stadiacast.helpers.dnswire and dns_sink: the wire format, redirects, NXDOMAIN and forwarding (loopback only)."""

from __future__ import annotations

import socket
import struct
import threading
from collections.abc import Iterator

import pytest

from stadiacast.helpers import dnswire
from stadiacast.helpers.dns_sink import Resolver
from stadiacast.log import EventLog


def encode_name(name: str) -> bytes:
    return b"".join(bytes([len(p)]) + p.encode() for p in name.split(".")) + b"\x00"


def query(name: str, qtype: int = 1, ident: int = 0x1234) -> bytes:
    return struct.pack("!HHHHHH", ident, 0x0100, 1, 0, 0, 0) + encode_name(name) + struct.pack("!HH", qtype, 1)


def rr(name: bytes, rtype: int, rdata: bytes, ttl: int = 60) -> bytes:
    return name + struct.pack("!HHIH", rtype, 1, ttl, len(rdata)) + rdata


def test_parse_message_decodes_every_record_type_the_sink_logs():
    q = query("a.example.com")
    pointer = b"\xc0\x0c"  # the question name, by compression pointer
    answers = (
        rr(pointer, 1, bytes([1, 2, 3, 4]))
        + rr(pointer, 28, bytes(range(16)))
        + rr(pointer, 5, encode_name("b.example.com"))
        + rr(pointer, 16, b"\x05hello\x05world")
        + rr(pointer, 33, struct.pack("!HHH", 10, 20, 443) + b"\xc0\x0c")
        + rr(pointer, 99, b"\xde\xad")
    )
    header = struct.pack("!HHHHHH", 0x1234, 0x8180, 1, 6, 0, 0)
    msg = dnswire.parse_message(header + q[12:] + answers)
    assert msg["id"] == 0x1234
    assert msg["qr"] is True
    assert msg["rcode"] == 0
    assert msg["questions"] == [
        {"name": "a.example.com", "type": "A", "qtype": 1, "class": 1, "unicast_response": False}
    ]
    assert [a["name"] for a in msg["answers"]] == ["a.example.com"] * 6
    assert [a["data"] for a in msg["answers"]] == [
        "1.2.3.4",
        "1:203:405:607:809:a0b:c0d:e0f",
        "b.example.com",
        ["hello", "world"],
        {"priority": 10, "weight": 20, "port": 443, "target": "a.example.com"},
        "dead",
    ]
    assert [a["type"] for a in msg["answers"]] == ["A", "AAAA", "CNAME", "TXT", "SRV", "99"]


def test_parse_name_rejects_truncated_names_and_pointer_loops():
    with pytest.raises(ValueError, match="past end"):
        dnswire.parse_name(b"\x05abc", 0)
    with pytest.raises(ValueError, match="compression loop"):
        dnswire.parse_name(b"\xc0\x00", 0)


def test_build_responses():
    q = query("cloudcast-pa.googleapis.com")
    a = dnswire.parse_message(dnswire.build_a_response(q, "192.168.2.1"))
    assert a["id"] == 0x1234
    assert a["rcode"] == 0
    assert a["questions"][0]["name"] == "cloudcast-pa.googleapis.com"
    assert a["answers"] == [
        {"name": "cloudcast-pa.googleapis.com", "type": "A", "class": 1, "ttl": 30, "data": "192.168.2.1"}
    ]
    empty = dnswire.parse_message(dnswire.build_a_response(query("x.googleapis.com", 28), None))
    assert empty["rcode"] == 0
    assert empty["answers"] == []
    nx = dnswire.parse_message(dnswire.build_rcode_response(q, dnswire.RCODE_NXDOMAIN))
    assert nx["rcode"] == 3
    assert nx["questions"][0]["name"] == "cloudcast-pa.googleapis.com"
    assert nx["answers"] == []
    garbage = dnswire.build_rcode_response(b"\x12\x34", dnswire.RCODE_FORMERR)
    assert garbage[:2] == b"\x12\x34"
    assert struct.unpack("!H", garbage[4:6])[0] == 0  # no question could be echoed


@pytest.fixture
def upstream() -> Iterator[tuple[int, list[bytes]]]:
    """A fake upstream resolver on loopback that answers every query with 93.184.216.34."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    sock.settimeout(0.2)
    seen: list[bytes] = []
    stop = threading.Event()

    def serve() -> None:
        while not stop.is_set():
            try:
                data, addr = sock.recvfrom(4096)
            except TimeoutError:
                continue
            seen.append(data)
            sock.sendto(dnswire.build_a_response(data, "93.184.216.34"), addr)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield sock.getsockname()[1], seen
    finally:
        stop.set()
        thread.join(1)
        sock.close()


def make_resolver(tmp_path, port: int, **kw) -> tuple[Resolver, EventLog]:
    log = EventLog(str(tmp_path / "dns.jsonl"), echo=False)
    resolver = Resolver(
        log,
        "127.0.0.1",
        "192.168.2.1",
        ["*.googleapis.com", "googleapis.com"],
        controller_ip="192.168.2.2",
        upstream_port=port,
        upstream_timeout=0.3,
        **kw,
    )
    return resolver, log


def test_redirected_names_are_answered_locally_and_the_rest_forwarded(tmp_path, upstream):
    port, seen = upstream
    resolver, log = make_resolver(tmp_path, port)
    local = dnswire.parse_message(resolver.handle(query("cloudcast-pa.googleapis.com"), ("192.168.2.2", 5353), "udp"))
    assert local["answers"][0]["data"] == "192.168.2.1"
    assert seen == []  # never left the host
    v6 = dnswire.parse_message(resolver.handle(query("googleapis.com", 28), ("192.168.2.2", 5353), "udp"))
    assert v6["answers"] == []
    other = dnswire.parse_message(resolver.handle(query("example.com"), ("192.168.2.2", 5353), "tcp"))
    assert other["answers"][0]["data"] == "93.184.216.34"
    assert len(seen) == 1
    actions = [(r["qname"], r["action"]) for r in log.records if r["event"] == "query"]
    assert actions == [
        ("cloudcast-pa.googleapis.com", "redirect"),
        ("googleapis.com", "redirect"),
        ("example.com", "forward"),
    ]
    forwarded = next(r for r in log.records if r.get("action") == "forward")
    assert forwarded["answers"] == ["93.184.216.34"]
    assert forwarded["rcode"] == 0
    assert forwarded["from_controller"] is True
    assert forwarded["proto"] == "tcp"


def test_log_only_forwards_even_the_redirected_names(tmp_path, upstream):
    port, seen = upstream
    resolver, _ = make_resolver(tmp_path, port, log_only=True)
    resp = dnswire.parse_message(resolver.handle(query("cloudcast-pa.googleapis.com"), ("10.0.0.9", 1), "udp"))
    assert resp["answers"][0]["data"] == "93.184.216.34"
    assert len(seen) == 1


def test_unreachable_upstream_gets_servfail_not_a_crash(tmp_path):
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    probe.bind(("127.0.0.1", 0))
    closed_port = probe.getsockname()[1]
    probe.close()
    resolver, log = make_resolver(tmp_path, closed_port)
    resp = dnswire.parse_message(resolver.handle(query("example.com"), ("192.168.2.2", 5353), "udp"))
    assert resp["rcode"] == dnswire.RCODE_SERVFAIL
    assert resp["id"] == 0x1234
    assert [r["action"] for r in log.records if r["event"] == "query"] == ["upstream_timeout"]


def test_garbage_gets_formerr(tmp_path):
    resolver, log = make_resolver(tmp_path, 1)
    resp = resolver.handle(b"\x00\x01nope", ("192.168.2.2", 5353), "udp")
    assert resp[:2] == b"\x00\x01"
    assert resp[3] & 0x0F == dnswire.RCODE_FORMERR
    assert [r["event"] for r in log.records] == ["query_unparsable"]
