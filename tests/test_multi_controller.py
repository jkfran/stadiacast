"""Several controllers at once: one session each, attribution of polls, input and control requests."""

from __future__ import annotations

import asyncio
import json

from tests.helpers import fake, make_receiver, wait_for


def test_two_controllers_connect_at_the_same_time_and_are_kept_apart(tmp_path):
    """Two controllers boot concurrently from the same address (the fakes share 127.0.0.1): each
    gets its own session, UDP ports, display token, GMsg answer and display link; input and rumble
    are attributed to the right one; a reboot of one controller leaves the other alone."""

    async def run():
        rx = make_receiver(
            tmp_path, display_rpc_delay=0.05, display_rpc_interval=0.1, start_game="auto", start_game_delay=0.05
        )
        await rx.start()
        fa = fake(rx, "fake-a", game_mode=True, game_framing="raw")
        fb = fake(rx, "fake-b", game_mode=True, game_framing="rtp")
        fakes = [fa, fb]
        try:
            await asyncio.gather(fa.boot(), fb.boot())
            a, b = rx.find("fake-a"), rx.find("fake-b")
            assert a is not None and b is not None and a is not b
            assert {a.label, b.label} == {"c0", "c1"} and rx.find("cloud-fake-a") is a and rx.find(a.label) is a
            assert (
                a.endpoint.local_port != b.endpoint.local_port
                and a.game.endpoint.local_port != b.game.endpoint.local_port
            )
            assert a.display_token != b.display_token and a.cert.fingerprint != b.cert.fingerprint
            await asyncio.gather(fa.start_udp(), fb.start_udp())
            await asyncio.gather(fa.discover(), fb.discover())
            (_, bundle_a), (_, bundle_b) = await asyncio.gather(fa.poll_gmsg(), fb.poll_gmsg())
            assert bundle_a.sdp.sdp_answer.ice_ufrag == a.endpoint.ufrag != bundle_b.sdp.sdp_answer.ice_ufrag
            assert bundle_b.sdp.sdp_answer.ice_ufrag == b.endpoint.ufrag
            assert fa.remote_candidates == [("127.0.0.1", a.endpoint.local_port)]
            assert fb.remote_candidates == [("127.0.0.1", b.endpoint.local_port)]
            assert fa.display_token == a.display_token and fb.display_token == b.display_token
            assert await asyncio.gather(fa.poll_gmsg(), fb.poll_gmsg()) == [(None, None), (None, None)]  # acks
            assert a.session["gmsg"]["acked"] == 1 and b.session["gmsg"]["acked"] == 1
            fa.connect()
            fb.connect()
            await asyncio.gather(fa.wait_connected(10), fb.wait_connected(10))
            await asyncio.gather(
                asyncio.wait_for(fa.display_ready.wait(), 5), asyncio.wait_for(fb.display_ready.wait(), 5)
            )
            assert (await fa.keepalive())["code"] == 0 and (await fb.keepalive())["code"] == 0
            assert a.display_state == "ready" and b.display_state == "ready"
            await wait_for(
                lambda: a.game.state["state"] == "game-started" and b.game.state["state"] == "game-started", 8
            )
            # display-link input carries the controller label
            assert fa.send_input_rpc(buttons=0x1)
            ev = await rx.wait_event(5)
            assert (ev["controller"], ev["pressed"]) == (a.label, [0])
            assert fb.send_input_rpc(buttons=0x2)
            ev = await rx.wait_event(5)
            assert (ev["controller"], ev["pressed"]) == (b.label, [1])
            # game-session input too (and it teaches each session its framing)
            fa.send_game_input(buttons=0x1000)
            ev = await rx.wait_event(5)
            assert (ev["controller"], ev["via"], ev["pressed"]) == (a.label, "game", [12])
            fb.send_game_input(buttons=0x2000)
            ev = await rx.wait_event(5)
            assert (ev["controller"], ev["via"], ev["pressed"]) == (b.label, "game", [13])
            assert a.game.state["input_framing"] == "raw" and b.game.state["input_framing"] == "rtp"
            # control endpoints must name the controller when there are two
            port = rx.http.http_ports[0]
            pulse = b'{"left":1000,"right":1000,"ms":300}'
            st, _, body = await fa.request(
                "POST", "x.local", "/_rumble", pulse, "application/json", port=port, tls=False
            )
            assert st == 400 and b'"c0"' in body and b'"c1"' in body
            st, _, _ = await fa.request(
                "POST", "x.local", f"/_rumble?controller={a.label}", pulse, "application/json", port=port, tls=False
            )
            assert st == 200
            await wait_for(lambda: fa.motors == (1000, 1000))
            assert fb.motors == (0, 0)
            await wait_for(lambda: fa.motors == (0, 0), 2)
            by_body = b'{"left":500,"right":0,"ms":300,"controller":"fake-b"}'
            st, _, _ = await fb.request(
                "POST", "x.local", "/_rumble", by_body, "application/json", port=port, tls=False
            )
            assert st == 200
            await wait_for(lambda: fb.motors == (500, 0))
            assert fa.motors == (0, 0)
            st, _, body = await fa.request(
                "POST", "x.local", "/_rumble?controller=nope", pulse, "application/json", port=port, tls=False
            )
            assert st == 404
            st, _, body = await fa.request("GET", "x.local", "/_status", port=port, tls=False)
            status = json.loads(body)
            assert set(status["controllers"]) == {"c0", "c1"}
            assert (
                status["controllers"][a.label]["device_id"] == "fake-a"
                and status["controllers"][b.label]["ip"] == "127.0.0.1"
            )
            # the console prefix carries the label and every http record from a known address is attributed
            assert rx.log.format(next(r for r in rx.log.records if r["event"] == "input")).startswith("[")
            assert any(r.get("controller") == b.label for r in rx.log.records if r["event"] == "http_request")
            # a reboot of A: same device id -> same session, reset; B is untouched
            fa.close()
            fa2 = fake(rx, "fake-a")
            fakes.append(fa2)
            await fa2.boot()
            await fa2.start_udp()
            await fa2.discover()
            assert rx.find("fake-a") is a and len(rx.controllers) == 2
            assert a.display_state == "idle" and a.game.state["state"] == "idle"
            assert b.display_state == "input" and b.game.state["state"] == "game-started"
            assert [r["controller"] for r in rx.log.records if r["event"] == "boot_state_reset"] == [a.label]
            assert (await fb.keepalive())["code"] == 0
        finally:
            for f in fakes:
                f.close()
            await rx.stop()

    asyncio.run(run())


def test_gmsg_poll_from_an_unknown_controller_gets_an_empty_answer(tmp_path):
    async def run():
        rx = make_receiver(tmp_path)
        await rx.start()
        fc = fake(rx, "fake-x")
        try:
            assert await fc.poll_gmsg() == (None, None)  # never registered: nothing to deliver, nothing created
            assert rx.controllers == {} and any(r["event"] == "gmsg_unattributed" for r in rx.log.records)
            await fc.boot()
            assert rx.find("fake-x") is rx.single()
        finally:
            fc.close()
            await rx.stop()

    asyncio.run(run())
