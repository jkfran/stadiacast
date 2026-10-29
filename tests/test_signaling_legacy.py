"""Legacy signaling experiments kept alive: the receiver as offerer (via GMsg) and bare WebRtcSdp bodies
on unknown paths.  These are also the only tests that run SRTP on the wire."""

from __future__ import annotations

import asyncio

from tests.fake_controller import FakeController
from tests.helpers import make_receiver, no_log, single


def test_receiver_offers_via_gmsg_controller_answers(tmp_path):
    async def run():
        rx = make_receiver(tmp_path, signaling_mode="offer")
        await rx.start()
        fc = FakeController(
            "127.0.0.1", rx.http.https_port, rx.protos, log=no_log, dtls_role="server", controlling=False
        )
        try:
            await fc.boot()
            await fc.start_udp()
            bundle = await fc.fetch_offer()
            offer = bundle.sdp.sdp_offer
            assert offer.ice_ufrag == single(rx).endpoint.ufrag
            assert offer.fingerprint.startswith("sha-256 ")
            assert [a.codec for a in offer.audio_format] == [1]  # opus
            assert fc.remote_candidates == [("127.0.0.1", single(rx).endpoint.local_port)]
            await fc.send_answer(bundle)  # bare WebRtcSdp nested in an unknown envelope, unknown path
            assert single(rx).session["answers_received"] == 1
            assert single(rx).endpoint.remote.ufrag == fc.endpoint.ufrag
            fc.connect()
            await fc.wait_connected(10)
            await asyncio.wait_for(single(rx).endpoint.connected.wait(), 10)
            assert single(rx).endpoint.dtls.role == "client"
            assert fc.endpoint.dtls.role == "server"
            assert single(rx).endpoint.srtp_in.profile == "SRTP_AES128_CM_SHA1_80"
            seq = fc.send_gamepad(buttons=0x3, ly=777)
            ev = await rx.wait_event(5)
            assert ev["pressed"] == [0, 1]
            assert ev["axes"]["ly"] == 777
            assert await fc.wait_ack(5) == seq
            rtp_logs = [r for r in rx.log.records if r["event"] == "rtp_input"]
            assert rtp_logs
            assert list(rtp_logs[-1]["ext"]) == [(3, "0001")]
        finally:
            fc.close()
            await rx.stop()

    asyncio.run(run())


def test_bare_offer_on_unknown_path_is_answered_in_body(tmp_path):
    async def run():
        rx = make_receiver(tmp_path)
        await rx.start()
        fc = FakeController(
            "127.0.0.1", rx.http.https_port, rx.protos, log=no_log, dtls_role="client", controlling=True
        )
        try:
            await fc.start_udp()
            bundle = await fc.post_offer()
            ans = bundle.sdp.sdp_answer
            assert ans.ice_ufrag == single(rx).endpoint.ufrag
            assert ans.use_boreal
            assert ans.boreal_transport == 2
            assert single(rx).session["offers_received"] == 1
            assert single(rx).endpoint.controlling is False
            fc.connect()
            await fc.wait_connected(10)
            await asyncio.wait_for(single(rx).endpoint.connected.wait(), 10)
            seq = fc.send_gamepad(buttons=0x3, compressed=True)
            ev = await rx.wait_event(5)
            assert ev["pressed"] == [0, 1]
            assert await fc.wait_ack(5) == seq
        finally:
            fc.close()
            await rx.stop()

    asyncio.run(run())
