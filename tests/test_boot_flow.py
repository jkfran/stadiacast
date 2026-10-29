"""End to end on the display link: HTTPS boot, discovery, the GMsg answer, ICE/DTLS, the display RPC
handshake to READY and input, against tests/fake_controller.FakeController over loopback."""

from __future__ import annotations

import asyncio
import time

import pytest

from stadiacast.protos import Protos
from stadiacast.rtc import WIRE_ID_DC_RPC_DISPLAY, pack_wire_message
from tests.fake_controller import FakeController
from tests.helpers import connect_firmware_flow, http_records, make_receiver, no_log, single


def test_firmware_flow_offer_in_discover_display_answer_via_gmsg(tmp_path):
    async def run():
        rx = make_receiver(tmp_path)
        await rx.start()
        fc = FakeController(
            "127.0.0.1",
            rx.http.https_port,
            rx.protos,
            log=no_log,
            dtls_role="client",
            controlling=True,
            raw_dtls=True,
        )
        try:
            boot = await fc.boot()
            assert boot["generate_204"] == 204
            assert boot["stadiaconfig"][0] == 200
            assert boot["omaha"][0] == 200
            assert '<updatecheck status="noupdate"/>' in boot["omaha"][1]
            assert boot["clearcut"] == (200, 30 * 60 * 1000)
            assert boot["firmware_version_check"] == (200, False)
            st, reg = boot["register_controller"]
            assert st == 200
            assert reg["poll_interval_ms"] == "1000"
            assert reg["registration_token"]
            assert single(rx).session["registered"]["device_id"] == "fake-device"
            rec = http_records(rx, "rpc:RegisterController")[-1]
            assert rec["request"]["device_id"] == "fake-device"
            assert rec["request"]["attempt_id"] == fc.attempt_id
            assert any(
                r.get("sni") == "cloudcast-pa.googleapis.com" for r in rx.log.records if r["event"] == "http_request"
            )

            await fc.start_udp()
            # nothing pending yet: an idle poll is an empty 200 (firmware event 10)
            assert await fc.poll_gmsg() == (None, None)
            disc = await fc.discover(keys=[1, 2, 3, 4])
            assert disc.display_token
            assert disc.display_id
            assert single(rx).session["offers_received"] == 1
            assert single(rx).session["gmsg"]["pending_id"] == 1
            assert single(rx).session["discovery"]["typed_keys"] == [1, 2, 3, 4]
            assert single(rx).session["discovery"]["has_offer"]
            assert single(rx).endpoint.controlling is False
            assert single(rx).endpoint.remote.ufrag == fc.endpoint.ufrag
            env, bundle = await fc.poll_gmsg()
            assert env.message.id == 1
            assert env.message.type == 42
            ans = bundle.sdp.sdp_answer
            assert bundle.sdp.type == rx.protos.SDP_ANSWER
            assert ans.ice_ufrag == single(rx).endpoint.ufrag
            assert ans.use_boreal
            assert ans.boreal_transport == 2
            assert ans.video_format.codec == 3
            assert ans.audio_format.codec == 1
            assert ans.disable_frame_tokens
            assert fc.remote_candidates == [("127.0.0.1", single(rx).endpoint.local_port)]
            # the next poll acks the id; nothing else is pending
            assert await fc.poll_gmsg() == (None, None)
            assert single(rx).session["gmsg"]["acked"] == 1
            assert single(rx).session["gmsg"]["pending_id"] is None
            assert single(rx).session["gmsg"]["last_ack"] == 1

            assert ans.raw_dtls
            assert ans.delayed_acks
            fc.connect()
            await fc.wait_connected(10)
            await asyncio.wait_for(single(rx).endpoint.connected.wait(), 10)
            assert single(rx).endpoint.dtls.role == "server"
            assert fc.endpoint.dtls.role == "client"
            assert single(rx).session["rtc"]["fingerprint_verified"] is True

            # post-DTLS: OnConnected -> ResolveDiscovery,
            # and only then does the display ask for the controller state on 0xFFFA;
            # the answer is ControllerToDisplay #2
            await asyncio.wait_for(fc.display_ready.wait(), 5)
            assert fc.resolve_status == 200
            assert fc.early_rpc_records == 0
            assert http_records(rx, "rpc:ResolveDiscovery")[-1]["response_len"] == 0
            trig = [r for r in rx.log.records if r["event"] == "display_rpc_trigger"]
            assert [r["reason"] for r in trig] == ["resolve-discovery"]
            rec = bytes.fromhex(next(r for r in rx.log.records if r["event"] == "rpc_out")["record"])
            assert rec[:4] == b"\x00\x00\x75\x00"
            assert rec[7] == 0
            assert int.from_bytes(rec[4:7], "little") == len(rec)  # total_length
            req = fc.rpc_in[0]
            assert req["kind"] == "request"
            assert req["ticket"] is not None
            assert req["payload"].get_controller_state.HasField("display_supports_send_log_report")
            assert req["payload"].get_controller_state.display_supports_send_log_report is False
            for _ in range(50):
                if single(rx).session["display"]["controller_state"] is not None:
                    break
                await asyncio.sleep(0.02)
            assert single(rx).session["display"]["controller_state"]["device_id"] == "fake-device"
            # READY: clock sync must echo f1 (remote_clock.cc CHECKs rtt >= 0), keepalives get OK
            t = await fc.get_current_time()
            assert t["echo"] == t["t0"]
            assert t["rtt_us"] >= 0
            assert abs(t["display_time_us"] - t["t0"]) < 5_000_000
            assert (await fc.keepalive())["code"] == 0
            assert single(rx).session["display"]["state"] == "ready"
            assert single(rx).session["display"]["time_replies"] == 1
            assert single(rx).session["display"]["keepalive_replies"] == 1
            assert single(rx).session["display"]["wire_in"] == {"0x0074": 3}
            assert fc.wire_dropped == 0

            # input on the display link: RpcNotification ControllerToDisplay #3 (HandleInput)
            assert fc.send_input_rpc(buttons=0x10, lx=-1234, rt=200)
            ev = await rx.wait_event(5)
            assert ev["type"] == "rpc_input"
            assert ev["pressed"] == [4]
            assert ev["axes"]["lx"] == -1234
            assert ev["pressed_names"] == ["menu"]
            assert ev["triggers"]["r"] == 200
            assert fc.send_input_rpc(buttons=0x0)
            ev = await rx.wait_event(5)
            assert ev["released"] == [4]
            assert single(rx).session["display"]["input_rpc"] == 2

            # input: plain RTP on sub-channel 0x66 inside DTLS, acks come back the same way
            seq = fc.send_gamepad(buttons=0x10, lx=-1234, rt=200)
            ev = await rx.wait_event(5)
            assert ev["type"] == "gamepad"
            assert ev["pressed"] == [4]
            assert ev["axes"]["lx"] == -1234
            assert ev["axes"]["rt"] == 200
            assert await fc.wait_ack(5) == seq
            seq = fc.send_gamepad(buttons=0x0, compressed=True)
            ev = await rx.wait_event(5)
            assert ev["released"] == [4]
            assert await fc.wait_ack(5) == seq
            assert single(rx).session["display"]["state"] == "input"
            assert single(rx).endpoint.stats["rtp_in"] == 2
            assert [r["via"] for r in rx.log.records if r["event"] == "rtp_input"] == ["dtls", "dtls"]
            assert fc.endpoint.stats["rtp_in"] == 0  # nothing arrived as SRTP on the wire
        finally:
            fc.close()
            await rx.stop()

    asyncio.run(run())


def test_second_boot_resets_the_ack_release_clock(tmp_path):
    """Two full boots under one server process (report wakes alternate with full
    boots): the second boot's ACK poll must be held against ITS OWN dtls-connected,
    not answered at once because the first boot's DTLS time is still set (an
    immediate ACK answer is the instant power-off seen live)."""

    async def run():
        rx = make_receiver(tmp_path, gmsg_hold=1.0, gmsg_ack_release=0.2)
        await rx.start()
        fcs = []
        try:
            for boot in (1, 2):
                fc = FakeController(
                    "127.0.0.1",
                    rx.http.https_port,
                    rx.protos,
                    log=no_log,
                    dtls_role="client",
                    controlling=True,
                    raw_dtls=True,
                )
                fcs.append(fc)
                await fc.boot()
                await fc.start_udp()
                await fc.discover(keys=[1, 2, 3, 4])
                env, _ = await fc.poll_gmsg()  # the SDP answer
                assert env.message.id == boot
                if boot == 1:
                    for _ in range(200):  # the fake runs DTLS on its own
                        if single(rx)._dtls_at is not None:
                            break
                        await asyncio.sleep(0.01)
                    assert single(rx)._dtls_at is not None
                    await asyncio.sleep(0.3)  # boot 1 is now past DTLS + 0.2 s
                    assert await fc.poll_gmsg() == (None, None)
                else:
                    # boot 2's ACK poll: without the reset, boot 1's stale DTLS time would
                    # release it at once; with it, it is held until boot 2's DTLS + 0.2 s
                    t0 = time.monotonic()
                    assert await fc.poll_gmsg() == (None, None)
                    t1 = time.monotonic()
                    assert single(rx)._dtls_at is not None
                    assert single(rx)._dtls_at > t0 - 0.1
                    assert t1 - t0 >= 0.1
                    assert abs((t1 - single(rx)._dtls_at) - 0.2) < 0.08
            resets = [r for r in rx.log.records if r["event"] == "boot_state_reset"]
            assert len(resets) == 1
            assert resets[0]["previous_dtls"] is True
        finally:
            for fc in fcs:
                fc.close()
            await rx.stop()

    asyncio.run(run())


def test_display_rpc_time_sync_unsupported_and_unknown_requests(tmp_path):
    async def run():
        rx = make_receiver(tmp_path, time_sync="unsupported")
        await rx.start()
        fc = FakeController(
            "127.0.0.1",
            rx.http.https_port,
            rx.protos,
            log=no_log,
            dtls_role="client",
            controlling=True,
            raw_dtls=True,
        )
        try:
            await fc.boot()
            await fc.start_udp()
            await fc.discover()
            await fc.poll_gmsg()
            fc.connect()
            await fc.wait_connected(10)
            await asyncio.wait_for(fc.display_ready.wait(), 5)
            with pytest.raises(RuntimeError, match="status 5"):  # NOT_FOUND: controller stops clock sync
                await fc.get_current_time()
            other = rx.protos.ControllerToDisplay()
            other.message5.SetInParent()
            rpc = await fc.rpc_call(other)
            assert rpc["code"] == 12
            assert "Unimplemented" in rpc["error_message"]
            assert single(rx).session["display"]["requests"] == {"get_current_time": 1, "message5": 1}
        finally:
            fc.close()
            await rx.stop()

    asyncio.run(run())


def test_display_rpc_before_on_connected_reproduces_the_live_stall(tmp_path):
    """Seen live: a GetControllerStateRequest 0.3 s after DTLS, before
    the controller's OnConnected -> no ResolveDiscovery, no answer to any of the
    retries.  The old trigger (dtls) reproduces it against the stall model; the
    default trigger (resolve) never sends before ResolveDiscovery."""

    async def run():
        rx = make_receiver(
            tmp_path,
            display_rpc_trigger="dtls",
            display_rpc_delay=0.05,
            display_rpc_window=1.0,
            display_rpc_interval=0.1,
        )
        await rx.start()
        fc = await connect_firmware_flow(rx, early_rpc="stall", on_connected_delay=0.3)
        try:
            await asyncio.sleep(1.5)
            assert fc.early_rpc_records >= 1
            assert fc.resolve_status is None
            assert http_records(rx, "rpc:ResolveDiscovery") == []
            assert single(rx).session["display"]["rpc_in"] == 0
            assert single(rx).session["display"]["controller_state"] is None
            assert any(r["event"] == "display_rpc_unanswered" for r in rx.log.records)
        finally:
            fc.close()
            await rx.stop()

        (tmp_path / "resolve").mkdir()
        rx = make_receiver(tmp_path / "resolve", display_rpc_delay=0.05, display_rpc_interval=0.1)
        await rx.start()
        fc = await connect_firmware_flow(rx, early_rpc="stall", on_connected_delay=0.3)
        try:
            await asyncio.wait_for(fc.display_ready.wait(), 5)
            assert fc.early_rpc_records == 0
            assert fc.resolve_status == 200
            assert single(rx).session["display"]["trigger"] == "resolve-discovery"
            first_out = next(r for r in rx.log.records if r["event"] == "rpc_out")
            resolved = next(r for r in rx.log.records if r["event"] == "discovery_resolved")
            assert first_out["ts"] >= resolved["ts"]
            assert resolved["since_dtls_s"] >= 0.25
        finally:
            fc.close()
            await rx.stop()

    asyncio.run(run())


def test_get_controller_state_deferred_until_handoff_and_internal_retried(tmp_path):
    """A request after OnConnected but before discovery hands the DC to the app is
    queued (answered at the hand-off) and moves the DC to
    WAITING_ACCEPT_COMMAND, where re-sends get INTERNAL from the responder
    destructor; the receiver keeps going until the OK answer."""

    async def run():
        rx = make_receiver(tmp_path, display_rpc_trigger="dtls", display_rpc_delay=0.4, display_rpc_interval=0.1)
        await rx.start()
        fc = await connect_firmware_flow(rx, on_connected_delay=0.2, handoff_delay=0.6)
        try:
            await asyncio.wait_for(fc.display_ready.wait(), 5)
            for _ in range(50):
                if single(rx).session["display"]["controller_state"] is not None:
                    break
                await asyncio.sleep(0.02)
            d = single(rx).session["display"]
            assert d["controller_state"]["device_id"] == "fake-device"
            assert d["controller_state_errors"] >= 1
            assert any(r["event"] == "display_controller_state_error" and r["code"] == 13 for r in rx.log.records)
            assert fc.early_rpc_records == 0
            assert (await fc.keepalive())["code"] == 0
            assert d["state"] == "ready"
        finally:
            fc.close()
            await rx.stop()

    asyncio.run(run())


def test_fake_controller_drops_0xfffa_framed_rpc_like_the_firmware():
    """A live failure in miniature: a GetControllerStateRequest framed with
    id 0xFFFA dies in the controller's WireHeader2 check (no reply, link stays up); the
    same payload framed with 0x75 reaches the RPC reader."""
    p = Protos()
    fc = FakeController("127.0.0.1", 0, p, log=no_log, dtls_role="client", controlling=True, raw_dtls=True)
    fc.dc_state, fc.app_has_dc = "waiting_gcs", True  # after the hand-off (ACCEPT_COMMANDS)
    msg = p.DisplayToController()
    msg.get_controller_state.display_supports_send_log_report = False
    body = p.rpc_request(1, msg)
    legacy = bytes([0, 0, 0xFA, 0xFF]) + (8 + len(body)).to_bytes(3, "little") + b"\x00" + body
    fc._on_app_data(legacy)
    assert fc.wire_dropped == 1
    assert fc.rpc_in == []
    sent = []
    fc.endpoint.send_wire = lambda mid, _data: sent.append(mid) or True
    fc._on_app_data(pack_wire_message(WIRE_ID_DC_RPC_DISPLAY, body))
    assert len(fc.rpc_in) == 1
    assert fc.wire_dropped == 1
    assert sent == [0x74]  # the controller answers on its own SendDataAsync id
    fc.close()
