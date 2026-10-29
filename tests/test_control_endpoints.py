"""The receiver's own HTTP surface: /_status, unknown paths, canned and overridden RPC responses and the
clock pin."""

from __future__ import annotations

import asyncio
import datetime
import email.utils

from cryptography import x509

from tests.fake_controller import FakeController
from tests.helpers import make_receiver, no_log


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
