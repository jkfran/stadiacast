"""The receiver's own HTTP surface: /_status, the live state (/_input, /_events), /_shutdown, unknown
paths, canned and overridden RPC responses and the clock pin."""

from __future__ import annotations

import asyncio
import datetime
import email.utils
import json
from typing import Any

from cryptography import x509

from stadiacast import __version__, receiver
from stadiacast.http_server import HttpRequest
from tests.fake_controller import FakeController
from tests.helpers import connect_firmware_flow, make_receiver, no_log, single, wait_for


def test_status_and_unknown_paths(tmp_path):
    async def run():
        rx = make_receiver(tmp_path)
        await rx.start()
        fc = FakeController("127.0.0.1", rx.http.https_port, rx.protos, log=no_log)
        try:
            st, _, body = await fc.request("GET", "whatever.local", "/_status", port=rx.http.http_ports[0], tls=False)
            assert st == 200
            assert b'"controllers"' in body
            st, _, _ = await fc.request(
                "POST", "cloudcast-pa.googleapis.com", "/v1/unknown/thing", b"\x08\x05", "application/x-protobuf"
            )
            assert st == 404
            rec = [r for r in rx.log.records if r["event"] == "http_request"][-1]
            assert rec["decoded"] == [{"f": 1, "varint": 5}]
            assert rec["body_file"].endswith(".bin")
            # an unknown v1: method still gets an empty 200
            st, _, body = await fc.request(
                "POST", "cloudcast-pa.googleapis.com", "/v1:Nope?key=K", b"\x08\x05", "application/x-protobuf"
            )
            assert (st, body) == (200, b"")
        finally:
            fc.close()
            await rx.stop()

    asyncio.run(run())


def test_rpc_field_overrides_and_canned_bodies(tmp_path):
    async def run():
        rx = make_receiver(
            tmp_path,
            rpc_fields={
                "RegisterController": {"poll_interval_ms": 2500, "pending": {"value": True}},
                "DiscoverDisplay": {"display_id": "tv"},
            },
            rpc_responses={"ResolveDiscovery": b"\x08\x01"},
        )
        await rx.start()
        fc = FakeController("127.0.0.1", rx.http.https_port, rx.protos, log=no_log)
        try:
            boot = await fc.boot()
            assert boot["register_controller"][1]["poll_interval_ms"] == "2500"
            assert boot["register_controller"][1]["pending"] == {"value": True}
            await fc.start_udp()
            assert (await fc.discover()).display_id == "tv"
            assert await fc.resolve() == (200, b"\x08\x01")
        finally:
            fc.close()
            await rx.stop()

    asyncio.run(run())


def test_clock_pin_auto_midpoint_of_cert_window(tmp_path):
    """--clock-pin auto: the controller's clock (Date header + GetCurrentTime
    replies) must sit inside the served certificate's validity window, so the
    expiry check never fails."""

    async def run():
        rx = make_receiver(tmp_path, clock_pin="auto")
        await rx.start()
        try:
            with open(rx.cfg.cert, "rb") as fh:
                cert = x509.load_pem_x509_certificate(fh.read())
            lo = cert.not_valid_before_utc.timestamp()
            hi = cert.not_valid_after_utc.timestamp()
            t = rx.now()
            assert lo <= t <= hi
            hdr = email.utils.formatdate(rx.now(), usegmt=True)
            assert lo <= email.utils.parsedate_to_datetime(hdr).timestamp() <= hi
            assert any(r["event"] == "clock_pinned" for r in rx.log.records)
        finally:
            await rx.stop()

    asyncio.run(run())


def test_clock_pin_fixed_date_freezes_the_controller_clock(tmp_path):
    """An explicit pin anchors the clock; it advances with uptime but stays at
    the pinned date (the cert window is judged against it forever)."""

    async def run():
        rx = make_receiver(tmp_path, clock_pin="2026-06-01T12:00:00Z")
        await rx.start()
        try:
            epoch = datetime.datetime(2026, 6, 1, 12, 0, 0, tzinfo=datetime.UTC).timestamp()
            t1, t2 = rx.now(), rx.now()
            assert epoch <= t1 < epoch + 30  # pinned date, small drift
            assert t2 >= t1  # time still flows forward within a session
            assert t2 < epoch + 3600  # ...but stays at the pinned date
        finally:
            await rx.stop()

    asyncio.run(run())


def test_the_control_endpoints_take_no_requests_from_web_pages(tmp_path):
    """A browser sends ``Origin`` with what a page asks it to post: refused, since the endpoints
    have no login.  Reading the status and requests without the header work as before."""

    async def run():
        rx = make_receiver(tmp_path)
        await rx.start()
        fc = FakeController("127.0.0.1", rx.http.https_port, rx.protos, log=no_log)
        port = rx.http.http_ports[0]
        page = [("Origin", "http://example.com")]
        try:
            for path in ("/_rumble", "/_start_game", "/_audio"):
                st, _, body = await fc.request("POST", "x.local", path, b"{}", "text/plain", page, port=port, tls=False)
                assert st == 403 and b"web pages" in body
                st, _, _ = await fc.request("POST", "x.local", path, b"{}", "text/plain", port=port, tls=False)
                assert st == 409  # no controller yet: it got past the check
            st, _, body = await fc.request("GET", "x.local", "/_status", headers=page, port=port, tls=False)
            assert st == 200 and b'"controllers"' in body
        finally:
            fc.close()
            await rx.stop()

    asyncio.run(run())


class EventsClient:
    """A client of ``/_events``: the response head, then one document per event."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, head: str) -> None:
        self.reader, self.writer, self.head = reader, writer, head

    @classmethod
    async def open(cls, port: int) -> EventsClient:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET /_events HTTP/1.1\r\nHost: receiver.local\r\n\r\n")
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 3)
        return cls(reader, writer, head.decode("latin-1"))

    async def event(self, timeout: float = 3.0) -> dict[str, Any]:
        while True:
            block = (await asyncio.wait_for(self.reader.readuntil(b"\n\n"), timeout)).decode()
            fields = dict(line.split(": ", 1) for line in block.strip().split("\n"))
            if fields.get("event") == "input":
                document: dict[str, Any] = json.loads(fields["data"])
                return document

    async def until(self, cond: Any, timeout: float = 3.0) -> dict[str, Any]:
        """The first event whose document meets ``cond``."""
        while True:
            document = await self.event(timeout)
            if cond(document):
                return document

    def close(self) -> None:
        self.writer.close()


async def get_json(port: int, path: str) -> tuple[int, Any]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(f"GET {path} HTTP/1.1\r\nHost: receiver.local\r\nConnection: close\r\n\r\n".encode())
    raw = await asyncio.wait_for(reader.read(), 3)
    writer.close()
    head, _, body = raw.partition(b"\r\n\r\n")
    return int(head.split()[1]), json.loads(body) if body else None


def test_input_is_what_each_controller_is_doing_right_now(tmp_path):
    """``/_input``: the state of the link, what is held and where the sticks are, per controller.
    A controller that is switched off holds nothing any more."""

    async def run():
        rx = make_receiver(tmp_path, display_rpc_delay=0.05)
        await rx.start()
        port = rx.http.http_ports[0]
        fc = None
        try:
            assert await get_json(port, "/_input") == (
                200,
                {"version": __version__, "gamepad": "off", "controllers": {}},
            )
            fc = await connect_firmware_flow(rx)
            await asyncio.wait_for(fc.display_ready.wait(), 5)
            assert (await fc.keepalive())["code"] == 0
            c0 = (await get_json(port, "/_input"))[1]["controllers"]["c0"]
            assert (c0["display_state"], c0["ip"], c0["input"], c0["gamepad"]) == ("ready", "127.0.0.1", None, None)
            assert (c0["battery_percent"], c0["headset"]) == (None, False) and 0 <= c0["last_seen_s"] < 1

            assert fc.send_input_rpc(buttons=(1 << 12) | (1 << 9), lx=-1234, ly=77, rt=200)
            assert fc.send_battery(0.76)
            await wait_for(lambda: single(rx).session["display"]["battery_percent"] == 76)
            c0 = (await get_json(port, "/_input"))[1]["controllers"]["c0"]
            report = c0.pop("input")
            assert (c0["display_state"], c0["battery_percent"]) == ("input", 76)
            assert 0 <= report.pop("age_s") < 1
            assert report == {
                "buttons": 0x1200, "held": ["r1", "a"], "lx": -1234, "ly": 77, "rx": 0, "ry": 0, "l2": 0, "r2": 200
            }  # fmt: skip

            fc.power_off()
            await wait_for(lambda: single(rx).session["state"] == "dtls-closed")
            c0 = (await get_json(port, "/_input"))[1]["controllers"]["c0"]
            assert (c0["display_state"], c0["input"]) == ("idle", None)
        finally:
            if fc is not None:
                fc.close()
            await rx.stop()

    asyncio.run(run())


def test_events_sends_the_live_state_when_it_changes_and_ends_with_the_receiver(tmp_path, monkeypatch):
    monkeypatch.setattr(receiver, "EVENTS_HEARTBEAT", 60.0)  # only a change sends an event here

    async def run():
        rx = make_receiver(tmp_path, display_rpc_delay=0.05)
        await rx.start()
        client = await EventsClient.open(rx.http.http_ports[0])
        fc = None
        try:
            head = client.head.lower()
            assert client.head.startswith("HTTP/1.1 200 OK") and "content-type: text/event-stream" in head
            assert "content-length" not in head and "connection: close" in head and "cache-control: no-store" in head
            assert (await client.event())["controllers"] == {}  # the first event comes without a change

            fc = await connect_firmware_flow(rx)  # a new controller, then every step of its link
            await asyncio.wait_for(fc.display_ready.wait(), 5)
            assert (await fc.keepalive())["code"] == 0
            ready = await client.until(lambda doc: doc["controllers"].get("c0", {}).get("display_state") == "ready")
            assert ready["controllers"]["c0"]["input"] is None

            assert fc.send_input_rpc(buttons=1 << 12)
            held = await client.until(lambda doc: doc["controllers"]["c0"]["input"] is not None)
            assert held["controllers"]["c0"]["input"]["held"] == ["a"]
            assert held["controllers"]["c0"]["display_state"] == "input"

            fc.power_off()
            gone = await client.until(lambda doc: doc["controllers"]["c0"]["display_state"] == "idle")
            assert gone["controllers"]["c0"]["input"] is None

            assert len(rx.event_streams) == 1
            await rx.stop()
            assert await asyncio.wait_for(client.reader.read(), 3) == b""  # the stream ended
            assert not rx.event_streams
        finally:
            client.close()
            if fc is not None:
                fc.close()
            if not rx.stopping:
                await rx.stop()

    asyncio.run(run())


def test_changes_that_come_together_share_one_event(tmp_path, monkeypatch):
    monkeypatch.setattr(receiver, "EVENTS_HEARTBEAT", 60.0)
    monkeypatch.setattr(receiver, "EVENTS_MIN_GAP", 0.05)

    async def run():
        rx = make_receiver(tmp_path)
        stream = receiver.LiveStream(rx)
        rx.event_streams.add(stream)
        try:
            assert (await stream.next_chunk() or b"").startswith(b"retry: 1000\n\nevent: input\ndata: {")
            for _ in range(20):
                rx.live_changed()
            assert (await asyncio.wait_for(stream.next_chunk(), 1) or b"").startswith(b"event: input\ndata: {")
            waiting = asyncio.ensure_future(stream.next_chunk())  # nothing changed since: no further event
            await asyncio.sleep(0.2)
            assert not waiting.done()
            rx.stopping = True
            rx.live_changed()
            assert await asyncio.wait_for(waiting, 1) is None  # the receiver stops: the stream ends
        finally:
            stream.close()
        assert not rx.event_streams

    asyncio.run(run())


def test_an_idle_events_stream_repeats_the_state_and_is_let_go_of_when_its_client_leaves(tmp_path, monkeypatch):
    """Without a change the state is sent again every heartbeat, which is also how a client that
    left is found out.  The streams open at once are limited."""
    monkeypatch.setattr(receiver, "EVENTS_HEARTBEAT", 0.05)
    monkeypatch.setattr(receiver, "MAX_EVENT_STREAMS", 2)

    async def run():
        rx = make_receiver(tmp_path)
        await rx.start()
        port = rx.http.http_ports[0]
        clients = [await EventsClient.open(port), await EventsClient.open(port)]
        try:
            for client in clients:
                for _ in range(3):
                    assert (await client.event())["controllers"] == {}
            status, body = await get_json(port, "/_events")
            assert status == 503 and body == {"error": "2 streams are open already"}
            st, _, _ = await FakeController("127.0.0.1", rx.http.https_port, rx.protos, log=no_log).request(
                "POST", "x.local", "/_events", port=port, tls=False
            )
            assert st == 405 and len(rx.event_streams) == 2
            clients[0].close()
            await wait_for(lambda: len(rx.event_streams) == 1)
            clients.append(await EventsClient.open(port))  # its place is free again
            assert (await clients[2].event())["controllers"] == {}
        finally:
            for client in clients:
                client.close()
            await rx.stop()

    asyncio.run(run())


def test_shutdown_is_taken_from_the_receivers_own_computer_only(tmp_path):
    async def run():
        rx = make_receiver(tmp_path)
        await rx.start()
        fc = FakeController("127.0.0.1", rx.http.https_port, rx.protos, log=no_log)
        port = rx.http.http_ports[0]
        try:
            st, _, _ = await fc.request("GET", "x.local", "/_shutdown", port=port, tls=False)
            assert st == 405
            page = [("Origin", "http://example.com")]
            st, _, body = await fc.request("POST", "x.local", "/_shutdown", b"", None, page, port=port, tls=False)
            assert st == 403 and b"web pages" in body
            elsewhere = HttpRequest(
                "POST", "/_shutdown", "HTTP/1.1", [], b"", ("192.168.2.4", 50000), "c9", None, None, path="/_shutdown"
            )
            answer = await rx.handle(elsewhere)
            assert answer.status == 403 and b"the computer the receiver runs on" in answer.body
            assert not rx.stop_requested.is_set()

            st, _, body = await fc.request("POST", "x.local", "/_shutdown", port=port, tls=False)
            assert (st, json.loads(body)) == (200, {"ok": True}) and rx.stop_requested.is_set()
            assert [r["peer"][0] for r in rx.log.records if r["event"] == "shutdown_requested"] == ["127.0.0.1"]
        finally:
            fc.close()
            await rx.stop()

    asyncio.run(run())
